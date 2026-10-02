"""Job state (DynamoDB) and result storage (S3) — shared by the API and worker.

The jobs table is a single-key table (partition key ``jobId``). Status transitions:
``PENDING`` (created by the API) → ``RUNNING`` → ``COMPLETED`` | ``FAILED`` (worker).
Result documents are stored in S3 under ``documents/{state}/{county}/{property}/``
(see ``upload_documents()``) and surfaced to the UI as presigned GET URLs by the API.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from survey_shared import aws
from survey_shared.config import get_shared_settings

logger = logging.getLogger(__name__)

# ponytail: a single log line pasted from a scraper (e.g. a raw HTML/JSON dump)
# could otherwise grow the DynamoDB item without bound on its own; cap it here
# rather than trusting every future logger.info() call to be well-behaved.
_MAX_LOG_MESSAGE_CHARS = 4000

PENDING = "PENDING"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
TERMINAL = {COMPLETED, FAILED, CANCELLED}

PRESIGN_TTL_SECONDS = 3600
JOB_TTL_SECONDS = 7 * 24 * 60 * 60  # kept in sync with the documents/ S3 lifecycle rule


class LogEntry(BaseModel):
    """One line of a job's running log.

    `kind` distinguishes plain-English milestones ("milestone" — the
    `survey_art.narration` logger, see
    [`apps/worker/survey_art/AGENTS.md`](../../apps/worker/survey_art/AGENTS.md))
    from verbose developer diagnostics ("detail" — every other logger), so the
    frontend's Logs tab can show a clean step-by-step progress list by default
    with the raw feed available on demand instead of one undifferentiated wall
    of text.
    """

    message: str
    kind: Literal["milestone", "detail"] = "detail"


class CostLine(BaseModel):
    """One service's share of a run's cost, for the Run Details tab.

    Produced by `apps/worker/survey_art/costs.py`, which owns the rates and the
    arithmetic; this package just round-trips the result. `basis` is "measured"
    only for figures that come from a provider's own accounting (today: Bedrock
    tokens) and "estimated" for everything priced from a published rate card, so
    the UI can label them honestly rather than implying they're billed amounts.
    """

    key: str
    label: str
    usd: float
    detail: str = ""
    basis: Literal["measured", "estimated"] = "estimated"


class Job(BaseModel):
    """A scrape job record. Field aliases are the DynamoDB item attribute names."""

    model_config = {"populate_by_name": True}

    job_id: str = Field(alias="jobId")
    address: str
    county: str
    status: str
    created_at: int = Field(alias="createdAt")
    updated_at: int = Field(alias="updatedAt")
    expires_at: int | None = Field(default=None, alias="expiresAt")
    error: str | None = None
    file_count: int = Field(default=0, alias="fileCount")
    task_arn: str | None = Field(default=None, alias="taskArn")
    logs: list[LogEntry] = Field(default_factory=list)
    metadata: dict | None = None
    # S3 key metadata was uploaded to (see upload_metadata()) — not stored
    # inline in the item because extracted_ids can grow past DynamoDB's 400KB
    # item cap on its own. get_job() resolves this into `metadata` for
    # callers; from_item()/to_item() otherwise treat it as an opaque field.
    metadata_key: str | None = Field(default=None, alias="metadataKey")
    location: dict[str, float] | None = None
    doc_prefix: str | None = Field(default=None, alias="docPrefix")
    # Wall-clock task runtime, which the Run Details tab also shows as run time.
    fargate_seconds: float | None = Field(default=None, alias="fargateSeconds")
    # The per-service cost breakdown (see apps/worker/survey_art/costs.py). `usd`
    # is typed, so DynamoDB's Decimals coerce back to float on read.
    costs: list[CostLine] = Field(default_factory=list)
    # "search" scrapes a property; "drawing" turns a finished search's documents
    # into a CAD drawing (survey_art/deed_plot.py). A drawing job points back at
    # its search through `source_job_id`, and the search at its latest drawing
    # through `drawing_job_id`, so the Results page can find it in one read.
    kind: Literal["search", "drawing"] = "search"
    source_job_id: str | None = Field(default=None, alias="sourceJobId")
    drawing_job_id: str | None = Field(default=None, alias="drawingJobId")

    def to_item(self) -> dict:
        # Alias keys for DynamoDB; drop error when unset rather than storing null.
        return self.model_dump(by_alias=True, exclude_none=True)

    @classmethod
    def from_item(cls, item: dict) -> Job:
        return cls.model_validate(item)


class SavedProperty(BaseModel):
    """A permanent record that a property has been searched at least once —
    survives `delete_job()`/`delete_jobs_for_property()` clearing run history,
    since it lives in its own table rather than the jobs table. Just enough to
    re-submit the same search (`address` holds whatever was typed — a street
    address or an account/parcel number, same overload as `Job.address`)."""

    model_config = {"populate_by_name": True}

    key: str = Field(alias="propertyKey")
    address: str
    county: str
    saved_at: int = Field(alias="savedAt")

    def to_item(self) -> dict:
        return self.model_dump(by_alias=True, exclude_none=True)

    @classmethod
    def from_item(cls, item: dict) -> SavedProperty:
        return cls.model_validate(item)


def _table():
    return aws.resource("dynamodb").Table(get_shared_settings().require("jobs_table"))


def _saved_properties_table():
    return aws.resource("dynamodb").Table(get_shared_settings().require("saved_properties_table"))


def create_job(
    job_id: str,
    address: str,
    county: str,
    *,
    kind: Literal["search", "drawing"] = "search",
    source_job_id: str | None = None,
) -> Job:
    now = int(time.time())
    job = Job(
        job_id=job_id,
        address=address,
        county=county,
        status=PENDING,
        created_at=now,
        updated_at=now,
        expires_at=now + JOB_TTL_SECONDS,
        kind=kind,
        source_job_id=source_job_id,
    )
    _table().put_item(Item=job.to_item())
    return job


def set_drawing_job(job_id: str, drawing_job_id: str) -> None:
    """Point a search job at its latest drawing job. Deliberately not
    `update_status()`: that also bumps `updatedAt`, which the Run Details tab
    reads as the search's finish time."""
    _table().update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET drawingJobId = :d",
        ExpressionAttributeValues={":d": drawing_job_id},
    )


