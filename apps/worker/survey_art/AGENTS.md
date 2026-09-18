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
- `_make_thumbnails(saved)` — first-page JPEG thumbnails for every saved PDF, rendered
  in a thread pool (`_TAIL_WORKERS`). Each one decodes a full-page scan raster and a
  property can bring back ~90 of them, so serially this added minutes to the end of a
  job. Pillow releases the GIL for decode/resize, so threads suffice on the task's 1
  vCPU — no process pool. A thumbnail that can't be rendered is skipped, never fatal.
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

## Rendering a tile: the scans are bitonal, so conversion order matters

Every recorder raster in this corpus is PIL mode `"1"` (1-bit black and white),
and **PIL resamples a mode `"1"` image by nearest-neighbour whatever filter you
pass it**. So `_fit()` in `id_extraction.py` — which every downscale in that
module goes through — converts to `"L"` *before*
resizing, not after. Getting that backwards means a 36"x24" sheet downscaled to
0.43x throws away ~57% of its rows and columns with no averaging at all, which
breaks thin strokes and turns 8s into 6s — the digit-transposition failure that
the `_TILE_MAX_NATIVE_PX` comment warns about. It cost ~9% of the references on
a measured sample and produced wrong-but-real reception numbers, which then
fetch the wrong document.

Two consequences worth keeping in mind:

- **Tiles go over the wire as JPEG, not PNG.** An antialiased greyscale scan is
  pathological for PNG (~6x the bytes of the bitonal original), enough to
  overrun a Converse request body. Bedrock prices an image by its dimensions,
  so the encoding is free either way.
- **`make_thumbnail()` has the same hazard** and the same fix, and it matters
  more there — a thumbnail is a ~0.04x downscale, so without averaging almost
  every stroke falls between samples and the result is noise. It caps width
  rather than pixel area, so it converts and then hands off to
  `Image.thumbnail` instead of going through `_fit`.

`tests/test_id_extraction.py::test_downscaling_a_bitonal_scan_antialiases` pins
this. Note it deliberately does **not** count distinct grey levels: JPEG's DCT
invents intermediate values from any input, so that check passes even when the
resampling is broken. It measures what fraction of pixels sit at the extremes
instead (~0% when correct, 100% when not).

## Sideways pages: ask, then read both ways

Weld recorder scans routinely store a landscape sheet in a portrait raster with
the text running vertically, and **no page sets `/Rotate`** — all 333 pages of
the R1611986 corpus report 0 — so nothing in the file declares it. 33 of those
333 pages (9.9%) are turned, and they skew towards the pages that matter:
exhibit tables with a `RECEPTION NUMBER` column. All twelve exhibits of
`exception_2873123.pdf` are sideways.

`_page_turn()` decides, per page, before tiling. Three things about it are
counter-intuitive enough to be worth not rediscovering:

- **You cannot trigger on a low yield.** The model does not fail quietly on a
  sideways page, it invents plausible reception numbers. 0 of 86 documents
  return zero references, and the worst offender returns five, all fabricated.
  An earlier design escalated to a stronger model on an empty result; it would
  never have fired.
- **Two questions, not one.** "How many degrees?" finds turned pages and then
  answers 90 for all of them, including the ones needing 270. "Which of these
  reads normally?" gets the direction right. So: a 3-way call (as supplied /
  anticlockwise / clockwise) on every page, then a 2-way call on the ~10% it
  flags.
- **Smaller thumbnails are better.** 65k pixels beats 260k beats 1M, and
  batching several pages into one call costs precision the same way batching
  tiles does. Orientation is a gestalt property; shrink the page until only
  layout survives. Measured on 33 hand-read pages: 16/16 found, 2 false
  positives, 1 turned the wrong way.

A flagged page is then **read both ways and the better answer kept**, never
just turned — the probe's false positives are real, and turning an upright page
loses every reference on it. "Better" counts only references that parsed as an
actual citation (`id_type != "other"`): a wrongly-turned render still emits
plenty of free text, and counting that junk loses real reception numbers.

The wording of the tool's one field is load-bearing. Describing it as "the
1-based index of the image that reads normally" instead of spelling out
"Which image reads normally: 1, 2 or 3" flipped `exception_2696065.pdf` — a
41-reference Map of Survey, plainly sideways — from 90 back to 0, reproducibly.
Re-score if you touch either string.

An ink-projection heuristic with no model call at all was tried first and
rejected: 7/16 recall at 70% precision, firing on `exception_3511023`'s dense
upright township tables and missing `exception_2873123`'s sideways exhibits.

