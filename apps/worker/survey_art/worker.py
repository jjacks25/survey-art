"""Worker entrypoint — runs scrape jobs.

Two modes, one image:

* **One-shot (AWS/Fargate):** when ``JOB_ID`` is set in the environment, run exactly
  that job and exit. This is how the dispatcher Lambda launches the task (job
  parameters passed as ECS container env overrides ``JOB_ID``/``ADDRESS``/``COUNTY``).

* **Poll loop (local dev):** when ``JOB_ID`` is not set, long-poll the SQS job queue
  and process messages as they arrive. Locally there is no dispatcher Lambda/ECS, so
  this stands in as the queue consumer in docker-compose.

Either way a job marks itself RUNNING, runs the existing async pipeline into a temp
dir, uploads documents to S3, and marks the job COMPLETED or FAILED. Result files and
job state live in S3/DynamoDB, so the task is stateless.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import queue
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from survey_art import cad_export, costs, deed_plot, plss
from survey_art.download import slug
from survey_art.geocode import address_to_county
from survey_art.id_extraction import make_thumbnail
from survey_art.pipeline import run_async
from survey_art.settings import get_settings
from survey_shared import aws, jobs
from survey_shared.config import get_shared_settings

logger = logging.getLogger(__name__)
narration = logging.getLogger("survey_art.narration")


def _cost_fields(
    start_time: float,
    bedrock_cost_usd: float,
    in_tok: int,
    out_tok: int,
    *,
    saved: list[Path],
    log_volume: tuple[int, int],
) -> dict:
    """Cost breakdown for one job run, for jobs.update_status() — see costs.py.

    `log_volume` is read before the log handler has finished draining its queue,
    so it misses the handful of lines the caller is about to emit — an estimate
    whose two consumers (DynamoDB write units, CloudWatch ingestion) together
    come to a fraction of a cent.
    """
    if not bedrock_cost_usd and (in_tok or out_tok):
        bedrock_cost_usd = costs.bedrock_token_cost(
            get_settings().id_extraction_model, in_tok, out_tok
        )
    elapsed = time.time() - start_time
    files = [p for p in saved if p.is_file()]
    log_appends, log_bytes = log_volume
    return {
        "fargate_seconds": round(elapsed, 1),
        "costs": costs.estimate(
            elapsed_s=elapsed,
            bedrock_usd=bedrock_cost_usd,
            input_tokens=in_tok,
            output_tokens=out_tok,
            document_bytes=sum(p.stat().st_size for p in files),
            document_count=len(files),
            log_appends=log_appends,
            log_bytes=log_bytes,
        ),
    }


# The whole run log lives in the job's DynamoDB item, which is capped at 400 KB —
# and the final status update has to fit in it too. A long Weld run blew through
# that: `append_log` started failing silently, then `update_status` raised and the
# job never reached a terminal state. Past this budget only milestones (a few KB a
# run) are still recorded; CloudWatch keeps every line regardless.
_LOG_DETAIL_BUDGET_BYTES = 250_000
_LOG_TRUNCATED_NOTICE = (
    "Technical log truncated here to stay within the job record's size limit — "
    "the complete log is in CloudWatch (/ecs/survey-art-worker)."
)


class _DynamoLogHandler(logging.Handler):
    """Writes one formatted log record to the job's `logs` list in DynamoDB.

    Only ever called from the QueueListener's background thread (see
    `_job_log_handler` below) — never directly from the scraper — so this
    blocking network call can't stall the asyncio event loop the scraper runs on.

    Records from the `survey_art.narration` logger are tagged `kind="milestone"`
    — the plain-English progress steps a non-technical surveyor should see by
    default — everything else (the scraper's own detailed `logger.*` calls) is
    `kind="detail"`, folded away in the frontend's Logs tab unless expanded.
    See `survey_shared.jobs.LogEntry`.
    """

    def __init__(self, job_id: str) -> None:
        super().__init__(level=logging.INFO)
        self.job_id = job_id
        self.setFormatter(logging.Formatter("%(message)s"))
        # How much log this run actually wrote — the DynamoDB and CloudWatch lines
        # of the cost breakdown are both driven by it (see costs.py). Only ever
        # touched from the single QueueListener thread that calls emit().
        self.appends = 0
        self.bytes = 0
        self.truncated = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            kind = "milestone" if record.name == narration.name else "detail"
            message = self.format(record)
            if kind == "detail" and self.bytes >= _LOG_DETAIL_BUDGET_BYTES:
                if not self.truncated:
                    self.truncated = True
                    jobs.append_log(self.job_id, _LOG_TRUNCATED_NOTICE, kind="detail")
                return
            self.appends += 1
            self.bytes += len(message.encode())
            jobs.append_log(self.job_id, message, kind=kind)
        except Exception:  # noqa: BLE001 — logging must never break the job
            pass


class _JobLogHandler(logging.handlers.QueueHandler):
    """Streams every survey_art.* log record to the job's `logs` list in DynamoDB,
    so the UI can show the exact steps the scraper is taking while it runs.

    `emit()` (called synchronously from the scraper's event loop) only puts the
    record on an in-memory queue — a `QueueListener` drains it on a separate
    thread and does the actual DynamoDB write there, so streaming logs never
    blocks the scraper's own async I/O.
    """

    def __init__(self, job_id: str) -> None:
        self._queue: queue.Queue = queue.Queue()
        super().__init__(self._queue)
        self.setLevel(logging.INFO)
        self._sink = _DynamoLogHandler(job_id)
        self._listener = logging.handlers.QueueListener(
            self._queue, self._sink, respect_handler_level=True
        )
        self._listener.start()

    @property
    def volume(self) -> tuple[int, int]:
        """`(appends, bytes)` written so far. Only meaningful after `close()`,
        which blocks until the listener has drained the queue."""
        return self._sink.appends, self._sink.bytes

    def close(self) -> None:
        self._listener.stop()
        super().close()


def _load_overview(tmp: Path) -> dict | None:
    """Best-effort load of the per-property overview.json (property metadata),
    if the scraper that ran for this job writes one."""
    for path in tmp.rglob("overview.json"):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _resolve_location(address: str, metadata: dict | None) -> dict | None:
    """Best-effort lat/lon for the Map tab. Tries the resolved property address
    from scraper metadata first (more precise, and the only option when the
    user searched by account/parcel number rather than a street address), then
    falls back to the original input address."""
    candidates = []
    if metadata:
        resolved = (metadata.get("identify_results") or {}).get("address")
        if resolved:
            candidates.append(resolved)
    candidates.append(address)
    for candidate in candidates:
        try:
            geocoded = address_to_county(candidate)
        except Exception:  # noqa: BLE001 — the map pin is a nice-to-have, not critical
            continue
        if geocoded and geocoded.lat is not None and geocoded.lon is not None:
            return {"lat": geocoded.lat, "lon": geocoded.lon}
    return None


# Thumbnail rendering is local CPU work on a 1-vCPU Fargate task, so this is
# about overlapping decode with I/O rather than about parallel compute.
_TAIL_WORKERS = 8


def _make_thumbnails(saved: list[Path]) -> dict[str, bytes]:
    """First-page JPEG thumbnails for every saved PDF, keyed by filename — the
    same key `upload_thumbnails()`/`list_result_files()` join back to each
    document by. Skips non-PDFs and anything `make_thumbnail()` can't render
    (e.g. a vector PDF with no embedded page image) rather than failing the
    job over a missing thumbnail; those cards just fall back to the frontend's
    iframe preview.

    Rendered in a thread pool: a property can bring back ~90 documents, and each
    thumbnail decodes a full-page scan raster, so doing them one at a time added
    minutes to the end of a job. Pillow releases the GIL for decode/resize, so
    threads are enough — no process pool needed."""
    pdfs = [p for p in saved if p.suffix.lower() == ".pdf"]
    if not pdfs:
        return {}
    with ThreadPoolExecutor(max_workers=_TAIL_WORKERS) as pool:
        rendered = pool.map(make_thumbnail, pdfs)
        return {p.name: jpeg for p, jpeg in zip(pdfs, rendered, strict=True) if jpeg is not None}


def _doc_prefix(tmp: Path, saved: list[Path], input_address: str, metadata: dict | None) -> str:
    """S3 key prefix documents land under in the storage bucket's ``documents/``
    namespace: ``{state}/{county}/{identifier}/`` — lets a surveyor browse the bucket by
    geography instead of by opaque jobId, and means repeated searches for the
    same property accumulate documents in one place instead of scattering
    them across per-job prefixes.

    County comes from where the scraper actually saved files locally
    (``tmp/{county_key}/{slug}/...``, see ``download.download_dir``) —
    more reliable than the job's `county` param, which may be unset (auto-
    detected). The identifier prefers, in order: the scraper's resolved
    property address, the original input, the account number, the legal
    description, then section/township/range — whichever is known first.
    """
    county_key = saved[0].relative_to(tmp).parts[0] if saved else "unknown_unknown"
    state, _, county_name = county_key.partition("_")
    identify = (metadata or {}).get("identify_results") or {}
    account_info = (metadata or {}).get("account_information") or {}
    identifier = (
        identify.get("address")
        or input_address
        or identify.get("account")
        or account_info.get("legal_description")
        or identify.get("section_township_range")
        or "unknown"
    )
    state = (state or "unknown").lower()
    county_name = (county_name or "unknown").lower()
    return f"{state}/{county_name}/{slug(identifier)}"


def _archive_full_log(job_id: str) -> None:
    """Best-effort archive of this job's complete log to S3 (`jobs.upload_job_log()`).

    Reads the job back from DynamoDB rather than tracking entries locally —
    `log_handler.close()` (called by the caller just before this) blocks
    until the QueueListener has flushed every queued record, so by the time
    this runs the DynamoDB record already holds the complete log. Never
    raises: archiving must not fail a job that's already finished.
    """
    try:
        job = jobs.get_job(job_id)
        if not job or not job.logs:
            return
        header = (
            f"Job: {job.job_id}\n"
            f"Address: {job.address}\n"
            f"County: {job.county}\n"
            f"Status: {job.status}\n"
            + ("Error: " + job.error + "\n" if job.error else "")
            + "-" * 40
        )
        body = "\n".join(f"[{entry.kind}] {entry.message}" for entry in job.logs)
        jobs.upload_job_log(job_id, f"{header}\n{body}\n")
    except Exception:  # noqa: BLE001 — archiving must never break the job
        logger.warning("Failed to archive full log for job %s", job_id, exc_info=True)


def _upload_map_image(job_id: str, metadata: dict | None) -> None:
    """If the scraper captured a property map screenshot (e.g. Weld's parcel
    boundary map — county sites usually block iframe embedding, so a live embed
    can't be shown), upload it and point metadata["map"]["image_url"] at it."""
    if not metadata:
        return
    map_section = metadata.get("map")
    if not isinstance(map_section, dict):
        return
    image_path = map_section.pop("image_path", None)
    if not image_path:
        return
    url = jobs.upload_map_image(job_id, Path(image_path))
    if url:
        map_section["image_url"] = url


# Runaway-spend guard: a property search that's still running after this long gets
# killed outright rather than left to keep burning Fargate/Bedrock time. In one-shot
# (Fargate) mode this process exiting IS the task stopping, so no separate ecs:StopTask
# call is needed.
_JOB_TIMEOUT_SECONDS = 2 * 60 * 60

# How often a running job checks whether the user cancelled it. On Fargate the API's
# ecs:StopTask kills the process anyway; the local poll-loop worker has no task to
# stop, so without this a cancelled job keeps the only worker busy until it finishes.
_CANCEL_POLL_SECONDS = 10


class _JobCancelledError(Exception):
    pass


async def _run_unless_cancelled(job_id: str, coro):
    task = asyncio.ensure_future(coro)
    while True:
        done, _ = await asyncio.wait({task}, timeout=_CANCEL_POLL_SECONDS)
        if done:
            return task.result()
        job = await asyncio.to_thread(jobs.get_job, job_id)
        if job is not None and job.status == jobs.CANCELLED:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise _JobCancelledError


def _draw(job: jobs.Job, tmp: Path) -> tuple[list[Path], float, int, int, str, dict]:
    """A drawing job's whole run: fetch the search's documents back from S3,
    plot them, and write the CAD files. Blocking; runs on a worker thread.

    Returns ``(files, bedrock_usd, in_tok, out_tok, doc_prefix, metadata)``.
    The outputs go under the search's own prefix in a dot-folder, which
    `list_result_files()` hides from the search's document grid but lists for
    the drawing job itself.
    """
    source = jobs.get_job(job.source_job_id or "")
    if source is None or not source.doc_prefix:
        raise RuntimeError("The search this drawing was made from no longer exists.")
    folder = tmp / "documents"
    narration.info("Fetching the documents from your search...")
    jobs.download_documents(source.doc_prefix, folder)
    if source.metadata:
        (folder / "overview.json").write_text(json.dumps(source.metadata))
    state = source.doc_prefix.split("/", 1)[0].upper()
    run = deed_plot.plot_folder(folder, state=state)
    name = source.doc_prefix.rsplit("/", 1)[-1]
    narration.info("Writing the CAD drawing and QC report...")
    files = cad_export.export(run, tmp / "drawing", name=name)
    metadata = {"drawing": {"crs": f"EPSG:{plss.GRID_EPSG}",
                            "documents": cad_export.qc_rows(run)}}  # fmt: skip
    prefix = f"{source.doc_prefix}/{jobs.DRAWING_SUBPREFIX}"
    return files, run.cost_usd, run.input_tokens, run.output_tokens, prefix, metadata


async def run_job(job_id: str, address: str, county: str | None) -> int:
    """Run one scrape job end-to-end. Returns a process-style exit code."""
    job = jobs.get_job(job_id)
    if job is None or job.status in jobs.TERMINAL:
        logger.info("Job %s skipped: already %s", job_id, job.status if job else "deleted")
        return 0
    jobs.update_status(job_id, jobs.RUNNING)
    logger.info("Job %s RUNNING: %s (county=%s)", job_id, address, county or "auto")
    scraper_logger = logging.getLogger("survey_art")
    log_handler = _JobLogHandler(job_id)
    scraper_logger.addHandler(log_handler)
    drawing = job.kind == "drawing"
    narration.info(
        f"Starting a CAD drawing for {address}..."
        if drawing
        else f"Starting your search for {address}..."
    )
    start_time = time.time()
    saved: list[Path] = []
    cost, in_tok, out_tok = 0.0, 0, 0

    def finish(status: str, **fields) -> None:
        # Every exit path records a cost: a failed run still burned real
        # Bedrock tokens and Fargate seconds.
        jobs.update_status(
            job_id,
            status,
            **fields,
            **_cost_fields(
                start_time, cost, in_tok, out_tok, saved=saved, log_volume=log_handler.volume
            ),
        )

    try:
        with tempfile.TemporaryDirectory(prefix=f"job-{job_id}-") as tmp_name:
            tmp = Path(tmp_name)
            # ponytail: a cancelled drawing stops being awaited, but its thread
            # runs on until the Fargate task exits (immediately, in one-shot mode).
            work = (
                asyncio.to_thread(_draw, job, tmp)
                if drawing
                else run_async(address, tmp_dir=tmp, quiet=True, county_override=county)
            )
            try:
                result = await _run_unless_cancelled(
                    job_id, asyncio.wait_for(work, timeout=_JOB_TIMEOUT_SECONDS)
                )
            except _JobCancelledError:
                narration.info("This search was cancelled.")
                logger.info("Job %s CANCELLED: scrape stopped", job_id)
                return 0
            except TimeoutError:
                hours = _JOB_TIMEOUT_SECONDS // 3600
                finish(
                    jobs.FAILED,
                    error=f"Search exceeded the {hours}-hour time limit and was stopped.",
                )
                narration.info("This search took too long and was stopped to avoid runaway cost.")
                logger.error("Job %s FAILED: timed out after %ss", job_id, _JOB_TIMEOUT_SECONDS)
                return 1
            if drawing:
                saved, cost, in_tok, out_tok, doc_prefix, metadata = result
                count = jobs.upload_documents(doc_prefix, saved)
                finish(jobs.COMPLETED, file_count=count, metadata=metadata, doc_prefix=doc_prefix)
                narration.info("All done — the CAD drawing and QC report are ready.")
                logger.info("Job %s COMPLETED: drawing with %s file(s)", job_id, count)
                return 0
            saved, err, cost, in_tok, out_tok = result
            if err:
                finish(jobs.FAILED, error=err)
                narration.info("We hit a problem and couldn't finish this search.")
                logger.error("Job %s FAILED: %s", job_id, err)
                return 1
            metadata = _load_overview(tmp)
            doc_prefix = _doc_prefix(tmp, saved, address, metadata)
            count = jobs.upload_documents(doc_prefix, saved)
            jobs.upload_thumbnails(doc_prefix, _make_thumbnails(saved))
            _upload_map_image(job_id, metadata)
            finish(
                jobs.COMPLETED,
                file_count=count,
                metadata=metadata,
                location=_resolve_location(address, metadata),
                doc_prefix=doc_prefix,
            )
            if (metadata or {}).get("limits", {}).get("cross_reference_depth_limit"):
                narration.info(
                    "Note: stopped chasing document citations after 3 hops deep — "
                    "some more-distantly-referenced documents may not be included."
                )
            narration.info(f"All done — found {count} document(s) for this property.")
            logger.info("Job %s COMPLETED: %s file(s) uploaded", job_id, count)
            return 0
    except Exception as exc:  # noqa: BLE001 — surface any failure to the job record
        narration.info("Something unexpected went wrong and the search had to stop.")
        logger.exception("Job %s crashed", job_id)
        finish(jobs.FAILED, error=str(exc))
        return 1
    finally:
        scraper_logger.removeHandler(log_handler)
        log_handler.close()
        _archive_full_log(job_id)


async def _run_once() -> int:
    job_id = os.environ.get("JOB_ID")
    address = os.environ.get("ADDRESS")
    county = os.environ.get("COUNTY") or None
    if not job_id or not address:
        logger.error("JOB_ID and ADDRESS environment variables are required")
        return 2
    return await run_job(job_id, address, county)


async def _poll_loop() -> int:
    """Local-dev queue consumer: process SQS messages until interrupted."""
    queue_url = get_shared_settings().require("job_queue_url")
    sqs = aws.client("sqs")
    logger.info("Worker polling %s", queue_url)
    while True:
        resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=20)
        for msg in resp.get("Messages", []):
            body = json.loads(msg["Body"])
            await run_job(body["jobId"], body["address"], body.get("county") or None)
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    if os.environ.get("JOB_ID"):
        sys.exit(asyncio.run(_run_once()))
    asyncio.run(_poll_loop())


if __name__ == "__main__":
    main()
