"""All-in cost estimate for one job run.

Every AWS service a run touches, priced from quantities the worker actually
measured, so the Run Details tab can show where the money went rather than just
the two biggest line items.

**Why estimates and not billed usage.** There is deliberately no Cost Explorer /
CUR integration here: tag-based cost allocation reports lag 24-48 hours, which
can't back a number shown the moment a job finishes. So each line multiplies a
measured quantity (tokens, seconds, bytes, request counts) by a published
on-demand rate. Two consequences worth knowing:

* Only `bedrock` is a *real* dollar figure — it comes from the model provider's
  own usage accounting. Everything else is this module's arithmetic, and each
  line says which it is via `CostLine.basis`.
* The rates below are **us-west-2** (`infra/deploy.py`'s `DEFAULT_REGION`) and
  were read from the AWS Pricing API on 2026-09-17. Deploying elsewhere, or a
  price change, means editing them here — nothing re-derives them. Note
  DynamoDB's on-demand rates in this region ($0.625/M write, $0.125/M read) are
  half the widely-quoted us-east-1 figures; don't "correct" them back.

**What this is for.** On a real Weld property the answer is lopsided: Bedrock is
~98% of the run and everything except Fargate rounds to well under a cent
combined. That is the point of itemising it — it stops anyone optimising S3 PUTs
while the model bill goes unwatched.
"""

from __future__ import annotations

import math

# --- Rates: us-west-2, on-demand, USD. See the module docstring before editing. --- #

_S3_STORAGE_GB_MONTH = 0.023
_S3_PUT_PER_1K = 0.005
_S3_GET_PER_1K = 0.0004

_DDB_WRITE_PER_UNIT = 0.625 / 1_000_000
_DDB_READ_PER_UNIT = 0.125 / 1_000_000

_LAMBDA_PER_REQUEST = 0.20 / 1_000_000
_LAMBDA_PER_GB_SECOND = 0.0000166667

_APIGW_HTTP_PER_REQUEST = 1.00 / 1_000_000
_SQS_PER_REQUEST = 0.40 / 1_000_000
_CLOUDWATCH_LOGS_PER_GB = 0.50

_FARGATE_VCPU_HOUR = 0.04048
_FARGATE_GB_HOUR = 0.004445

# --- Fixed facts about how this app is deployed --- #

# WorkerTaskDefinition in infra/cloudformation/backend.yaml: Cpu '1024', Memory '2048'.
# Change those and these must change with them.
_FARGATE_VCPUS = 1
_FARGATE_MEM_GB = 2

# The `documents/` lifecycle rule in backend.yaml, kept in sync with JobsTable's TTL.
# A run's files are billed for storage only until they expire.
_DOCUMENT_RETENTION_DAYS = 7

# ResultsPage.tsx polls GET /api/jobs/{id} on this interval while a job is live.
# Each poll is one API Gateway request, one Lambda invoke, and one DynamoDB read —
# small individually, but a long run makes thousands of them, so they get a line.
_POLL_INTERVAL_S = 1.5
_API_LAMBDA_MEM_GB = 0.5
_API_LAMBDA_SECONDS = 0.1

# Floor for a DynamoDB job item before any logs accumulate (address, status,
# timestamps, cost fields). Rough, and it barely matters — one write unit is
# $0.000000625.
_DDB_BASE_ITEM_BYTES = 1_500


class CostLine:
    """One row of the Run Details cost table."""

    __slots__ = ("key", "label", "usd", "detail", "basis")

    def __init__(self, key: str, label: str, usd: float, detail: str, basis: str) -> None:
        self.key = key
        self.label = label
        self.usd = usd
        self.detail = detail
        self.basis = basis

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "usd": round(self.usd, 6),
            "detail": self.detail,
            "basis": self.basis,
        }


