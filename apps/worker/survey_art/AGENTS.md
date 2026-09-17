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
   never internal jargon — no code identifiers, doc-phase labels, or query parameters a
   surveyor didn't type themselves).
3. **The two streams aren't just conceptually separate — they're tagged.**
   `_DynamoLogHandler.emit()` in `worker.py` writes each record as a `LogEntry`
   (`survey_shared.jobs.LogEntry`) with `kind="milestone"` for `survey_art.narration`
   records and `kind="detail"` for everything else. The frontend's Logs tab renders
   `milestone` entries as the primary step-by-step progress view and folds `detail`
   entries into a "show technical log" toggle — so a `logger.info(...)` you add to a
   scraper is *still safe to leave verbose/technical*, since it won't clutter the
   non-technical view by default. It's still visible on request, though, so keep it
   truthful and free of anything sensitive.

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
- `_archive_full_log(job_id)` — called from `run_job()`'s `finally` block, after
  `log_handler.close()` has blocked until the `QueueListener` flushes every queued
  record to DynamoDB, so it reads the job back (`jobs.get_job()`) rather than tracking
  entries locally and always sees the complete log for the run. Uploads a header
  (address/county/status/error) plus every `LogEntry`, both kinds, via
  `jobs.upload_job_log()` (see
  [`packages/survey_shared/AGENTS.md`](../../../packages/survey_shared/AGENTS.md)).
  Never raises — a failed archive upload must not fail a job that's already finished.

## Estimated cost & run time (`worker.py`, frontend's Run Details tab)

Every job records a cost/runtime breakdown, written by `run_job()` via
`jobs.update_status()` on every exit path (`COMPLETED`, `FAILED`, and the generic
`except` — a crashed job still burned real Bedrock tokens and real Fargate seconds, so
it still gets a number). Two independent inputs, one real and one estimated:

- **Bedrock cost — a real dollar figure.** `run_async()` (`pipeline.py`) returns
  `(saved, err, cost, in_tok, out_tok)` all the way up from whichever scraper ran —
  every county scraper's `scrape()` sums `llm.agent_cost()` (browser-use's own
  `agent.history.usage`, real per-call Bedrock pricing) across every LLM call it makes,
  plus, for Weld, `id_extraction.py`'s raw `bedrock-runtime.converse()` token counts for
  cross-reference ID extraction. This number needs no maintenance when infra changes —
  it comes from the model provider's own usage accounting, not a local estimate.
- **Fargate cost — an estimate, not billed usage.** There is deliberately no AWS Cost
  Explorer/CUR integration here: tag-based cost allocation reports lag 24-48h, which
  can't back a same-run UI. Instead, `_cost_fields()` in `worker.py` takes the task's own
  wall-clock runtime (`time.time()` at the top of `run_job()` to the moment the job
  reaches a terminal state) and multiplies by a **hardcoded flat on-demand rate**:

  ```python
  _FARGATE_VCPU_HOUR_USD = 0.04048   # us-west-2, Linux/x86, on-demand
  _FARGATE_GB_HOUR_USD = 0.004445    # us-west-2, Linux/x86, on-demand
  _FARGATE_VCPUS = 1                 # WorkerTaskDefinition: Cpu: '1024'
  _FARGATE_MEM_GB = 2                # WorkerTaskDefinition: Memory: '2048'
  ```

  **If you change the worker's Fargate sizing or region, update these four constants to
  match** — nothing re-derives them automatically:
  - `WorkerTaskDefinition.Cpu`/`Memory` in `infra/cloudformation/backend.yaml` changes →
    update `_FARGATE_VCPUS`/`_FARGATE_MEM_GB` (they're `Cpu`/1024 and `Memory`/1024).
  - Deploying somewhere other than `us-west-2` (`infra/deploy.py`'s `DEFAULT_REGION`) →
    look up that region's Fargate on-demand rate and update
    `_FARGATE_VCPU_HOUR_USD`/`_FARGATE_GB_HOUR_USD` (AWS Pricing page, "Fargate", Linux/x86).
  - Switching to Fargate Spot, ARM/Graviton, or a different launch type entirely → these
    four constants aren't enough on their own; revisit `_cost_fields()`'s formula, not
    just the numbers.

  `Job.fargate_seconds`/`Job.fargate_cost_usd` (`packages/survey_shared/survey_shared/jobs.py`)
  round-trip these once computed; there's no separate "recompute later" path, so a rate
  change only affects jobs run after the deploy — historical job records keep whatever
  rate was in effect when they ran.

- **Run time.** The frontend derives everything else from three fields the job record
  already carries — no separate timing plumbing: `fargateSeconds` (the task's own
  wall-clock runtime, same number the Fargate estimate above is built from — the
  closest thing to "how long the scrape actually took"), and `createdAt`/`updatedAt`
  (set by `jobs.create_job()`/`update_status()`). `createdAt` is stamped when the API
  creates the `PENDING` record, before the job is even dispatched to Fargate, so
  `updatedAt - createdAt` includes SQS/dispatcher/task-start latency that
  `fargateSeconds` doesn't — the UI shows both plus the difference as "time waiting to
  start," and "average time per document" as `fargateSeconds / fileCount`.

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

## `extracted_ids` — cross-reference expansion, not just the ALTA

`_expand_cross_references()` in `scrapers/weld_county.py` reads **every** document the
scraper downloads — not only the ALTA — for the other documents it cites
(`id_extraction.extract_document_ids()`), fetches those too, and repeats on the newly
downloaded ones until nothing new turns up. Two sets keep this from doing wasted work:
`known_receptions` (already downloaded or queued this run — never fetched twice) and an
internal `extracted` set (already read for citations — never sent through
`extract_document_ids()`, and its Bedrock fallback, twice, even if two different
documents both cite it). Real recorder data is a finite graph, so this terminates on its
own; `_MAX_CROSS_REFERENCE_DOCS` is just a cost/runtime backstop for a pathological case.

Every ID found — from any document, not just the ALTA — lands in `overview.json` as
`extracted_ids`, one flat list of `{id, id_type, context, raw, source_reception,
source_doc_type}` rows (the last two say which downloaded document cited it), written
once per document processed so a crash mid-walk still leaves everything found so far.
The frontend renders it as a table for free (see `MetadataValue` in
[`apps/web/AGENTS.md`](../../../apps/web/AGENTS.md)) — it only reads the array shape, so
adding `source_reception`/`source_doc_type` columns didn't require a frontend change.

Keep **every** ID in that section, not just the fetchable ones. Only
`id_type == "reception_number"` becomes a download (the recorder's integration URL takes
nothing else), but a book/page or an unparseable reference is still something a surveyor
needs to chase by hand, so `_select_schedule_b2_exception_targets()` filters the *download
targets*, never the stored metadata. `APPLICATION_MODE=demo` (`_DEMO_EXCEPTION_LIMIT`)
truncates those targets on each document processed for the same reason — a demo shouldn't
wait out ~90 downloads, but it should still show the surveyor everything cited.

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
