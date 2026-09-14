# Survey Art — Agent & Developer Guide

## Project Goal

Automate the land survey research workflow for Colorado counties. Given a street address,
the tool navigates county government websites to locate and download all relevant property
records (deeds, plats, surveys, easements, monument records) that a licensed surveyor would
normally gather manually.

**Primary users:** Land survey technicians and PLS (Professional Land Surveyors) who currently
spend significant time manually navigating county portals before fieldwork.

---

## Where to read what

This file covers only what isn't documented closer to the code. **Do not duplicate
material from these into this file** — they are the source of truth for their area:

| For | Read |
|---|---|
| Running the CLI, all flags, env vars, `.env` setup, output format, troubleshooting | [`README.md`](README.md) |
| Weld's end-to-end procedure (the spec the scraper implements) | [`docs/weld_county_sop.md`](docs/weld_county_sop.md) |
| AWS architecture, request/job flow, repo layout, local docker-compose stack | [`docs/architecture.md`](docs/architecture.md) |
| CloudFormation stacks, deploy model, deploy gotchas | [`infra/AGENTS.md`](infra/AGENTS.md) |
| Job broker API endpoints and auth | [`apps/api/AGENTS.md`](apps/api/AGENTS.md) |
| SPA structure and metadata rendering | [`apps/web/AGENTS.md`](apps/web/AGENTS.md) |
| Worker internals: job log streaming, property metadata, map capture | [`apps/worker/survey_art/AGENTS.md`](apps/worker/survey_art/AGENTS.md) |
| `Job` model, DynamoDB/S3 helpers, storage prefixes | [`packages/survey_shared/AGENTS.md`](packages/survey_shared/AGENTS.md) |
| Adding a new county (current `scrape()` signature) | [`README.md`](README.md) → "Adding a new county" |

Monorepo layout, one deployable per `apps/` subdir: `apps/api` (survey-api), `apps/web`
(SPA), `apps/dispatcher` (SQS→ECS Lambda), `apps/worker` (survey-art — core scraper +
worker, the heavy one). `packages/survey_shared` holds code shared *between* deployables
(jobs/AWS helpers) — not a service itself. `infra/` (CloudFormation + boto3 deploy
harness) is deploy tooling, not app code. Wired as a **uv workspace** so the API image
stays lean (no browser deps) — see [`apps/worker/survey_art/AGENTS.md`](apps/worker/survey_art/AGENTS.md)
for why the heavy deps are isolated there.

Common commands (`make help` lists all):

```bash
make up / make down / make logs   # local containerized stack (web + api + worker + LocalStack)
make test / make lint / make lock # quality (run in containers; host needs no uv)
make build-push                   # build + push api/worker images to ECR
make deploy                       # AWS deploy entrypoint — `make deploy help` lists targets
                                   # (bootstrap, network, ecr, backend, frontend, all, web, diff, destroy)
```

---

## Scraper architecture

```
Address Input
    │
    ▼
geocode.py          Census Bureau API → resolves address to county
    │
    ▼
pipeline.py         Dispatches to county-specific scraper via COUNTY_SCRAPERS dict
    │
    ├── scrapers/weld_county.py
    ├── scrapers/denver_county.py
    ├── scrapers/arapahoe_county.py
    └── scrapers/jefferson_county.py
         │  Weld: pure HTTP + Playwright. Others: browser-use (LLM agent) + Crawl4AI.
         ▼
    download.py         Async parallel downloads → local filesystem
```

### Key modules (`apps/worker/survey_art/`)

| Module | Purpose |
|--------|---------|
| `settings.py` | Pydantic settings — lazy `get_settings()`; LLM model + county portal credentials |
| `llm.py` | LLM factory for the browser-use scrapers |
| `document_filter.py` | Document types and file formats relevant to land surveying |
| `geocode.py` | US Census Bureau geocoder → `GeocodedAddress` + `County` |
| `county_sites.py` | Static registry of supported county URLs |
| `scrapers/` | County-specific scrape logic |
| `pipeline.py` | Orchestration: geocode → dispatch → download |
| `download.py` | Async file downloader with semaphore concurrency |
| `overview.py` | Incremental `overview.json` writer (per-phase, crash-safe) |
| `id_extraction.py` | Reads a survey PDF for the record IDs it cites — text layer if there is one, else Bedrock over tiled page images |
| `worker.py` | AWS job entrypoint (Fargate one-shot or local SQS poll) |
| `console.py` | Rich terminal UI helpers |
| `types.py` | Shared dataclasses (`DocumentLink`) |