def get_job(job_id: str) -> Job | None:
    resp = _table().get_item(Key={"jobId": job_id})
    item = resp.get("Item")
    if not item:
        return None
    job = Job.from_item(item)
    if job.metadata_key:
        job.metadata = _download_metadata(job.metadata_key)
    return job


def list_jobs(limit: int = 100) -> list[Job]:
    """Most-recent-first job history for the sidebar. A single `scan` is fine
    at this table's size (single-user/small-team MVP, no query pattern beyond
    "recent jobs" yet) — switch to a GSI on a constant partition + createdAt
    sort key if this table ever grows large enough for scan cost to matter."""
    items = _table().scan().get("Items", [])
    # Drawing jobs are reached from their search's Results page, not listed
    # as properties of their own.
    jobs_ = [j for item in items if (j := Job.from_item(item)).kind == "search"]
    jobs_.sort(key=lambda j: j.created_at, reverse=True)
    return jobs_[:limit]


def _dynamo_value(value):
    """DynamoDB rejects native floats; go through str() so the stored decimal is
    what was computed, not its binary-float neighbour."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_dynamo_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _dynamo_value(v) for k, v in value.items()}
    return value


def update_status(job_id: str, status: str, **fields) -> None:
    """Set status plus any other `Job` fields given by their Python names
    (`file_count=3`, `costs=[...]`), unless the job was already cancelled — a
    cancel wins over a worker that finishes (or fails) after the user gave up
    on it. `metadata` is uploaded to S3 and stored as `metadataKey` instead
    (see `upload_metadata()`)."""
    if (metadata := fields.pop("metadata", None)) is not None:
        fields["metadata_key"] = upload_metadata(job_id, metadata)
    fields = {"status": status, "updated_at": int(time.time()), **fields}
    # Every attribute goes through a name placeholder, so DynamoDB's reserved
    # words (status, error, location, costs, ...) never need special-casing.
    names, values, sets = {}, {}, []
    for i, (name, value) in enumerate(f for f in fields.items() if f[1] is not None):
        names[f"#a{i}"] = Job.model_fields[name].alias or name
        values[f":v{i}"] = _dynamo_value(value)
        sets.append(f"#a{i} = :v{i}")
    try:
        _table().update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_not_exists(#a0) OR #a0 <> :cancelled",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={**values, ":cancelled": CANCELLED},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def append_log(
    job_id: str, message: str, *, kind: Literal["milestone", "detail"] = "detail"
) -> None:
    """Append one entry to the job's running log (best-effort, non-fatal on error).

    See `LogEntry` for what `kind` means to the frontend.
    """
    if len(message) > _MAX_LOG_MESSAGE_CHARS:
        message = message[:_MAX_LOG_MESSAGE_CHARS] + "... [truncated]"
    try:
        _table().update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #l = list_append(if_not_exists(#l, :empty), :line)",
            ExpressionAttributeNames={"#l": "logs"},
            ExpressionAttributeValues={":line": [{"message": message, "kind": kind}], ":empty": []},
        )
    except ClientError:
        pass


def cancel_job(job_id: str, *, _attempts: int = 3) -> Job | None:
    """Mark a job CANCELLED unless it already reached a terminal state. Returns
    the job (so the caller can stop its ECS task via ``task_arn``), or None if
    the job doesn't exist or already finished. Retries a few times against the
    optimistic-concurrency check, since the worker may flip PENDING -> RUNNING
    in the same instant the user hits cancel."""
    for _ in range(_attempts):
        job = get_job(job_id)
        if job is None or job.status in TERMINAL:
            return None
        try:
            _table().update_item(
                Key={"jobId": job_id},
                UpdateExpression="SET #s = :s, updatedAt = :u",
                ConditionExpression="#s = :prev",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": CANCELLED,
                    ":u": int(time.time()),
                    ":prev": job.status,
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue
            raise
        job.status = CANCELLED
        return job
    return None


def delete_job(job_id: str) -> bool:
    """Permanently remove a job's record (status, logs, metadata) from the jobs
    table. Leaves S3 untouched — the property's documents/log archive/map
    screenshot age out on their own lifecycle rules (see `upload_documents()`
    etc.) and are shared across repeated searches for the same property, so
    deleting one job's history entry shouldn't reach into them. Returns
    whether a record actually existed to delete."""
    resp = _table().delete_item(Key={"jobId": job_id}, ReturnValues="ALL_OLD")
    return "Attributes" in resp


def save_property(address: str, county: str) -> None:
    """Upsert a permanent record that `address` (a street address or
    account/parcel number, whatever was submitted) has been searched. Keyed on
    the raw address string — the same fallback `property_key()` uses before a
    job has a `doc_prefix` — so re-running the same input overwrites the same
    record's `saved_at` rather than piling up duplicates."""
    _saved_properties_table().put_item(
        Item=SavedProperty(
            key=address, address=address, county=county, saved_at=int(time.time())
        ).to_item()
    )


