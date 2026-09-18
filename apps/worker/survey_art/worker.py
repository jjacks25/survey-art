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
from decimal import Decimal
from pathlib import Path

from survey_art import costs
from survey_art.download import _slug
from survey_art.geocode import address_to_county
from survey_art.id_extraction import make_thumbnail
from survey_art.pipeline import run_async
from survey_art.settings import get_settings
from survey_shared import aws, jobs
from survey_shared.config import get_shared_settings

logger = logging.getLogger(__name__)
narration = logging.getLogger("survey_art.narration")

# ponytail: flat on-demand Fargate rate (Linux/x86, us-west-2 — this deploy's default
# region, see infra/deploy.py's DEFAULT_REGION) times wall-clock task runtime, rather
# than a real AWS Cost Explorer/CUR integration (24-48h reporting lag, can't back a
# live UI). Revisit with per-region pricing if this ever deploys outside us-west-2.
_FARGATE_VCPU_HOUR_USD = 0.04048
_FARGATE_GB_HOUR_USD = 0.004445
_FARGATE_VCPUS = 1  # WorkerTaskDefinition: Cpu: '1024'
_FARGATE_MEM_GB = 2  # WorkerTaskDefinition: Memory: '2048'

# On-demand Bedrock price per 1K tokens, (input, output) — from https://claude.com/pricing
# (Bedrock tracks Anthropic's own published rates 1:1). Keyed by the substring a Bedrock
# model/inference-profile ID contains, e.g. "us.anthropic.claude-haiku-4-5-20251001-v1:0".
# Fallback only: browser-use's Agent scrapers (Denver/Arapahoe/Jefferson) already return a
# real dollar cost via llm.agent_cost(), so this only fires when that's 0 — today, that's
# every Weld run, since id_extraction.py's raw bedrock-runtime.converse() calls never priced
# their own tokens. ponytail: add a row here for each new model id_extraction_model/model
# gets pointed at; there's no API that returns this, so it can't be looked up automatically.
_BEDROCK_PRICE_PER_1K_TOKENS: dict[str, tuple[float, float]] = {
    "haiku-4-5": (0.001, 0.005),
    "sonnet-4-5": (0.003, 0.015),
    "sonnet-4-6": (0.003, 0.015),
    "opus-4-5": (0.005, 0.025),
    "opus-4-6": (0.005, 0.025),
    "opus-4-7": (0.005, 0.025),
    "opus-4-8": (0.005, 0.025),
    "sonnet-5": (0.002, 0.010),
    "opus-5": (0.005, 0.025),
    # Bedrock on-demand rate (not OpenAI's own API rate, which differs) — per AWS's
    # 2026-07-30 Bedrock price cut announcement for GPT-5.6 Luna/Terra.
    "gpt-5.6-luna": (0.00022, 0.00132),
}


def _bedrock_token_cost(model: str, in_tok: int, out_tok: int) -> float:
    for fragment, (in_price, out_price) in _BEDROCK_PRICE_PER_1K_TOKENS.items():
        if fragment in model:
            return (in_tok / 1000) * in_price + (out_tok / 1000) * out_price
    logger.warning("No Bedrock price entry for model %r — bedrock_cost_usd will read 0", model)
    return 0.0


def _cost_fields(
    start_time: float,
    bedrock_cost_usd: float,
    in_tok: int,
    out_tok: int,
    *,
    saved: list[Path] | None = None,
    log_volume: tuple[int, int] = (0, 0),
) -> dict:
    """Cost breakdown for one job run, for jobs.update_status().

    `costs` is the full per-service itemisation (see costs.py). The four scalar
    fields alongside it are the two biggest lines repeated, kept because job
    records written before `costs` existed still carry them and DynamoDB items
    don't migrate themselves.

    `log_volume` is read before the log handler has finished draining its queue,
    so it misses the handful of lines this call itself is about to emit — an
    estimate whose two consumers (DynamoDB write units, CloudWatch ingestion)
    together come to a fraction of a cent, so it isn't worth restructuring the
    job's exit paths to get exact.
    """
    if not bedrock_cost_usd and (in_tok or out_tok):
        bedrock_cost_usd = _bedrock_token_cost(get_settings().id_extraction_model, in_tok, out_tok)
    elapsed = time.time() - start_time
    fargate_cost = (elapsed / 3600) * (
        _FARGATE_VCPUS * _FARGATE_VCPU_HOUR_USD + _FARGATE_MEM_GB * _FARGATE_GB_HOUR_USD
    )
    files = [p for p in (saved or []) if p.is_file()]
    log_appends, log_bytes = log_volume
    return {
        "bedrock_cost_usd": round(bedrock_cost_usd, 4),
        "bedrock_input_tokens": in_tok,
        "bedrock_output_tokens": out_tok,
        "fargate_cost_usd": round(fargate_cost, 4),
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

    def emit(self, record: logging.LogRecord) -> None:
        try:
            kind = "milestone" if record.name == narration.name else "detail"
            message = self.format(record)
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
            return json.loads(path.read_text(), parse_float=Decimal)
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
            # DynamoDB rejects native floats — round-trip through str() to Decimal.
            return {"lat": Decimal(str(geocoded.lat)), "lon": Decimal(str(geocoded.lon))}
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
    (``tmp/{county_key}/{slug}/...``, see ``download.make_download_dir``) —
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
    return f"{state}/{county_name}/{_slug(identifier)}"


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


async def run_job(job_id: str, address: str, county: str | None) -> int:
    """Run one scrape job end-to-end. Returns a process-style exit code."""
    jobs.update_status(job_id, jobs.RUNNING)
    logger.info("Job %s RUNNING: %s (county=%s)", job_id, address, county or "auto")
    scraper_logger = logging.getLogger("survey_art")
    log_handler = _JobLogHandler(job_id)
    scraper_logger.addHandler(log_handler)
    narration.info(f"Starting your search for {address}...")
    start_time = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix=f"job-{job_id}-") as tmp:
            saved, err, cost, in_tok, out_tok = await run_async(
                address, tmp_dir=Path(tmp), quiet=True, county_override=county
            )
            if err:
                jobs.update_status(
                    job_id,
                    jobs.FAILED,
                    error=err,
                    **_cost_fields(
                        start_time, cost, in_tok, out_tok,
                        saved=saved, log_volume=log_handler.volume,
                    ),
                )
                narration.info("We hit a problem and couldn't finish this search.")
                logger.error("Job %s FAILED: %s", job_id, err)
                return 1
            metadata = _load_overview(Path(tmp))
            doc_prefix = _doc_prefix(Path(tmp), saved, address, metadata)
            count = jobs.upload_documents(doc_prefix, saved)
            jobs.upload_thumbnails(doc_prefix, _make_thumbnails(saved))
            _upload_map_image(job_id, metadata)
            location = _resolve_location(address, metadata)
            jobs.update_status(
                job_id,
                jobs.COMPLETED,
                file_count=count,
                metadata=metadata,
                location=location,
                doc_prefix=doc_prefix,
                **_cost_fields(
                    start_time, cost, in_tok, out_tok,
                    saved=saved, log_volume=log_handler.volume,
                ),
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
        jobs.update_status(
            job_id,
            jobs.FAILED,
            error=str(exc),
            **_cost_fields(start_time, 0.0, 0, 0, log_volume=log_handler.volume),
        )
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
    queue_url = get_shared_settings().require_job_queue_url()
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
