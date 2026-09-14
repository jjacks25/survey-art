# apps/worker/survey_art/ — core scraper + worker entrypoint

The `survey-art` package: the county scrapers (`scrapers/`), the CLI (`__main__.py`),
and `worker.py` — the entrypoint that runs a scrape as an AWS job (Fargate task
one-shot, or a local SQS poll loop; see `worker.py`'s module docstring). This package
has the heavy deps (Playwright/browser-use/Crawl4AI) — it is **not** installed into the
API's Lambda image (see [`apps/api/AGENTS.md`](../../../apps/api/AGENTS.md)).

## Two log streams: developer diagnostics vs. end-user narration

Every module here logs to `logging.getLogger(__name__)` (e.g. `survey_art.scrapers.weld_county`)
as before — that's unchanged and still just goes to stderr/CloudWatch for developers.

`worker.py` additionally attaches a streaming handler to the whole `survey_art` logger
tree, which mirrors every record into the job's `logs` field in DynamoDB (see
`Job.logs` in [`packages/survey_shared/AGENTS.md`](../../../packages/survey_shared/AGENTS.md))
so the frontend's Logs tab can show live progress. Two things follow from that:

1. **Anything logged anywhere under `survey_art.*` streams to the end user.** Keep that
   in mind before adding a `logger.info(...)` deep in a scraper — it's not just going to
   your terminal anymore, it's going to a non-technical surveyor watching a progress
   feed.
2. **For genuinely user-facing narration, use the separate `survey_art.narration`
   logger**, not the module's own `logger`. It's a child logger (so it still propagates
   to the same streaming handler — nothing extra to wire up), but keeping it separate
   means the plain-English milestones ("Reading the county's property report page for
   ownership and deed history...") don't get mixed into the same code path as the
   detailed SOP/phase diagnostics developers rely on for debugging. Narration calls are
   purely additive — never remove or repurpose an existing `logger.*` call to "become"
   a narration line; add a new `narration.info(...)` alongside it instead. See the
   narration calls already threaded through `scrapers/weld_county.py` for the pattern
   and tone to match (state what's happening/what was found/decided, in plain language,
   not internal jargon like "decision matrix" or "Path 3C").

## Why log streaming uses `QueueHandler`/`QueueListener`, not a plain handler

`_JobLogHandler` (a `logging.handlers.QueueHandler`) is deliberately *not* a plain
`logging.Handler` subclass that writes to DynamoDB directly in `emit()`. An earlier
version did exactly that, and it caused a real, hard-to-diagnose regression: `emit()`
runs synchronously on the calling thread, and the scraper's `logger.info(...)` calls
happen from inside async scraper code — so a blocking DynamoDB network round-trip on
every single log line (~20-40 per scrape) was stalling the asyncio event loop, which
was subtle enough to perturb Playwright's timing-sensitive `wait_for_load_state`/
`wait_for_function` calls and produce flaky, seemingly-unrelated scrape failures
("unroutable" decision-matrix errors, missing documents that had worked moments
before).

The fix: `_JobLogHandler.emit()` only enqueues onto an in-memory `queue.Queue` (fast,
non-blocking); a background thread (the `QueueListener`, started in `__init__`) drains
it and calls the actual blocking `jobs.append_log()` via `_DynamoLogHandler`. **If you
ever add another handler to the scraper's logger that talks to a network service, follow
this same pattern** — never let a handler attached to a logger the scraper actively uses
perform blocking I/O directly in `emit()`.

## `worker.py` — job lifecycle helpers

- `_load_overview(tmp)` — best-effort load of `overview.json` (see `overview.py` and
  the Weld scraper) as the job's `metadata`. Returns `None` if the scraper that ran
  doesn't write one (only Weld does today) or the file is corrupt — never raises.
- `_resolve_location(address, metadata)` — best-effort lat/lon for the Map tab. Tries
  geocoding the scraper's *resolved* property address first (`metadata.identify_results.address`,
  more precise, and the only option when the user searched by account/parcel number
  rather than a street address), then falls back to the original input address. Returns
  `None` (not an exception) on any geocoding failure — the map pin is a nice-to-have,
  never something that should fail a job.