Cost: the probe adds ~9% and re-reading flagged pages ~12%. Over the corpus,
corpus-verified reception numbers — ids naming a PDF the scraper actually
downloaded, so no human adjudication needed — go 218 → 257.

## The extraction cache — the one real cost lever

Reading a document for its citations is the most expensive thing a run does. On real
Weld data **no recorder PDF has a text layer**, so `id_extraction.py`'s free path never
fires and all ~90 documents take the Bedrock vision path — ~340 model calls and ~98% of
a run's total cost.

`_extract_cited_ids()` (`scrapers/weld_county.py`) puts an S3 cache in front of that,
keyed by reception number under `extractions/`. A recorded document is immutable once
filed, so its citations never change and the answer is reusable forever. Two ways that
pays off, both common:

- **Re-running a property** (the Reprocess button) costs ~$0 in Bedrock instead of ~$2.
- **A different parcel in the same section** reuses everything
  `_section_township_range_search()` and `_easement_row_search()` turn up — those return
  the same easements, plats and ROW documents for every parcel in a section.

Three invariants to keep:

1. **A hit reports zero tokens.** `IdExtraction(source="cache")` carries the original
   ids but `input_tokens=0`/`output_tokens=0`, because *this* run didn't spend them and
   `costs.py` prices the run off those fields. Copying the original counts over would
   silently re-bill every cached document.
2. **Failures aren't cached.** `source="none"` means the read failed (unreadable PDF,
   Bedrock outage); storing it would make that failure permanent for the document.
3. **The key includes a fingerprint** (`id_extraction.cache_fingerprint()`) of the model,
   the tile geometry *and the wire encoding* — everything that would change the answer.
   Re-tune `_TILE_MAX_NATIVE_PX`, or change how a tile is rendered, and you get a fresh
   namespace rather than results produced by the old settings. That's also how you
   invalidate the cache deliberately.

The cache needs `s3:GetObject` on `TaskRole` (added in `infra/cloudformation/backend.yaml`).
It swallows every error by design, so a missing grant doesn't fail the run — it just
makes the cache always miss and every run cost full price. If cache hits never appear in
the logs, check that permission first. `extractions/` deliberately has **no** expiry
lifecycle rule, so entries outlive the `documents/` copies they describe.

## Estimated cost & run time (`worker.py`, frontend's Run Details tab)

Every job records a cost/runtime breakdown, written by `run_job()` via
`jobs.update_status()` on every exit path (`COMPLETED`, `FAILED`, and the generic
`except` — a crashed job still burned real Bedrock tokens and real Fargate seconds, so
it still gets a number).

**`costs.py` owns the all-in itemisation.** It turns quantities the run measured
(tokens, seconds, bytes stored, log volume, derived poll counts) into one `CostLine` per
service — Bedrock, Fargate, S3, DynamoDB, API Gateway+Lambda+SQS, CloudWatch Logs —
which `Job.costs` round-trips and the Run Details tab renders and totals generically.
**Add a service there, not in the frontend**: the tab renders whatever lines it gets.

Two things about that module worth knowing before editing it:

- **Only Bedrock is `basis="measured"`.** Everything else is arithmetic over a published
  rate card, and the UI says so. Don't mark a line "measured" unless a provider reports
  the number.
- **Its rates are us-west-2 and were read from the AWS Pricing API on 2026-09-17.**
  DynamoDB on-demand in this region is $0.625/M write and $0.125/M read — *half* the
  widely-quoted us-east-1 figures. Don't "correct" them back.

For scale, on a real ~50-minute Weld property: Bedrock $1.98, Fargate $0.042, DynamoDB
$0.019, everything else under a cent. Bedrock is ~97% of the run, which is the entire
reason the extraction cache above exists. Note the DynamoDB line is third-largest and
grows **quadratically** with log volume — `append_log()` rewrites the whole job item per
line, so N appends against a growing item cost ~N²/2 write units. Irrelevant next to a
$2 model bill; not irrelevant on a fully-cached re-run that costs $0.06 total.

The two inputs feeding the biggest lines:

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

## `documents` — what each downloaded file is, for the Results tab

`scrape()` ends by writing an `overview.json` section listing every file it saved:
`{file, reception, doc_type, category, role}`, sorted by category and then reception
number. `doc_classify.py` assigns the category, and its `CATEGORIES` list **is** the
display order — the frontend derives the order from the row sequence rather than keeping
its own copy (see [`apps/web/AGENTS.md`](../../../apps/web/AGENTS.md)), so a new category
goes in that module and nowhere else.

