# Land Survey Scraper

Automated land survey research tool for Colorado counties. Given a street address, the tool
navigates county government websites to locate and download all relevant property records —
deeds, plats, surveys, and easements — that a surveyor would normally gather manually.

## Prerequisites

| Requirement | Version | Install |
|-------------|---------|---------|
| [Python](https://www.python.org/) | 3.13+ | `brew install python` or pyenv |
| [uv](https://docs.astral.sh/uv/) | latest | `brew install uv` |
| [Docker](https://www.docker.com/products/docker-desktop/) | latest | Docker Desktop |
| [OpenRouter API key](https://openrouter.ai/) | — | Free account at openrouter.ai |
| Weld County eRecording login | — | Shared surveyor community credentials |

**OpenRouter** is used to run the AI browser agent that navigates county websites. A free
account gives you 1500 requests/day — enough for POC use. Sign up at openrouter.ai, generate
an API key, and add it to your `.env` file.

**`.env` file** — create this in the project root before running anything:

```bash
# LLM (free tier via OpenRouter — recommended for POC)
OPENROUTER_API_KEY=sk-or-...
MODEL=google/gemini-2.0-flash-exp:free

# Weld County eRecording shared surveyor login
WELD_ERECORDING_USERNAME=your_username
WELD_ERECORDING_PASSWORD=your_password
```

> `.env` is gitignored. Never commit it. For Docker, vars are passed in via `--env-file .env`.

---

## How It Works

The tool chains together several steps that mirror how a survey technician researches a project:

```
Address
  │
  ▼ Census Bureau Geocoder
County (e.g. Weld, CO)
  │
  ▼ browser-use (Claude AI agent)
County portal navigation
  • Looks up the parcel by situs address
  • Extracts parcel ID / schedule number / Reception Numbers
  • Logs into authenticated portals (e.g. Weld eRecording)
  • Searches for deeds, plats, surveys, easements by ID and document type
  │
  ▼ Crawl4AI
Document links extracted from result pages
  │
  ▼ httpx (async)
PDFs downloaded in parallel → ./tmp/{county}/{address}/
```

Rather than hard-coded CSS selectors that break when county sites update, the scraper uses
**browser-use** — an open-source LLM browser agent — to describe navigation tasks in natural
language and let Claude figure out how to execute them. This makes the scraper resilient to
layout changes across different county portals.

## Supported Counties

| County | State | Scraper Key | Primary Sources |
|--------|-------|-------------|-----------------|
| Weld | CO | `CO_weld` | Property Portal + eRecording (authenticated) |
| Denver | CO | `CO_denver` | Denver Assessor + Clerk & Recorder |
| Arapahoe | CO | `CO_arapahoe` | Arapahoe Assessor + Recorder |
| Jefferson | CO | `CO_jefferson` | Jeffco Records Search |

## Setup

Once prerequisites are in place (`.env` created, Docker running):

```bash
uv sync
uv run playwright install chromium   # installs the Chromium browser for local runs
```

## Usage

```bash
# Recommended: run via Docker (handles all deps including Chromium)
make process ADDRESS="1234 Main St, Greeley, CO 80631"

# Or run locally with uv
uv run land-survey-scraper "1234 Main St, Greeley, CO 80631"

# Force a specific county (skip geocoding — useful for testing)
uv run land-survey-scraper "1234 Main St" --county CO_weld

# Save to a custom directory
uv run land-survey-scraper "1234 Main St, Lakewood, CO 80215" -t ./output
```

**Output** is saved to `./tmp/{county_key}/{address_slug}/`. Example:

```
tmp/
  CO_weld/
    1234_main_st_greeley_co_80631/
      warranty_deed_2021.pdf
      land_survey_plat_2019.pdf
      easement_agreement.pdf
```

Existing files are skipped by default. Use `--no-skip-existing` to re-download.

## Docker

```bash
make build          # build image
make dev            # shell with source mounted (live code changes)
make process ADDRESS="123 Main St, Greeley, CO 80631"   # run end-to-end
```

The `process` target builds if needed, passes `.env` into the container, and mounts
`./tmp` so downloaded files land on your host machine.

## Development

```bash
uv run pytest --cov=src tests/   # run tests with coverage
uv run ruff check src tests      # lint
uv run ruff format src tests     # format
```

## Adding a New County

1. **Add the county entry** to `SUPPORTED_COUNTIES` in `src/land_survey_scraper/county_sites.py`:

   ```python
   {
       "state": "CO",
       "county": "Boulder",
       "scraper_key": "CO_boulder",
       "urls": {
           "assessor": "https://www.bouldercounty.gov/property-and-land/assessor/",
           "recorder": "https://www.bouldercounty.gov/property-and-land/recording/",
       },
   }
   ```

2. **Create the scraper** at `src/land_survey_scraper/scrapers/boulder_county.py`.
   Copy an existing county scraper as a template. The only required export is:

   ```python
   async def scrape(
       geocoded: GeocodedAddress,
       tmp_dir: Path,
       pdf_only: bool = False,
   ) -> tuple[list[Path], str | None]: ...
   ```

   Write browser-use agent tasks that describe the navigation in plain English.
   The agent will use Claude to figure out the specifics of each portal.

3. **Register the scraper** in `COUNTY_SCRAPERS` in `src/land_survey_scraper/pipeline.py`:

   ```python
   from land_survey_scraper.scrapers import boulder_county

   COUNTY_SCRAPERS = {
       ...
       "CO_boulder": boulder_county.scrape,
   }
   ```

4. **Add tests** in `tests/test_boulder_county_scraper.py` following the patterns in
   `tests/test_pipeline_dispatch.py`.

## Weld County Workflow (Detail)

Weld is the most complex county — two separate systems must be queried:

1. **Property Portal** (`maps.weld.gov/propertyportal/`) — The agent searches by situs
   address, opens the parcel's property report, and extracts all Reception Numbers from the
   document history. Reception Numbers are Weld County's canonical cross-reference key for
   all recorded documents.

2. **eRecording** (`erecording.weld.gov`) — The agent logs in with the shared surveyor
   community credentials and searches by:
   - Each Reception Number found in step 1
   - Document types: Land Survey Plats, Subdivision Exemptions, ALTA Surveys, Warranty Deeds,
     Quit Claim Deeds

   > Note: Weld County scanned records typically only go back to the 1990s.