def _ddb_write_units(appends: int, final_log_bytes: int) -> float:
    """Write units an incrementally-grown job item costs.

    DynamoDB charges a full item rewrite per `list_append` update, and the item
    grows as logs accumulate — so the Nth append costs more than the first. The
    item is about `base + log_bytes * i/n` on append *i*, which averages out to
    half the final log size.
    """
    if appends <= 0:
        return 0.0
    average_bytes = _DDB_BASE_ITEM_BYTES + final_log_bytes / 2
    return appends * math.ceil(average_bytes / 1024)


def estimate(
    *,
    elapsed_s: float,
    bedrock_usd: float,
    input_tokens: int,
    output_tokens: int,
    document_bytes: int,
    document_count: int,
    log_appends: int,
    log_bytes: int,
) -> list[dict]:
    """Every cost line for one run, biggest first. Returns plain dicts for
    `jobs.update_status(costs=...)`.

    The caller supplies only things it measured directly; everything derived
    (poll counts, write units, storage GB-months) happens here so the
    assumptions live in one file next to the rates they multiply.
    """
    hours = elapsed_s / 3600
    gb_stored = document_bytes / 1_000_000_000
    # Documents and their thumbnails are one PUT each, plus the metadata blob
    # and the archived job log.
    s3_puts = document_count * 2 + 2
    polls = elapsed_s / _POLL_INTERVAL_S

    fargate = hours * (_FARGATE_VCPUS * _FARGATE_VCPU_HOUR + _FARGATE_MEM_GB * _FARGATE_GB_HOUR)

    s3_storage = gb_stored * _S3_STORAGE_GB_MONTH * (_DOCUMENT_RETENTION_DAYS / 30)
    s3_requests = (s3_puts / 1000) * _S3_PUT_PER_1K + (document_count / 1000) * _S3_GET_PER_1K
    s3 = s3_storage + s3_requests

    write_units = _ddb_write_units(log_appends, log_bytes)
    # Polls read the whole item; eventually-consistent reads are 0.5 units per 4KB.
    read_units = polls * max(1.0, (_DDB_BASE_ITEM_BYTES + log_bytes) / 4096) * 0.5
    dynamodb = write_units * _DDB_WRITE_PER_UNIT + read_units * _DDB_READ_PER_UNIT

    # The dispatcher Lambda fires once; the API Lambda fires once per poll.
    lambda_invokes = polls + 1
    api = (
        lambda_invokes * _LAMBDA_PER_REQUEST
        + lambda_invokes * _API_LAMBDA_SECONDS * _API_LAMBDA_MEM_GB * _LAMBDA_PER_GB_SECOND
        + polls * _APIGW_HTTP_PER_REQUEST
        + 2 * _SQS_PER_REQUEST  # one send, one receive
    )

    cloudwatch = (log_bytes / 1_000_000_000) * _CLOUDWATCH_LOGS_PER_GB

    lines = [
        CostLine(
            "bedrock",
            "Bedrock / LLM",
            bedrock_usd,
            f"{input_tokens:,} in / {output_tokens:,} out tokens",
            "measured",
        ),
        CostLine(
            "fargate",
            "ECS / Fargate",
            fargate,
            f"{elapsed_s:,.0f}s x {_FARGATE_VCPUS} vCPU / {_FARGATE_MEM_GB} GB",
            "estimated",
        ),
        CostLine(
            "s3",
            "S3 (documents)",
            s3,
            f"{document_count:,} files, {gb_stored * 1000:,.1f} MB "
            f"for {_DOCUMENT_RETENTION_DAYS}d + {s3_puts:,} requests",
            "estimated",
        ),
        CostLine(
            "dynamodb",
            "DynamoDB (job record)",
            dynamodb,
            f"{write_units:,.0f} write / {read_units:,.0f} read units",
            "estimated",
        ),
        CostLine(
            "api",
            "API Gateway + Lambda + SQS",
            api,
            f"{polls:,.0f} status polls while running",
            "estimated",
        ),
        CostLine(
            "cloudwatch",
            "CloudWatch Logs",
            cloudwatch,
            f"{log_bytes / 1_000_000:,.1f} MB ingested",
            "estimated",
        ),
    ]
    lines.sort(key=lambda line: line.usd, reverse=True)
    return [line.as_dict() for line in lines]
