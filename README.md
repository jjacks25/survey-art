# Survey Art

Automated land survey research for Colorado counties. Given a street address, account
number, parcel ID, owner name, or PLSS coordinates, the tool navigates county government
websites to locate and download all relevant property records — deeds, plats, surveys,
easements, right-of-way agreements — that a surveyor would normally gather manually.

> **Current focus:** Weld County. Other counties (Denver, Jefferson, Arapahoe) are
> scaffolded but less mature. See [docs/weld_county_sop.md](docs/weld_county_sop.md)
> for the human-validated source-of-truth procedure the Weld scraper implements.

---

## Quickstart

```bash
# 1. Install prerequisites (one time)
brew install python uv docker poppler
uv sync
uv run playwright install chromium

# 2. Create .env (see "Configuration" below)
cp .env.example .env  # then edit

# 3. Run for the example parcel
uv run survey-art "R1611986" --county CO_weld
```

Output lands in `tmp/CO_weld/R1611986/`:

```
tmp/CO_weld/R1611986/
├── overview.json       # all scraped data (see "Output format" below)
├── map.png             # ESRI satellite view with parcel boundary highlighted
└── reception_*.pdf     # downloaded recorded documents (when credentials set)
```

---

## How to run

The scraper has one positional argument (the **query**) and several optional flags.
The query auto-detects its type by pattern:

| Query example | Auto-detected as | SOP Priority |
|---|---|---|
| `"123 Main St, Greeley, CO 80631"` | Address | 1 |
| `"R1611986"` | Weld account number | (skips Phase 1) |
| `"095715000012"` | 12-digit Weld parcel ID | (skips Phase 1) |
| `"STRATUS DELANTERO"` (with `--owner`) | Owner name | 4 |

### All run modes

```bash
# 1. By address — geocodes to determine the county, then dispatches
uv run survey-art "1234 Main St, Greeley, CO 80631"

# 2. By account number — bypasses geocoding, fastest for Weld
uv run survey-art "R1611986" --county CO_weld

# 3. By parcel ID
uv run survey-art "095715000012" --county CO_weld

# 4. By owner name (SOP "Priority 4 — last resort")
uv run survey-art "any address" --county CO_weld --owner "STRATUS DELANTERO"

# 5. By Section / Township / Range (PLSS) — requires --sop-strict (browser walk)
uv run survey-art "any address" --county CO_weld \
    --str "15,5N,67W" --sop-strict

# 6. SOP-strict mode — literal Playwright walk through the GIS Hub UI
#    (slower; useful for demos or when the HTTP shortcut fails)
uv run survey-art "R1611986" --county CO_weld --sop-strict

# 7. Watch the browser drive itself (only useful with --sop-strict or Phase 3)
WELD_HEADED=1 uv run survey-art "R1611986" --county CO_weld --sop-strict

# 8. Custom output directory
uv run survey-art "R1611986" --county CO_weld -t ./output

# 9. Re-download files that already exist
uv run survey-art "R1611986" --county CO_weld --no-skip-existing

# 10. Suppress progress UI (CI-friendly)
uv run survey-art "R1611986" --county CO_weld --quiet
```

### Docker

Recommended for reproducibility and CI — no host Python/Chromium needed. Runs via the
local docker-compose stack (see [`docs/architecture.md`](docs/architecture.md) for what
else is in it — API, web, LocalStack).

```bash
make up                                                   # build + start the stack (one time / after changes)
make process ADDRESS="R1611986" ARGS="--county CO_weld"   # run end-to-end
make sh-worker                                            # interactive bash inside the worker container
make logs                                                 # tail stack logs
make down                                                 # stop and remove the stack
```

Pass extra flags via `ARGS="..."`:

```bash
make process ADDRESS="R1611986" ARGS="--county CO_weld --sop-strict"
make process ADDRESS="STRATUS DELANTERO" ARGS="--county CO_weld --owner 'STRATUS DELANTERO LLC'"
```

> **Docker limitation:** `WELD_HEADED=1` does nothing inside the container — there's no
> display. Use local `uv run` to watch the browser.