Categories are matched by keyword against the type label, ordered so the specific reading
wins (`MINERAL & ROYALTY DEED` is mineral, not a vesting deed; `ASSIGNMENT DEED OF TRUST`
is financing). The recorder's own vocabulary is far larger than it looks useful —
114 distinct document types in one section — so enumerating it is not an option;
`tests/test_doc_classify.py` pins the rules against `weld_document_types.json`, a real
sweep of S32-T5N-R65W's 872 documents, and fails if too much starts falling through to
"Other".

Note `doc_type` is not always a recorder label: a Schedule B-2 exception carries the
description the model read off the citing document ("20' sewer easement"), since that's
all the run knows about a document it reached by citation. The keyword rules handle both.

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

**It walks a whole depth level at a time, not a document at a time** — this is a
performance property, but changing it back would quietly cost ~20 minutes a run:

- Every document at a level is read concurrently (`asyncio.gather` over
  `asyncio.to_thread(extract_document_ids, ...)`). On real Weld data *no* recorder PDF
  has a text layer, so every document pays for a Bedrock vision read — a property is
  ~340 Bedrock calls, not a handful. A level now costs about as long as its slowest
  document instead of the sum of all of them.
- `extract_document_ids()` is synchronous and blocks on both PDF decoding and the
  network, so it **must** go through `asyncio.to_thread` — calling it inline stalls the
  event loop and every download sharing it (the same failure mode as the log handler,
  above).
- A level's citations are fetched in **one** `_download_documents()` call. That function
  launches a browser and logs in per call, so batching keeps it at one login per depth
  level rather than one per citing document — and re-logging-in mid-run is exactly what
  poisons the disclaimer cookie (see `_fetch_document`'s comments).

Keep the per-document monkeypatch point intact when editing: the tests patch
`weld_county.extract_document_ids`, and `asyncio.to_thread` resolves it at call time.

Every ID found — from any document, not just the ALTA — lands in `overview.json` as
`extracted_ids`, one flat list of `{id, id_type, context, raw, source_reception,
source_doc_type}` rows (the last two say which downloaded document cited it), written
once per level processed so a crash mid-walk still leaves everything found so far.
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

## Document fetching: concurrency is a politeness budget, not a throughput dial

`_download_documents()` fetches a batch of recorder documents by running
`_fetch_document()` in `weld_download_concurrency`-many tabs of **one** already
authenticated Playwright context. Two things about it are load-bearing:

- **Each worker still paces itself exactly as the old serial loop did** (a growing
  `_DOC_FETCH_PAUSE_S` sleep before *every* attempt, not just retries). The 80/80 success
  measurement recorded in that function's docstring was taken with that pacing in place,
  and what recording.weld.gov reacts to is request rate. `WELD_DOWNLOAD_CONCURRENCY`
  (default 4) therefore multiplies the load on a county server directly — raise it a
  step at a time and watch the job log for retry warnings ("No printCustom button",
  "Print endpoint returned HTTP"). Getting the worker's IP throttled is a worse outcome
  than a slow run.
- **The disclaimer-cookie re-assert is under an `asyncio.Lock`.** Cookies are
  context-wide, so the `clear_cookies` / `add_cookies` pair a retry performs is shared
  with every fetch in flight; without the lock a sibling can land in the window where the
  cookie is missing and get served the disclaimer page instead of its document. Any new
  context-wide mutation in this path needs the same treatment.

The page wait is `wait_for_selector("#printCustom")`, deliberately **not**
`wait_for_load_state("networkidle")`: the viewer is PDF.js pulling page images over HTTP
Range requests, so the network stays busy long after the only thing we need (the print
button's `data-href`) exists.

## Adding metadata/map support to another county

Nothing scraper-agnostic needs to change — `_load_overview`/`_upload_map_image` just
look for keys that happen to exist. To light up Property Metadata / Map for another
county, have its scraper write an `overview.json` (any structure — the frontend renders
whatever's there generically, see `MetadataView` in
[`apps/web/AGENTS.md`](../../../apps/web/AGENTS.md)) and, if you want a Map tab image
rather than just an address-based Google Maps fallback, capture a screenshot the same
way `_capture_map_image()` does in `scrapers/weld_county.py` and set
`overview["map"] = {"image_path": ..., "iframe_url": ...}`.
