"""Job state (DynamoDB) and result storage (S3) — shared by the API and worker.

The jobs table is a single-key table (partition key ``jobId``). Status transitions:
``PENDING`` (created by the API) → ``RUNNING`` → ``COMPLETED`` | ``FAILED`` (worker).
Result documents are stored in S3 under ``{jobId}/`` and surfaced to the UI as
presigned GET URLs by the API.
"""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from survey_shared import aws
from survey_shared.config import get_shared_settings

PENDING = "PENDING"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
TERMINAL = {COMPLETED, FAILED, CANCELLED}

PRESIGN_TTL_SECONDS = 3600


class Job(BaseModel):
    """A scrape job record. Field aliases are the DynamoDB item attribute names."""

    model_config = {"populate_by_name": True}

    job_id: str = Field(alias="jobId")
    address: str
    county: str
    status: str
    created_at: int = Field(alias="createdAt")
    updated_at: int = Field(alias="updatedAt")
    error: str | None = None
    file_count: int = Field(default=0, alias="fileCount")
    task_arn: str | None = Field(default=None, alias="taskArn")
    logs: list[str] = Field(default_factory=list)
    metadata: dict | None = None
    location: dict | None = None
    doc_prefix: str | None = Field(default=None, alias="docPrefix")

    def to_item(self) -> dict:
        # Alias keys for DynamoDB; drop error when unset rather than storing null.
        return self.model_dump(by_alias=True, exclude_none=True)

    @classmethod
    def from_item(cls, item: dict) -> Job:
        return cls.model_validate(item)


def _table():
    return aws.resource("dynamodb").Table(aws.jobs_table_name())


def create_job(job_id: str, address: str, county: str) -> Job:
    now = int(time.time())
    job = Job(
        job_id=job_id,
        address=address,
        county=county,
        status=PENDING,
        created_at=now,
        updated_at=now,
    )
    _table().put_item(Item=job.to_item())
    return job


def get_job(job_id: str) -> Job | None:
    resp = _table().get_item(Key={"jobId": job_id})
    item = resp.get("Item")
    return Job.from_item(item) if item else None


def list_jobs(limit: int = 100) -> list[Job]:
    """Most-recent-first job history for the sidebar. A single `scan` is fine
    at this table's size (single-user/small-team MVP, no query pattern beyond
    "recent jobs" yet) — switch to a GSI on a constant partition + createdAt
    sort key if this table ever grows large enough for scan cost to matter."""
    items = _table().scan().get("Items", [])
    jobs_ = [Job.from_item(item) for item in items]
    jobs_.sort(key=lambda j: j.created_at, reverse=True)
    return jobs_[:limit]


def update_status(
    job_id: str,
    status: str,
    *,
    error: str | None = None,
    file_count: int | None = None,
    metadata: dict | None = None,
    location: dict | None = None,
    doc_prefix: str | None = None,
) -> None:
    """Set status, unless the job was already cancelled — a cancel wins over a
    worker that finishes (or fails) after the user gave up on it."""
    expr = ["#s = :s", "updatedAt = :u"]
    names = {"#s": "status"}
    values: dict = {":s": status, ":u": int(time.time())}
    if error is not None:
        expr.append("#e = :e")
        names["#e"] = "error"
        values[":e"] = error
    if file_count is not None:
        expr.append("fileCount = :fc")
        values[":fc"] = file_count
    if metadata is not None:
        expr.append("metadata = :m")
        values[":m"] = metadata
    if location is not None:
        expr.append("#l2 = :loc")
        names["#l2"] = "location"
        values[":loc"] = location
    if doc_prefix is not None:
        expr.append("docPrefix = :dp")
        values[":dp"] = doc_prefix
    try:
        _table().update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET " + ", ".join(expr),
            ConditionExpression="attribute_not_exists(#s) OR #s <> :cancelled",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={**values, ":cancelled": CANCELLED},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def append_log(job_id: str, line: str) -> None:
    """Append one line to the job's running log (best-effort, non-fatal on error)."""
    try:
        _table().update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET #l = list_append(if_not_exists(#l, :empty), :line)",
            ExpressionAttributeNames={"#l": "logs"},
            ExpressionAttributeValues={":line": [line], ":empty": []},
        )
    except ClientError:
        pass


def set_task_arn(job_id: str, task_arn: str) -> None:
    _table().update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET taskArn = :t",
        ExpressionAttributeValues={":t": task_arn},
    )


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


DOCUMENTS_PREFIX = "documents"
SCRATCH_PREFIX = "scratch"


def upload_documents(prefix: str, files: list[Path]) -> int:
    """Upload downloaded county documents to the storage bucket under
    ``documents/{prefix}/`` (e.g. ``documents/co/weld/123_main_st/``, see
    worker.py's ``_doc_prefix()``) rather than a per-job scratch prefix — files
    for the same property land in the same place across repeated searches.
    Kept under the ``documents/`` prefix, which the bucket's lifecycle rule
    excludes, so these durable records never expire (unlike ``scratch/``).
    Returns count uploaded."""
    s3 = aws.client("s3")
    bucket = aws.storage_bucket()
    count = 0
    for path in files:
        if not path.is_file():
            continue
        key = f"{DOCUMENTS_PREFIX}/{prefix}/{path.name}"
        content_type, _ = mimetypes.guess_type(path.name)
        extra_args = {"ContentType": content_type} if content_type else {}
        s3.upload_file(str(path), bucket, key, ExtraArgs=extra_args)
        count += 1
    return count


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
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=PRESIGN_TTL_SECONDS
    )
    return _make_browser_reachable(url)


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


def list_result_files(prefix: str) -> list[dict]:
    """Return presigned download URLs for a property's documents (see
    ``Job.doc_prefix`` / ``upload_documents()``)."""
    s3 = aws.client("s3")
    bucket = aws.storage_bucket()
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{DOCUMENTS_PREFIX}/{prefix}/")
    files: list[dict] = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        name = key.rsplit("/", 1)[-1]
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGN_TTL_SECONDS,
        )
        # Same object, signed a second time with an attachment disposition. The
        # plain `url` has to stay inline — the Results tab previews PDFs in an
        # iframe — so the download button needs its own URL rather than a flag on
        # this one. `filename` keeps S3's key out of the saved file's name.
        download_url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{name}"',
            },
            ExpiresIn=PRESIGN_TTL_SECONDS,
        )
        files.append(
            {
                "name": name,
                "size": obj.get("Size", 0),
                "url": _make_browser_reachable(url),
                "downloadUrl": _make_browser_reachable(download_url),
            }
        )
    return files