The worker mounts your `~/.aws` read-only and uses `AWS_PROFILE` (default: `default`).
LocalStack ignores credentials, but Bedrock doesn't and has no LocalStack equivalent, so
the worker talks to real AWS for that one client and to LocalStack for everything else.

### CLI reference

| Flag | Applies to | Default | Description |
|---|---|---|---|
| (positional) | All | — | Address, account, parcel ID, or any string to dispatch from |
| `-t / --tmp PATH` | All | `./tmp` | Output directory root |
| `--county KEY` | All | (geocode) | Override county dispatch (e.g. `CO_weld`). Required when query isn't an address. |
| `--no-skip-existing` | All | (skip) | Re-download files even if they're already on disk |
| `--quiet` | All | (verbose) | Suppress Rich progress panels |
| `--str S,T,R` | Weld | — | PLSS lookup, comma-separated (e.g. `15,5N,67W`). Requires `--sop-strict`. |
| `--owner NAME` | Weld | — | Owner-name lookup (SOP Priority 4) |
| `--sop-strict` | Weld | (off) | Drive the literal SOP browser walk instead of HTTP shortcut |
| `--version` | All | — | Print version |

### Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `MODEL` | Yes for non-Weld counties | — | Bedrock model ID or cross-region inference profile ID (e.g. `us.anthropic.claude-haiku-4-5-20251001-v1:0`) |
| `ID_EXTRACTION_MODEL` | No | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Bedrock model that reads a scanned ALTA for the record IDs it cites (Weld Schedule B-2 exception walk) |
| `AWS_REGION` | No | `us-west-2` | Bedrock region |
| `APPLICATION_MODE` | No | `regular` | `demo` caps the Schedule B-2 exception downloads at 5 so a walkthrough finishes in minutes |
| `WELD_RECORDER_USERNAME` | Optional (Weld Phase 3) | — | Login for `recording.weld.gov` |
| `WELD_RECORDER_PASSWORD` | Optional (Weld Phase 3) | — | Login for `recording.weld.gov` |
| `WELD_DOWNLOAD_CONCURRENCY` | No | `4` | How many recorder documents to fetch at once. A politeness limit against a county server, not a throughput dial — raise it a step at a time and watch for retry warnings in the log |
| `CO_DENVER_USERNAME` | Optional (Denver) | — | Login for the Denver Kofile recorder portal |
| `CO_DENVER_PASSWORD` | Optional (Denver) | — | Login for the Denver Kofile recorder portal |
| `WELD_HEADED` | No | `0` | Set to `1` to show the Playwright browser window |