- `_upload_map_image(job_id, metadata)` — if the scraper captured a map screenshot
  (`metadata["map"]["image_path"]`, a local temp-dir path), uploads it via
  `jobs.upload_map_image()` and replaces `image_path` with a browser-reachable
  `image_url` in the metadata that gets persisted — the local path is meaningless
  outside the (ephemeral, per-job) worker container, so it's popped rather than kept
  alongside the URL.
- `_doc_prefix(tmp, saved, input_address, metadata)` — computes the
  `{state}/{county}/{identifier}` prefix documents are uploaded under (see
  [`packages/survey_shared/AGENTS.md`](../../../packages/survey_shared/AGENTS.md)). County
  comes from the local save path (`tmp/{county_key}/...`, more reliable than the job's
  `county` param, which may be unset when auto-detected); the identifier falls back
  through resolved address → input → account → legal description → section/township/
  range. Called *before* `jobs.upload_documents()`, since the prefix has to exist first.

## Metadata: cleaned sections vs. `raw_report_fields`

`weld_county.py`'s `_group_report_fields()` sorts the property report's flat field dict
into named sections (`account_information`, `owners`, `land_information`,
`valuation_information`, `tax_authorities`) per `_REPORT_SECTION_MAP`, and
deliberately **does not** duplicate a field across sections or repeat identity fields
(`account`, `parcel`, `subdivision`, `section`, `township`, `range`) that are already
captured once, in SOP form, under `identify_results` — see `_IDENTIFY_RESULTS_LABELS`.
The `legal` field is renamed to `legal_description` on the way in (`_FIELD_RENAMES`) so
it reads clearly in the UI.

Separately, `scrape()` also stores the complete, unsectioned, undeduplicated field dict
as `raw_report_fields` — collect everything the county published, even if today's
grouping doesn't have a clean home for it. The frontend deliberately does not render this
section (see `RAW_SECTION_KEYS` in `apps/web/src/App.tsx`) — it's for completeness/future
use, not the curated view a surveyor reads. If you add a new grouped section or rename a
field, only touch the grouped path — `raw_report_fields` should stay a verbatim dump.

## `extracted_ids` — what the ALTA points at

SOP Step 3A.5. After the ALTA downloads, `id_extraction.extract_document_ids()` reads it
for the records it cites and the scraper writes them to `overview.json` as
`extracted_ids` — a list of `{id, id_type, context, raw}` rows, which the frontend
renders as a table for free (see `MetadataValue` in
[`apps/web/AGENTS.md`](../../../apps/web/AGENTS.md)).

Keep **every** ID in that section, not just the fetchable ones. Only
`id_type == "reception_number"` becomes a download (the recorder's integration URL takes
nothing else), but a book/page or an unparseable reference is still something a surveyor
needs to chase by hand, so `_select_phase_3a_exception_targets()` filters the *download
targets*, never the stored metadata. `APPLICATION_MODE=demo` (`_DEMO_EXCEPTION_LIMIT`)
truncates those targets for the same reason — a demo shouldn't wait out ~90 downloads,
but it should still show the surveyor everything the ALTA cites.

`extract_document_ids()` also never raises — it enriches a document that already
downloaded, so a Bedrock outage must leave the run otherwise intact.

## Adding metadata/map support to another county

Nothing scraper-agnostic needs to change — `_load_overview`/`_upload_map_image` just
look for keys that happen to exist. To light up Property Metadata / Map for another
county, have its scraper write an `overview.json` (any structure — the frontend renders
whatever's there generically, see `MetadataView` in
[`apps/web/AGENTS.md`](../../../apps/web/AGENTS.md)) and, if you want a Map tab image
rather than just an address-based Google Maps fallback, capture a screenshot the same
way `_capture_map_image()` does in `scrapers/weld_county.py` and set
`overview["map"] = {"image_path": ..., "iframe_url": ...}`.
