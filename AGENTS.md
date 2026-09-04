# Land Survey Scraper — Agent & Developer Guide

## Project Goal

Automate the land survey research workflow for Colorado counties. Given a street address,
the tool navigates county government websites to locate and download all relevant property
records (deeds, plats, surveys, easements, monument records) that a licensed surveyor would
normally gather manually.

**Primary users:** Land survey technicians and PLS (Professional Land Surveyors) who currently
spend significant time manually navigating county portals before fieldwork.

---

## Supported Counties (MVP)

| County | State | Scraper Key |
|--------|-------|-------------|
| Weld | CO | `CO_weld` |
| Denver | CO | `CO_denver` |
| Arapahoe | CO | `CO_arapahoe` |
| Jefferson | CO | `CO_jefferson` |

---

## Architecture

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
         │  browser-use (LLM agent) navigates portals
         │  Crawl4AI extracts structured document links
         ▼
    download.py         Async parallel downloads → local filesystem
```

### Key Modules

| Module | Purpose |
|--------|---------|
| `config.py` | Pydantic settings — lazy `get_settings()`, reads `.env` or injected env vars |
| `llm.py` | LLM factory — returns OpenRouter or Anthropic client based on credentials |
| `document_filter.py` | Defines document types and file formats relevant to land surveying |
| `geocode.py` | US Census Bureau geocoder → `GeocodedAddress` + `County` |
| `county_sites.py` | Static registry of supported county URLs |
| `scrapers/` | County-specific scrape logic (browser-use + Crawl4AI) |
| `pipeline.py` | Orchestration: geocode → dispatch → download |
| `download.py` | Async file downloader with semaphore concurrency |
| `console.py` | Rich terminal UI helpers |
| `types.py` | Shared dataclasses (`DocumentLink`) |

---

## County Workflows

### Weld County

**Data sources:**
- **Property Portal** — `apps.weld.gov/propertyportal/`
  React SPA. Parcel lookup by situs address, owner name, account number, or parcel number.
  Returns account number (e.g. R1234567), owner, and links to the property report page.
- **Property Report** — `propertyreport.weld.gov/?account=RXXXXXXX`
  Standard HTML page. Shows owner, legal description, and document history with Reception Numbers.
- **Clerk & Recorder** — `recording.weld.gov` (Tyler Technologies)
  Public access — click-through disclaimer only, no login required.
  Supports grantor/grantee name search and reception number search.

**Notes:**
- Reception Numbers are the canonical lookup key — collect them from the property report first.
- The old Java eRecording site (`erecording.weld.gov`) has been replaced by the Tyler Tech portal.
- Owner name search in Tyler Tech uses "Last Name, First Name" for individuals and full name for businesses.

**Full SOP:** See [docs/weld_county_sop.md](docs/weld_county_sop.md) for the human-validated
three-path procedure (Happy Path / Research Path 1 / Research Path 2), document-type cheat
sheet, URL reference, and a worked example for parcel `R1611986`. The SOP is the
source-of-truth specification the scraper should implement end-to-end — today only Path A
is automated.

### Denver County

**Data sources:**
- **Denver Assessor** — property lookup by address → Schedule Number
- **Denver Clerk & Recorder** — recorded documents search by Schedule Number

**Notes:**
- Denver is a combined city-county. Multiple departments manage permits and ROW.
- The Schedule Number (also called Account Number) is the join key between Assessor and Recorder.

### Arapahoe County

**Data sources:**
- **Arapahoe Assessor** — `arapahoegov.com/assessor` → parcel number
- **Arapahoe Recorder** — `recording.arapahoegov.com` → recorded documents

### Jefferson County

**Data sources:**
- **Jeffco Records Search** — `jeffco.us/1027/Records-Search` (direct search, no separate assessor step required)
- **Jeffco Assessor** — `jeffco.us/assessor` (used for parcel enrichment if needed)

---

## Document Types

All document types and accepted file formats are defined centrally in `document_filter.py`
as `SURVEY_DOCUMENT_TYPES` and `SURVEY_FILE_EXTENSIONS`. The `DocumentFilter` class converts
these into a prompt fragment injected into every browser-use agent task.

**Document categories covered:** survey plats, subdivision/exemption plats, vesting deeds,
easements, right-of-way dedications, liens, and encumbrances.

**File formats accepted:** PDF, TIF/TIFF, JPG/PNG, DWG/DXF (CAD), SHP/KML/KMZ (GIS), ZIP archives.

To adjust what gets collected, edit `SURVEY_DOCUMENT_TYPES` or `SURVEY_FILE_EXTENSIONS` in
`document_filter.py` — no scraper code changes needed.

---

## Statewide / Supplemental Sources (Future)

| Source | URL | Purpose |
|--------|-----|---------|
| Monument Records (DORA) | `dpo.colorado.gov/AES/MonumentRecords` | Corner records for field prep |
| Monument Records (cp-db) | `cp-db.com` | Alternate monument lookup |
| BLM GLO Records | `glorecords.blm.gov` | Original government survey plats |
| NOAA Geodesy | `geodesy.noaa.gov/datasheets/` | Control networks |
| USGS Topo | `ngmdb.usgs.gov/topoview/viewer/` | Quad sheets |
| COGCC GIS | `cogccmap.state.co.us/cogcc_gis_online/` | Energy/mineral records |
| DRCOG | `drcog.org/data-maps-modeling` | Regional planning data |

---

## Tool Stack

| Tool | Role |
|------|------|
| **browser-use** | LLM-driven browser agent. Navigates county portals using natural language task descriptions. Eliminates brittle CSS selectors — resilient to site layout changes. |
| **Crawl4AI** | Structured page extraction. Converts property result pages to JSON document lists. |
| **Playwright** | Underlying browser driver for browser-use (Chromium). |
| **httpx** | Async HTTP client for file downloads. |
| **pydantic-settings** | Typed configuration from `.env` or injected environment variables. |
| **langchain-openai** | OpenRouter LLM client (free models via OpenRouter API). |
| **langchain-anthropic** | Anthropic LLM client (fallback when `ANTHROPIC_API_KEY` is set). |
| **rich** | Terminal progress UI. |

---

## Configuration & Credentials

Settings are loaded via `get_settings()` (lazy, cached) from environment variables or a
local `.env` file. The app fails fast at startup with a clear error if required vars are missing.

| Env Var | Required | Description |
|---------|----------|-------------|
| `LLM_PROVIDER` | No | Provider to use: `nvidia`, `openrouter`, `anthropic`, `openai`. Auto-detects from available keys if omitted. |
| `MODEL` | Yes | Model string for the chosen provider (see examples below) |
| `NVIDIA_API_KEY` | If provider=nvidia | NVIDIA NIM API key (free tier at build.nvidia.com) |
| `OPENROUTER_API_KEY` | If provider=openrouter | OpenRouter API key (free models available) |
| `ANTHROPIC_API_KEY` | If provider=anthropic | Anthropic API key |
| `WELD_RECORDER_USERNAME` | Yes (Weld) | Weld County Recorder portal login (free registration at recording.weld.gov) |
| `WELD_RECORDER_PASSWORD` | Yes (Weld) | Weld County Recorder portal password |

**Recommended models by provider:**
- NVIDIA (free): `meta/llama-3.3-70b-instruct` or `nvidia/llama-3.1-nemotron-70b-instruct-hf`
- OpenRouter (free): `google/gemini-2.0-flash-exp:free`
- Anthropic: `claude-sonnet-4-6` (most capable), `claude-haiku-4-5` (fastest/cheapest)

**Local dev:** create a `.env` file (gitignored) in the project root:
```bash
OPENROUTER_API_KEY=sk-or-...
MODEL=google/gemini-2.0-flash-exp:free
WELD_ERECORDING_USERNAME=your_username
WELD_ERECORDING_PASSWORD=your_password
```

**Docker:** vars are passed via `--env-file .env` in the `make process` target.

> The Weld County eRecording credentials are a community login used by Colorado surveyors.
> Never hardcode them in source. Store only in `.env` or a secrets manager.

---

## Running the Tool

```bash
# Local
uv run land-survey-scraper "123 Main St, Greeley, CO 80631"