> Note the two distinct settings accessors: `survey_art.settings.get_settings()` (scraper
> config: model, county logins) and `survey_shared.config.get_shared_settings()` (AWS
> wiring: queue, table, bucket). The worker imports both — don't conflate them.

---

## County data sources

Portal URLs live in `county_sites.py`. What's recorded here is the non-obvious part: which
identifier joins the systems together, and which quirks have already bitten us.

### Weld County

- **Property Portal** — `apps.weld.gov/propertyportal/` (React SPA). Parcel lookup by situs
  address, owner name, account number, or parcel number. Returns account number (e.g.
  `R1234567`), owner, and a link to the property report.
- **Property Report** — `propertyreport.weld.gov/?account=RXXXXXXX`. Server-rendered HTML;
  owner, legal description, and document history with Reception Numbers.
- **Clerk & Recorder** — `recording.weld.gov` (Tyler Technologies). Public access behind a
  click-through disclaimer; document *images* need a free registered account.

Notes:
- **Reception Numbers are the canonical lookup key** — collect them from the property
  report before touching the recorder portal.
- The old Java eRecording site (`erecording.weld.gov`) was replaced by the Tyler portal;
  the `WELD_ERECORDING_*` settings are vestigial.
- Tyler owner-name search wants "Last Name, First Name" for individuals, full name for
  businesses.
- [`docs/weld_county_sop.md`](docs/weld_county_sop.md) is the human-validated spec — read it
  before changing scraper logic. README's "Weld County — the SOP" section tracks which
  phases are actually implemented today.
- The SOP also documents two supplemental phases that run independent of the research
  path taken: retrieving the BLM GLO original survey of record (see "BLM GLO Records"
  below), and assembling the county/state road right-of-way packet. Neither is automated
  yet — see [`docs/weld_county_sop.md`](docs/weld_county_sop.md#whats-not-yet-automated).

### Denver County

- **Denver Assessor** — address → Schedule Number.
- **Denver Clerk & Recorder** (Kofile Tech, login required) — recorded documents by Schedule
  Number.
- The Schedule Number (a.k.a. Account Number) is the join key. Denver is a combined
  city-county, and multiple departments manage permits and ROW.

### Arapahoe County

- **Assessor** `arapahoegov.com/assessor` → parcel number →
  **Recorder** `recording.arapahoegov.com` → recorded documents.

### Jefferson County

- **Records Search** `jeffco.us/1027/Records-Search` — searchable directly, no separate
  assessor step. `jeffco.us/assessor` only if parcel enrichment is needed.

---

## Document Types

Document types and accepted file formats are defined centrally in `document_filter.py` as
`SURVEY_DOCUMENT_TYPES` and `SURVEY_FILE_EXTENSIONS`. `DocumentFilter` turns them into a
prompt fragment injected into every browser-use agent task. To change what gets collected,
edit those two constants — no scraper code changes needed.

Covered: survey plats, subdivision/exemption plats, vesting deeds, easements, ROW
dedications, liens, encumbrances. Formats: PDF, TIF/TIFF, JPG/PNG, DWG/DXF, SHP/KML/KMZ, ZIP.

---

## Statewide / Supplemental Sources (not yet integrated)

| Source | URL | Purpose |
|--------|-----|---------|
| Monument Records (DORA) | `dpo.colorado.gov/AES/MonumentRecords` | Corner records for field prep |
| Monument Records (cp-db) | `cp-db.com` | Alternate monument lookup |
| BLM GLO Records | `glorecords.blm.gov` | Original government survey plats — Weld's SOP calls this out as its own Phase 4, see [`docs/weld_county_sop.md`](docs/weld_county_sop.md#phase-4--glo-original-survey-of-record) |
| NOAA Geodesy | `geodesy.noaa.gov/datasheets/` | Control networks |
| USGS Topo | `ngmdb.usgs.gov/topoview/viewer/` | Quad sheets |
| COGCC GIS | `cogccmap.state.co.us/cogcc_gis_online/` | Energy/mineral records |
| DRCOG | `drcog.org/data-maps-modeling` | Regional planning data |

---

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