def list_saved_properties(limit: int = 200) -> list[SavedProperty]:
    """Every property ever searched, most-recent-first. Unlike `list_jobs()`,
    never shrinks when run history is deleted — that's the point."""
    items = _saved_properties_table().scan().get("Items", [])
    props = [SavedProperty.from_item(item) for item in items]
    props.sort(key=lambda p: p.saved_at, reverse=True)
    return props[:limit]


def property_key(job: Job) -> str:
    """Grouping key for 'every run of the same property' — a job's docPrefix
    once it has one, else its raw address. Mirrors the frontend's
    propertyHistory dedup key (Layout.tsx), so a property-level delete removes
    exactly the runs the sidebar already collapses into one entry."""
    return job.doc_prefix or job.address


def delete_jobs_for_property(key: str) -> int:
    """Delete every job record sharing `property_key()` with `key` — repeated
    searches for one property (retries, re-runs) otherwise pile up as separate
    job records that a single-job delete only clears one at a time. Leaves S3
    untouched, same as `delete_job()`. Returns how many records were deleted."""
    items = _table().scan().get("Items", [])
    matches = [j for item in items if property_key(j := Job.from_item(item)) == key]
    for j in matches:
        _table().delete_item(Key={"jobId": j.job_id})
    return len(matches)


DOCUMENTS_PREFIX = "documents"
SCRATCH_PREFIX = "scratch"
LOGS_PREFIX = "property-search-logs"