# Docker (preferred — handles Chromium and all deps)
make process ADDRESS="123 Main St, Greeley, CO 80631"

# Force a specific county (bypass geocoding)
uv run land-survey-scraper "123 Main St" --county CO_weld

# Save to a custom output directory
uv run land-survey-scraper "123 Main St, Denver, CO 80202" -t ./output

# Re-download files that already exist locally
uv run land-survey-scraper "123 Main St, Greeley, CO 80631" --no-skip-existing
```

Output is saved to `./tmp/{county_key}/{address_slug}/`.

---

## Adding a New County

1. Add an entry to `SUPPORTED_COUNTIES` in `county_sites.py` with the county name,
   state, scraper key, and relevant URLs.
2. Create `scrapers/{state}_{county}.py` exposing:
   ```python
   async def scrape(
       geocoded: GeocodedAddress,
       tmp_dir: Path,
       doc_filter: DocumentFilter = DEFAULT_FILTER,
   ) -> tuple[list[Path], str | None]
   ```
3. Register the scraper in `COUNTY_SCRAPERS` in `pipeline.py`.
4. Add tests in `tests/test_{county}_scraper.py`.

---

## Development

```bash
uv run pytest --cov=src tests/    # run tests with coverage
uv run ruff check src tests       # lint
uv run ruff format src tests      # format
make build                        # build Docker image
make process ADDRESS="..."        # run end-to-end
```

---

## Future Roadmap

- **AWS Secrets Manager** — graduate credentials out of `.env`
- **S3 output** — cloud storage for downloaded documents, team sharing
- **Document parsing** — Claude API to extract metadata, classify document types from content
- **Monument records** — integrate `cp-db.com` for field prep phase
- **Batch processing** — Lambda + EventBridge for multi-address jobs
- **Statewide sources** — BLM GLO, DORA monuments, USGS quads