**Weld's navigation uses no LLM.** Phases 1, 2, and 3 are pure HTTP + Playwright with
hard-coded selectors. The one exception is the Schedule B-2 exception walk, which reads
the downloaded ALTA for the documents it cites — free when the PDF has a text layer, and
Bedrock otherwise (see [Reading the ALTA](#reading-the-alta-schedule-b-2)). The Denver / Jefferson / Arapahoe
scrapers go through Bedrock throughout (`llm.py`). Auth is IAM only (the Fargate task
role in prod, or your local `aws sso login`/profile credentials via boto3's default
chain), so no API key belongs in `.env`.

Weld Phase 3 (document image download) requires a free registered account at
`recording.weld.gov` → Log In → "new registered user". Without credentials the scraper
still completes Phases 1 and 2, and `overview.json` will list each document with
`download_status: "failed"`.

---

## Output format

Every run produces `tmp/{county_key}/{address_slug}/overview.json` — an incremental
record updated after each phase. A crash mid-pipeline still leaves a valid file.

```json
{
  "meta": {
    "county_key": "CO_weld",
    "input_address": "R1611986, , CO ",
    "account": "R1611986",
    "sop_path": "A",
    "source_urls": ["https://apps.weld.gov/...", "https://propertyreport.weld.gov/..."],
    "last_updated": "2026-05-17T16:50:25+00:00"
  },
  "identify_results": {
    "owner": "STRATUS DELANTERO LLC",
    "account": "R1611986",
    "parcel_id": "095715000012",
    "address": "GREELEY",
    "subdivision": "",
    "section": "15",
    "township": "5N",
    "range": "67W",
    "section_township_range": "S15-T5N-R67W",
    "source": "http"
  },
  "account_information": { "account_type": "...", "legal_description": "...", "tax_year": "...", ... },
  "owners": { "Owner Name": "...", "Address": "..." },
  "land_information": { "Code": "...", "Description": "...", "Acres": "270.840", ... },
  "valuation_information": { "Actual Value": "37,320", ... },
  "tax_authorities": { "Tax Area": "...", "Mill Levy": "...", ... },
  "map": {
    "image_path": "tmp/CO_weld/R1611986/map.png",
    "iframe_url": "https://maps.weld.gov/mapanaccount/?Account=R1611986"
  },
  "document_history": [
    { "reception": "...", "rec_date": "...", "doc_type": "WD", "grantor": "...", "grantee": "...", "doc_fee": "...", "sale_date": "...", "sale_price": "...", "url": "..." }
  ],
  "decision_matrix": {
    "path": "direct",
    "reasoning": ["Direct extraction matched: ...", "Most recent SURV: ...", "→ Route to direct extraction."],
    "vesting_deed_present": true,
    "survey_present": true,
    "empty_state_message_present": false,
    "row_count": 5,
    "vesting_deeds": [ /* all matching rows */ ],
    "survey_rows":   [ /* all SURV rows */ ],
    "most_recent_survey": { /* the most recent SURV row */ },
    "most_recent_vesting_deed": { /* the most recent vesting deed */ }
  },
  "survey_documents": [
    { "reception": "4970002", "doc_type": "SWD", ..., "download_status": "downloaded", "downloaded_to": "tmp/.../reception_4970002.pdf" }
  ],
  "raw_report_fields": { /* every label parsed from propertyreport.weld.gov, verbatim/undeduplicated */ }
}
```

The schema is open: downstream consumers can read `Overview` via the
[overview.py](apps/worker/survey_art/overview.py) helper or just parse JSON.

---

## Supported counties

| County | State | Scraper key | Approach | Status |
|---|---|---|---|---|
| Weld | CO | `CO_weld` | Pure HTTP + Playwright (no LLM) | Direct extraction, partial-history, and owner-name search all working (needs registered recorder account); GLO and road ROW not yet automated |
| Denver | CO | `CO_denver` | browser-use + LLM | Works; requires `CO_DENVER_USERNAME/PASSWORD` |
| Jefferson | CO | `CO_jefferson` | Hybrid REST API + browser-use | Works (no auth) |
| Arapahoe | CO | `CO_arapahoe` | browser-use + LLM | Proof-of-concept |

---

## Weld County — the SOP

The Weld scraper implements the procedure documented in
**[docs/weld_county_sop.md](docs/weld_county_sop.md)**. That doc is the source of truth —
read it before modifying scraper logic. Key points:

- **Parcel discovery**: SOP Steps 1.1–1.5 yield an Identify Results panel
  with Owner / Account / Parcel / Address / Subdivision / S-T-R. Steps 1.6 + 1.7 capture
  all Property Report accordion sections (server-rendered) and a screenshot of the Map
  iframe (`tmp/.../map.png`).
- **Decision Matrix**: evaluates Document History against three conditions to choose a
  research route. Result lands in `overview.json` as `meta.sop_path` plus a
  `decision_matrix` section with human-readable reasoning:
  - **`direct`** — both vesting deed AND SURV row present → direct extraction.
  - **`alternate_partial`** — rows present, missing survey or deed → partial-history search.
  - **`alternate_empty`** — empty + `No documents found.` text → owner-name search.
  - **`unroutable`** — 0 rows without the empty-state message → UI error (per
    tie-breaker rule #3); retry the run.
- **Direct extraction** (implemented): runs when `path == "direct"`.
  Picks the most-recent SURV row as the ALTA + the most-recent WD/SWD/QCD/GEN
  as the vesting deed, then downloads each as a single complete PDF via Tyler's
  `#printCustom` endpoint. Files land at `tmp/{county}/{account}/{role}_{reception}.pdf`.
  Requires `WELD_RECORDER_USERNAME` / `WELD_RECORDER_PASSWORD` in `.env`.
  The cross-reference walk (implemented — see below) then reads every downloaded
  document, starting with the ALTA, for the documents it cites and downloads each
  as `exception_{reception}.pdf`; see [Reading the ALTA](#reading-the-alta-schedule-b-2).
  The S/T/R easement/ROW Advanced Search below also runs unconditionally after
  direct extraction — an ALTA's Schedule B-2 only lists what its surveyor happened
  to cite, not necessarily everything else recorded against the section.
- **Partial-history search** (implemented): runs when
  `path == "alternate_partial"` (rows present but missing survey or deed).
  Downloads the most-recent vesting deed (if any), then drives the Advanced
  Search at `/web/search/DOCSEARCH524S12` with the parcel's S/T/R (and
  Subdivision name if known), filters results to easement / ROW types, and
  downloads each. Cross-reference harvesting from the vesting deed's Exhibit A
  is not implemented — would require OCR since Tyler PDFs are scanned images.
- **Owner-name search** (implemented): runs when `path == "alternate_empty"`
  (empty Document History + "No documents found." text). Searches the
  Advanced Search form's "Search Name as Grantor or Grantee" field for the
  owner's most recent vesting deed and any affidavits, then runs the same
  S/T/R Advanced Search filtered to subdivision-exemption types, the
  easement/ROW types above, and finally a SURVEY/ALTA fallback search.
- **Full section/township/range document scan** (implemented): runs unconditionally
  after every route above (and after the cross-reference walk), via an unfiltered
  `_run_advanced_search()` on the same S/T/R — no doc-type filter this time — so
  anything else recorded against the section that neither the property's own history
  nor cross-reference harvesting surfaced still gets downloaded. Deduplicated against
  the whole run's `known_receptions`; results land in `overview.json` under
  `section_township_range_search`, separate from the route's own results section.
- **GLO original survey of record** and **county + state road right-of-way** — not yet
  implemented. Both would run independently of which research route fired; see
  [docs/weld_county_sop.md](docs/weld_county_sop.md#phase-4--glo-original-survey-of-record).

Document type codes (`SURV`, `WD`, `SWD`, `QCD`, `EASE`, `ROW`, etc.) and the
Decision Matrix routing are defined in the SOP and mirrored in
[scrapers/weld_county.py](apps/worker/survey_art/scrapers/weld_county.py).

### Reading the ALTA (Schedule B-2)

An ALTA's Schedule B-2 lists every easement, right-of-way and prior deed burdening
the parcel, each with a reception number — documents that do *not* appear in the
parcel's own Document History, so nothing earlier in the pipeline can find them.
[`id_extraction.py`](apps/worker/survey_art/id_extraction.py) reads a document off disk
and returns the IDs it cites, cheapest path first:

1. **Text layer** — `pypdf` plus a labelled regex (`RECORDING NO: 1766550`,
   `BOOK 999 AT PAGE 426`). Free and exact, but only born-digital PDFs have one.
2. **Bedrock** — a scanned PDF has no text at all. A 36"x24" survey sheet holds far
   too much fine print to survive being squeezed into one model-sized image, so each
   page is split into overlapping tiles that stay legible (30 for a full-size sheet)
   and sent as images with a forced tool call. Roughly `$0.30` per 5-sheet ALTA on
   Haiku 4.5.

Measured on Weld ALTA 4571638 (account R1611986, 78 title-commitment items citing 81
distinct reception numbers): **79/81 found, 2 missed**. Tile resolution is the whole
ballgame — at a third of the tile count the same sheet scored 20/36 with 11 digit
transpositions. `_TILE_MAX_NATIVE_PX` in `id_extraction.py` carries the scores; re-run
them before raising it.

A misread reception usually 404s and disappears, but it can also fetch a real *wrong*
document. Two did in that run. Treat `extracted_ids` as a strong lead list, not a
verified index — a surveyor should still eyeball the exception PDFs against Schedule B-2.

**Runtime.** This step turns a Weld run from two downloads into up to ~90, and the
recorder starts refusing requests after roughly 40 in a row (a non-200 from the print
endpoint, or a viewer page with no download button). `_download_documents()` paces each
worker, re-asserts the disclaimer cookie, and retries every failure.

Both halves of that work run concurrently rather than one document at a time —
`WELD_DOWNLOAD_CONCURRENCY` fetches (default 4), and the whole of a citation level read
for IDs at once — so expect a commercial ALTA in roughly **5-10 minutes** rather than the
hour it took serially. None of the Weld recorder's PDFs carry a text layer, so all ~90
documents take the Bedrock path; that's ~340 model calls per property, well inside the
account's 10,000-requests-per-minute Haiku quota but the reason the concurrency matters.
Set `APPLICATION_MODE=demo` to stop after the first 10 — enough to show the step working
without the wait.

Every ID found is written to `overview.json` under `extracted_ids` (and so shows up
in the UI's Property Metadata tab) with its type, context, and which downloaded
document cited it (`source_reception`/`source_doc_type`), demo mode included —
the cap applies to downloading, never to what gets recorded. Only reception numbers
are auto-downloaded — the recorder's document URL takes nothing else — so book/page
and other formats are recorded for the surveyor to pull by hand.

**Not just the ALTA.** `_expand_cross_references()` (`scrapers/weld_county.py`) runs
this same extraction on *every* document the scraper downloads, for every routing
path, then fetches whatever new documents turn up and reads those too — recursively,
until nothing new is found. A `known_receptions` set (never re-download the same
reception) and an internal `extracted` set (never re-run extraction on the same
document, even if two other documents both cite it) keep this from doing wasted work
or looping on a citation cycle; `_MAX_CROSS_REFERENCE_DOCS` is a cost/runtime backstop
on top of that, not something normal runs should ever hit.

**Cost, and the cache that cuts it.** Reading those ~90 documents is ~98% of what a run
costs — about **$2 per property** at Haiku 4.5 rates, against roughly 4 cents for
everything else combined. So `_extract_cited_ids()` caches each result in S3 under
`extractions/`, keyed by reception number: a recorded document is immutable, so what it
cites never changes and the answer is reusable indefinitely. Two common cases go to
near-zero Bedrock cost as a result:

- **Re-running a property** (the Reprocess button) — every document is already read.
- **Another parcel in the same section** — the section-wide recorder searches return the
  same easements, plats and rights-of-way for every parcel in that section.

The cache key includes the model and the tile geometry, so re-tuning either starts a
fresh namespace rather than serving results the old settings produced. Cache misses are
silent by design (an unreachable cache must cost money, not correctness) — which also
means a missing `s3:GetObject` grant on the worker's task role shows up only as "every
run costs full price".

The Run Details tab shows the full per-service breakdown for a run — Bedrock, Fargate,
S3, DynamoDB, API Gateway/Lambda/SQS, CloudWatch Logs — with a total. Only the Bedrock
line is a billed figure (the provider's own usage accounting); the rest are estimated
from measured quantities against published us-west-2 on-demand rates, since AWS cost
reports lag 24-48h. See `apps/worker/survey_art/costs.py` for the rates and assumptions.

> **Bedrock model access:** Anthropic models on Bedrock need the *Anthropic use case
> details* form submitted once per account (Bedrock console → Model access). Until
> then every call fails with `ResourceNotFoundException: Model use case details have
> not been submitted`, and extraction returns nothing rather than failing the scrape —
> the ALTA and vesting deed still download normally.

---

## Configuration (`.env`)

Create `.env` in the project root (gitignored, see [`.env.example`](.env.example)).
Minimum for Weld:

```bash
# Optional — Weld doesn't use the LLM, but other counties do (AWS Bedrock, IAM auth):
MODEL=us.anthropic.claude-haiku-4-5-20251001-v1:0

# Optional — only needed for Weld Phase 3 document downloads:
WELD_RECORDER_USERNAME=your_email@example.com
WELD_RECORDER_PASSWORD=your_password
```

`make up`/`make process` read this file too — docker-compose loads `.env` from the
project root automatically to fill in the `${VAR}` substitutions in `docker-compose.yml`.

---

## How it works (Weld)

```
Query (address / account / parcel / owner / S-T-R)
   │
   ▼ _resolve_parcel() — priority routing
Parcel discovery
   ├── HTTP path: POST apps.weld.gov/propertyportal/index.cfm  (default)
   └── Browser path: literal SOP walk (--sop-strict)
   │
   ▼ ParcelInfo (Owner, Account, Parcel, S-T-R, …)
   ▼ → overview.json: identify_results
Property Report
   │  GET propertyreport.weld.gov/?account=R…
   ▼  parse every accordion section in one HTTP response, incl. Document History
   ▼ → overview.json: account_information, owners, valuation, tax, document_history
Map capture
   │  Playwright renders maps.weld.gov/mapanaccount/?Account=R…
   ▼  screenshot ESRI map with parcel boundary highlighted
   ▼ → tmp/.../map.png  +  overview.json: map.image_path
Decision Matrix (_decision_matrix()) — branches on Document History
   │
   ├── direct: SURV row + vesting deed both present
   │      Playwright downloads both via recording.weld.gov, then reads the ALTA's
   │      Schedule B-2 (id_extraction.py) and fetches every referenced exception too,
   │      then also runs the S/T/R easement/ROW Advanced Search below
   │      ▼ → tmp/.../{role}_{reception}.pdf  +  overview.json: direct_extraction,
   │            extracted_ids, easement_and_row_search
   │
   ├── alternate_partial: rows present, missing the deed and/or survey
   │      downloads the vesting deed (if any), then drives recorder Advanced Search
   │      by S/T/R (+ subdivision) filtered to easement/ROW document types
   │      ▼ → tmp/.../{role}_{reception}.pdf  +  overview.json: partial_history_search
   │
   └── alternate_empty: no rows, "No documents found."
          searches by owner name for the vesting deed, then the same S/T/R
          Advanced Search filtered to subdivision-exemption, easement/ROW, and
          finally a SURVEY/ALTA fallback
          ▼ → tmp/.../{role}_{reception}.pdf  +  overview.json: owner_name_search
```

All document downloads inject a `disclaimerAccepted=true` cookie (bypasses the
reCAPTCHA-gated disclaimer button) and require an authenticated session — see
[`docs/weld_county_sop.md`](docs/weld_county_sop.md) for the full decision tree and
what's not yet automated (GLO survey-of-record, road ROW).

For Denver / Jefferson / Arapahoe the architecture differs — see each scraper file
under `apps/worker/survey_art/scrapers/`.

---

## Adding a new county

1. Add an entry to `SUPPORTED_COUNTIES` in
   [county_sites.py](apps/worker/survey_art/county_sites.py) with the county name,
   state, scraper key, and relevant URLs.
2. Create `scrapers/{state}_{county}.py` exposing:

   ```python
   async def scrape(
       geocoded: GeocodedAddress,
       tmp_dir: Path,
       doc_filter: DocumentFilter = DEFAULT_FILTER,
       **kwargs,
   ) -> tuple[list[Path], str | None, float, int, int]: ...
   ```

3. Register it in `COUNTY_SCRAPERS` in [pipeline.py](apps/worker/survey_art/pipeline.py).
4. Add tests in `tests/test_{county}_scraper.py`.

---

## Development

```bash
uv run pytest --cov=src tests/    # tests with coverage
uv run ruff check src tests       # lint
uv run ruff format src tests      # format
make build                        # rebuild Docker image after dependency changes
```

`AGENTS.md` (which `CLAUDE.md` symlinks to) is the developer/agent guide. Keep it
in sync with reality — agents read it before touching the code.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Phase 1 failed: could not resolve … to a Weld parcel` | Address not in Weld portal index | Try the account number directly, or use `--owner` |
| `WELD_RECORDER_USERNAME/PASSWORD not set` (Phase 3) | Anonymous access blocked at `recording.weld.gov` | Register at `recording.weld.gov` (free) and set env vars |
| `Disclaimer accept failed: Timeout … #submitDisclaimerAccept` | Old binary — cookie-injection fix not picked up | Pull latest; the scraper now bypasses the reCAPTCHA-gated button |
| `No #ImageDiv found` for every reception | Disclaimer cookie not set OR not registered | Run Phase 3 with credentials set in `.env` |
| Map.png shows tiles but no red boundary | ESRI selection layer didn't render in time | The 15s wait is usually enough; rerun |
| `pdftoppm is not installed` (when reading PDFs) | poppler missing | `brew install poppler` |
