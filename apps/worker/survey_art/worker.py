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
from decimal import Decimal
from pathlib import Path

from survey_art.download import _slug
from survey_art.geocode import address_to_county
from survey_art.pipeline import run_async
from survey_shared import aws, jobs
from survey_shared.config import get_shared_settings

logger = logging.getLogger(__name__)
narration = logging.getLogger("survey_art.narration")


class _DynamoLogHandler(logging.Handler):
    """Writes one formatted log record to the job's `logs` list in DynamoDB.

    Only ever called from the QueueListener's background thread (see
    `_job_log_handler` below) — never directly from the scraper — so this
    blocking network call can't stall the asyncio event loop the scraper runs on.
    """

    def __init__(self, job_id: str) -> None:
        super().__init__(level=logging.INFO)
        self.job_id = job_id
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            jobs.append_log(self.job_id, self.format(record))
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
        self._listener = logging.handlers.QueueListener(
            self._queue, _DynamoLogHandler(job_id), respect_handler_level=True
        )
        self._listener.start()

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
    try:
        with tempfile.TemporaryDirectory(prefix=f"job-{job_id}-") as tmp:
            saved, err = await run_async(
                address, tmp_dir=Path(tmp), quiet=True, county_override=county
            )
            if err:
                jobs.update_status(job_id, jobs.FAILED, error=err)
                narration.info("We hit a problem and couldn't finish this search.")
                logger.error("Job %s FAILED: %s", job_id, err)
                return 1
            metadata = _load_overview(Path(tmp))
            doc_prefix = _doc_prefix(Path(tmp), saved, address, metadata)
            count = jobs.upload_documents(doc_prefix, saved)
            _upload_map_image(job_id, metadata)
            location = _resolve_location(address, metadata)
            jobs.update_status(
                job_id,
                jobs.COMPLETED,
                file_count=count,
                metadata=metadata,
                location=location,
                doc_prefix=doc_prefix,
            )
            narration.info(f"All done — found {count} document(s) for this property.")
            logger.info("Job %s COMPLETED: %s file(s) uploaded", job_id, count)
            return 0
    except Exception as exc:  # noqa: BLE001 — surface any failure to the job record
        narration.info("Something unexpected went wrong and the search had to stop.")
        logger.exception("Job %s crashed", job_id)
        jobs.update_status(job_id, jobs.FAILED, error=str(exc))
        return 1
    finally:
        scraper_logger.removeHandler(log_handler)
        log_handler.close()


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
