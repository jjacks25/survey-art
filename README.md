# Land Survey Scraper

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
uv run land-survey-scraper "R1611986" --county CO_weld
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
uv run land-survey-scraper "1234 Main St, Greeley, CO 80631"

# 2. By account number — bypasses geocoding, fastest for Weld
uv run land-survey-scraper "R1611986" --county CO_weld

# 3. By parcel ID
uv run land-survey-scraper "095715000012" --county CO_weld

# 4. By owner name (SOP "Priority 4 — last resort")
uv run land-survey-scraper "any address" --county CO_weld --owner "STRATUS DELANTERO"

# 5. By Section / Township / Range (PLSS) — requires --sop-strict (browser walk)
uv run land-survey-scraper "any address" --county CO_weld \
    --str "15,5N,67W" --sop-strict

# 6. SOP-strict mode — literal Playwright walk through the GIS Hub UI
#    (slower; useful for demos or when the HTTP shortcut fails)
uv run land-survey-scraper "R1611986" --county CO_weld --sop-strict

# 7. Watch the browser drive itself (only useful with --sop-strict or Phase 3)
WELD_HEADED=1 uv run land-survey-scraper "R1611986" --county CO_weld --sop-strict

# 8. Custom output directory
uv run land-survey-scraper "R1611986" --county CO_weld -t ./output

# 9. Re-download files that already exist
uv run land-survey-scraper "R1611986" --county CO_weld --no-skip-existing

# 10. Suppress progress UI (CI-friendly)
uv run land-survey-scraper "R1611986" --county CO_weld --quiet
```

### Docker

Recommended for reproducibility and CI — no host Python/Chromium needed.

```bash
make build                                                # build the image (one time)
make process ADDRESS="R1611986" ARGS="--county CO_weld"   # run end-to-end
make shell                                                # interactive bash inside the image
make dev                                                  # bash with local src/ mounted (live edits)
make down                                                 # stop a backgrounded container
```

Pass extra flags via `ARGS="..."`:

```bash
make process ADDRESS="R1611986" ARGS="--county CO_weld --sop-strict"
make process ADDRESS="STRATUS DELANTERO" ARGS="--county CO_weld --owner 'STRATUS DELANTERO LLC'"
```

> **Docker limitation:** `WELD_HEADED=1` does nothing inside the container — there's no
> display. Use local `uv run` to watch the browser.

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
| `MODEL` | Yes for non-Weld counties | — | LLM model id (e.g. `claude-haiku-4-5`) |
| `LLM_PROVIDER` | No | auto-detect | One of `nvidia`, `openrouter`, `anthropic`, `openai` |
| `ANTHROPIC_API_KEY` | If using Anthropic | — | Claude API key |
| `OPENROUTER_API_KEY` | If using OpenRouter | — | OpenRouter key (free tier available) |
| `NVIDIA_API_KEY` | If using NVIDIA NIM | — | Free tier at build.nvidia.com |
| `OPENAI_API_KEY` | If using OpenAI | — | OpenAI key |
| `WELD_RECORDER_USERNAME` | Optional (Weld Phase 3) | — | Login for `recording.weld.gov` |
| `WELD_RECORDER_PASSWORD` | Optional (Weld Phase 3) | — | Login for `recording.weld.gov` |
| `WELD_HEADED` | No | `0` | Set to `1` to show the Playwright browser window |

**No LLM is invoked for Weld today.** Phases 1, 2, and 3 are pure HTTP + Playwright with
hard-coded selectors. The LLM env vars are only used by the Denver / Jefferson / Arapahoe
scrapers.

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
  "account_information": { "Account": "...", "Parcel": "...", "Acres": "...", ... },
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
    "sop_letter": "A",
    "reasoning": ["Condition A matched: ...", "Most recent SURV: ...", "→ Route to Phase 3A."],
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
  "raw_property_report_fields": { /* every label parsed from propertyreport.weld.gov, verbatim */ }
}
```

The schema is open: downstream consumers can read `Overview` via the
[overview.py](src/land_survey_scraper/overview.py) helper or just parse JSON.

---

## Supported counties

| County | State | Scraper key | Approach | Status |
|---|---|---|---|---|
| Weld | CO | `CO_weld` | Pure HTTP + Playwright (no LLM) | Phase 1 + 2 fully working; Phase 3 needs registered account |
| Denver | CO | `CO_denver` | browser-use + LLM | Works; requires `CO_DENVER_USERNAME/PASSWORD` |
| Jefferson | CO | `CO_jefferson` | Hybrid REST API + browser-use | Works (no auth) |
| Arapahoe | CO | `CO_arapahoe` | browser-use + LLM | Proof-of-concept |

---

## Weld County — the SOP

The Weld scraper implements the procedure documented in
**[docs/weld_county_sop.md](docs/weld_county_sop.md)**. That doc is the source of truth —
read it before modifying scraper logic. Key points:

- **Phase 1** (parcel discovery): SOP Steps 1.1–1.5 yield an Identify Results panel
  with Owner / Account / Parcel / Address / Subdivision / S-T-R. Steps 1.6 + 1.7 capture
  all Property Report accordion sections (server-rendered) and a screenshot of the Map
  iframe (`tmp/.../map.png`).
- **Phase 2** (Decision Matrix): evaluates Conditions A/B/C against the Document History
  to choose a routing path. Result lands in `overview.json` as `meta.sop_path` plus a
  `decision_matrix` section with human-readable reasoning. Each path has a descriptive
  label (used in code) and a SOP letter (kept for cross-reference with the surveyor doc):
  - **`direct`** (SOP A) — both vesting deed AND SURV row → direct extraction in Phase 3A.
  - **`alternate_partial`** (SOP B) — rows present, missing survey or deed → S/T/R Advanced Search in 3B.
  - **`alternate_empty`** (SOP C) — empty + `No documents found.` text → owner-name search in 3C.
  - **`unroutable`** — 0 rows without the empty-state message → UI error (per
    tie-breaker rule #3); retry the run.
- **Phase 3A** (Direct Extraction — implemented): runs when `path == "direct"`.
  Picks the most-recent SURV row as the ALTA + the most-recent WD/SWD/QCD/GEN
  as the vesting deed, then downloads each as a single complete PDF via Tyler's
  `#printCustom` endpoint. Files land at `tmp/{county}/{account}/{role}_{reception}.pdf`.
  Requires `WELD_RECORDER_USERNAME` / `WELD_RECORDER_PASSWORD` in `.env`. Step
  3A.5 (Schedule B-2 exception walk) is not yet implemented.
- **Phase 3B** (Alternative Research 1 — implemented): runs when
  `path == "alternate_partial"` (rows present but missing survey or deed).
  Downloads the most-recent vesting deed (if any), then drives the Advanced
  Search at `/web/search/DOCSEARCH524S12` with the parcel's S/T/R (and
  Subdivision name if known), filters results to easement / ROW types, and
  downloads each. Step 3B.3 (Exhibit A cross-reference harvest) is not
  implemented — would require OCR since Tyler PDFs are scanned images.
- **Phase 3C** (Alternative Research 2 — not yet implemented): would run for
  `alternate_empty` (empty Document History + "No documents found." text).

Document type codes (`SURV`, `WD`, `SWD`, `QCD`, `EASE`, `ROW`, etc.) and the
Decision Matrix routing are defined in the SOP and mirrored in
[scrapers/weld_county.py](src/land_survey_scraper/scrapers/weld_county.py).

---

## Configuration (`.env`)

Create `.env` in the project root (gitignored). Minimum for Weld:

```bash
# Optional — Weld doesn't use the LLM, but other counties do:
ANTHROPIC_API_KEY=sk-ant-...
MODEL=claude-haiku-4-5

# Optional — only needed for Weld Phase 3 document downloads:
WELD_RECORDER_USERNAME=your_email@example.com
WELD_RECORDER_PASSWORD=your_password
```

Docker reads this same file via `--env-file .env`.

---

## How it works (Weld)

```
Query (address / account / parcel / owner / S-T-R)
   │
   ▼ _resolve_parcel() — SOP Priority routing
Phase 1: Parcel discovery
   ├── HTTP path: POST apps.weld.gov/propertyportal/index.cfm  (default)
   └── Browser path: literal SOP walk (--sop-strict)
   │
   ▼ ParcelInfo (Owner, Account, Parcel, S-T-R, …)
   ▼ → overview.json: identify_results
Phase 1.6: Property Report
   │  GET propertyreport.weld.gov/?account=R…
   ▼  parse every accordion section in one HTTP response
   ▼ → overview.json: account_information, owners, valuation, tax, …
Phase 1.7: Map accordion
   │  Playwright renders maps.weld.gov/mapanaccount/?Account=R…
   ▼  screenshot ESRI map with parcel boundary highlighted
   ▼ → tmp/.../map.png  +  overview.json: map.image_path
Phase 2: Document History
   │  parse Document History rows from property report HTML
   ▼ → overview.json: document_history, survey_documents
Phase 3: Document Download
   │  Playwright iterates recording.weld.gov/web/web/integration/document/{id}
   │  injects disclaimerAccepted cookie  (bypasses reCAPTCHA-gated button)
   │  authenticated session (if credentials set) → captures image responses
   ▼ → tmp/.../reception_{id}.{ext}
   ▼ → overview.json: survey_documents[].download_status
```

For Denver / Jefferson / Arapahoe the architecture differs — see each scraper file
under `src/land_survey_scraper/scrapers/`.

---

## Adding a new county

1. Add an entry to `SUPPORTED_COUNTIES` in
   [county_sites.py](src/land_survey_scraper/county_sites.py) with the county name,
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

3. Register it in `COUNTY_SCRAPERS` in [pipeline.py](src/land_survey_scraper/pipeline.py).
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