def upload_job_log(job_id: str, text: str) -> str:
    """Archive a job's complete log as a plain-text file at
    ``property-search-logs/{job_id}.log``.

    Kept for 30 days (the bucket's ``property-search-logs/`` lifecycle rule) — deliberately
    longer than a job record's own 7-day DynamoDB TTL (`Job.expires_at`), so
    the exact log of a run survives long enough to debug an issue reported
    after the job record itself has aged out. Returns the S3 key.
    """
    s3 = aws.client("s3")
    bucket = aws.storage_bucket()
    key = f"{LOGS_PREFIX}/{job_id}.log"
    s3.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"), ContentType="text/plain")
    return key


def upload_metadata(job_id: str, metadata: dict) -> str:
    """Upload a job's property metadata (the scraper's ``overview.json``) to
    ``scratch/metadata/{job_id}.json`` and return the S3 key.

    Stored in S3 rather than inline on the DynamoDB item: `extracted_ids`
    (see [`apps/worker/survey_art/AGENTS.md`](../../apps/worker/survey_art/AGENTS.md))
    grows with how tangled a property's document chain is and can push a job
    item past DynamoDB's 400KB cap on its own. Job-specific scratch output
    (like the map screenshot), not a durable per-property record, so it lives
    under `scratch/` and ages out on that 90-day lifecycle rule rather than
    `documents/`'s.
    """
    s3 = aws.client("s3")
    bucket = aws.storage_bucket()
    key = f"{SCRATCH_PREFIX}/metadata/{job_id}.json"
    body = json.dumps(metadata).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    return key


def _download_metadata(key: str) -> dict | None:
    """Best-effort read-back of `upload_metadata()`'s object. Never raises —
    metadata is a nice-to-have for the Property Metadata tab, not something
    that should fail a job status lookup."""
    try:
        s3 = aws.client("s3")
        obj = s3.get_object(Bucket=aws.storage_bucket(), Key=key)
        return json.loads(obj["Body"].read())
    except (ClientError, ValueError):
        logger.warning("Could not load metadata from s3://%s", key)
        return None


# Concurrent S3 PUTs for a job's documents/thumbnails. Latency-bound, not
# CPU-bound, so this sits well above the task's 1 vCPU.
_UPLOAD_WORKERS = 8


def upload_documents(prefix: str, files: list[Path]) -> int:
    """Upload downloaded county documents to the storage bucket under
    ``documents/{prefix}/`` (e.g. ``documents/co/weld/123_main_st/``, see
    worker.py's ``_doc_prefix()``) rather than a per-job scratch prefix — files
    for the same property land in the same place across repeated searches.
    Kept under the ``documents/`` prefix, which the bucket's lifecycle rule
    excludes, so these durable records never expire (unlike ``scratch/``).
    Returns count uploaded.

    Uploaded concurrently: a property can bring back ~90 documents, and these
    are independent network round-trips, so doing them one at a time put minutes
    of pure latency at the end of every job. A boto3 client is thread-safe for
    concurrent calls like this (it's the *resource* layer that isn't)."""
    s3 = aws.client("s3")
    bucket = aws.storage_bucket()
    uploadable = [p for p in files if p.is_file()]
    if not uploadable:
        return 0

    def put(path: Path) -> None:
        content_type, _ = mimetypes.guess_type(path.name)
        extra_args = {"ContentType": content_type} if content_type else {}
        key = f"{DOCUMENTS_PREFIX}/{prefix}/{path.name}"
        s3.upload_file(str(path), bucket, key, ExtraArgs=extra_args)

    with ThreadPoolExecutor(max_workers=_UPLOAD_WORKERS) as pool:
        list(pool.map(put, uploadable))  # `map` re-raises, so a failed upload still fails the job
    return len(uploadable)


THUMBNAILS_SUBPREFIX = ".thumbnails"


def upload_thumbnails(prefix: str, thumbnails: dict[str, bytes]) -> None:
    """Upload JPEG thumbnails for a subset of the documents at `prefix`, keyed
    by the document's own filename (see `worker.py`'s thumbnail generation
    step). Stored under ``documents/{prefix}/.thumbnails/{filename}.jpg`` —
    same durable prefix and retention as the documents themselves, but a
    leading-dot subpath `list_result_files()` explicitly skips so a thumbnail
    never shows up as a document of its own in the Results grid.

    Concurrent for the same reason as `upload_documents()` — one PUT per
    document, all independent."""
    if not thumbnails:
        return
    s3 = aws.client("s3")
    bucket = aws.storage_bucket()

    def put(item: tuple[str, bytes]) -> None:
        filename, jpeg_bytes = item
        key = f"{DOCUMENTS_PREFIX}/{prefix}/{THUMBNAILS_SUBPREFIX}/{filename}.jpg"
        s3.put_object(Bucket=bucket, Key=key, Body=jpeg_bytes, ContentType="image/jpeg")

    with ThreadPoolExecutor(max_workers=_UPLOAD_WORKERS) as pool:
        list(pool.map(put, thumbnails.items()))


DRAWING_SUBPREFIX = ".drawing"


def download_documents(prefix: str, dest: Path) -> list[Path]:
    """Copy a property's documents (not its thumbnails or drawings) from
    ``documents/{prefix}/`` into ``dest``. What a drawing job reads."""
    s3 = aws.client("s3")
    bucket = aws.storage_bucket()
    base = f"{DOCUMENTS_PREFIX}/{prefix}/"
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=base)
    keys = [
        obj["Key"]
        for page in pages
        for obj in page.get("Contents", [])
        if "/" not in obj["Key"][len(base) :]
    ]
    dest.mkdir(parents=True, exist_ok=True)

    def get(key: str) -> Path:
        path = dest / key.rsplit("/", 1)[-1]
        s3.download_file(bucket, key, str(path))
        return path

    with ThreadPoolExecutor(max_workers=_UPLOAD_WORKERS) as pool:
        return list(pool.map(get, keys))


EXTRACTIONS_PREFIX = "extractions"


def _extraction_key(fingerprint: str, reception: str) -> str:
    return f"{EXTRACTIONS_PREFIX}/{fingerprint}/{reception}.json"


def get_cached_extraction(fingerprint: str, reception: str) -> dict | None:
    """A previous run's citation-extraction result for this recorded document,
    or None if there isn't one.

    A recorded document is immutable once filed, so what it cites never changes
    and the answer is reusable forever — across re-runs of the same property
    (the Reprocess button) and across different parcels, since the recorder's
    section-wide searches return the same easements and plats for every parcel
    in a section. That matters because reading one is the single biggest cost in
    a run: no Weld recorder PDF has a text layer, so every one takes the Bedrock
    vision path at roughly two cents a document.

    `fingerprint` scopes the key to whatever would change the answer (the model
    and the tiling parameters), so tuning either doesn't silently serve results
    produced by the old settings — it just starts a new, empty namespace.

    Best-effort: any failure returns None and the caller re-extracts. A cache
    that's down must cost money, not correctness.
    """
    try:
        obj = aws.client("s3").get_object(
            Bucket=aws.storage_bucket(), Key=_extraction_key(fingerprint, reception)
        )
        return json.loads(obj["Body"].read())
    except Exception:  # noqa: BLE001 — a miss and an outage are the same to the caller
        return None


def put_cached_extraction(fingerprint: str, reception: str, payload: dict) -> None:
    """Store one extraction result for reuse. Best-effort; never raises.

    These are a few hundred bytes each and outlive the documents they describe —
    the `documents/` lifecycle rule doesn't cover this prefix, deliberately, so a
    re-run a month later still skips the model call even though the PDF itself
    has aged out and has to be re-downloaded.
    """
    try:
        aws.client("s3").put_object(
            Bucket=aws.storage_bucket(),
            Key=_extraction_key(fingerprint, reception),
            Body=json.dumps(payload).encode(),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not cache extraction for %s: %s", reception, exc)


def upload_map_image(job_id: str, path: Path) -> str | None:
    """Upload a scraper-captured property map screenshot and return a presigned
    URL for it. Stored under ``scratch/maps/`` — ephemeral per-job output, not
    a durable document, so the bucket's 90-day expiry rule (scoped to
    ``scratch/``) applies and it never shows up in the Results file list."""
    if not path.is_file():
        return None
    s3 = aws.client("s3")
    bucket = aws.storage_bucket()
    key = f"{SCRATCH_PREFIX}/maps/{job_id}.png"
    s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": "image/png"})
    return _presign(key)


def _make_browser_reachable(url: str) -> str:
    """Rewrite a presigned URL's host for the browser, if a public endpoint is
    configured (local dev only — LocalStack signs URLs with the docker-compose
    service hostname `localstack`, which a host-machine browser can't resolve)."""
    public = get_shared_settings().public_endpoint_url
    if not public:
        return url
    signed = urlsplit(url)
    target = urlsplit(public)
    return urlunsplit((target.scheme, target.netloc, signed.path, signed.query, signed.fragment))


def _presign(key: str, **params) -> str:
    url = aws.client("s3").generate_presigned_url(
        "get_object",
        Params={"Bucket": aws.storage_bucket(), "Key": key, **params},
        ExpiresIn=PRESIGN_TTL_SECONDS,
    )
    return _make_browser_reachable(url)


def list_result_files(prefix: str) -> list[dict]:
    """Return presigned download URLs for a property's documents (see
    ``Job.doc_prefix`` / ``upload_documents()``)."""
    base = f"{DOCUMENTS_PREFIX}/{prefix}/"
    thumbs = f"{base}{THUMBNAILS_SUBPREFIX}/"
    pages = (
        aws.client("s3")
        .get_paginator("list_objects_v2")
        .paginate(Bucket=aws.storage_bucket(), Prefix=base)
    )
    objects = [obj for page in pages for obj in page.get("Contents", [])]
    thumbnail_keys = {obj["Key"] for obj in objects if obj["Key"].startswith(thumbs)}
    files: list[dict] = []
    for obj in objects:
        key = obj["Key"]
        # Dot-prefixed subfolders (.thumbnails, a drawing job's .drawing) hold
        # derived files, never documents of the property itself.
        if "/." in key[len(base) - 1 :]:
            continue
        name = key.rsplit("/", 1)[-1]
        thumb_key = f"{thumbs}{name}.jpg"
        files.append(
            {
                "name": name,
                "size": obj.get("Size", 0),
                "url": _presign(key),
                # Same object, signed a second time with an attachment
                # disposition. The plain `url` has to stay inline — the Results
                # tab previews PDFs in an iframe — so the download button needs
                # its own URL. `filename` keeps S3's key out of the saved name.
                "downloadUrl": _presign(
                    key, ResponseContentDisposition=f'attachment; filename="{name}"'
                ),
                "thumbnailUrl": _presign(thumb_key) if thumb_key in thumbnail_keys else None,
            }
        )
    return files
