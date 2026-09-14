"""Shared AWS client/config helpers for the API, worker, and dispatcher.

All AWS access uses IAM role credentials at runtime (Fargate task role / Lambda
execution role) — never static access keys. For local development against
LocalStack, set ``AWS_ENDPOINT_URL`` (e.g. ``http://localstack:4566``) and dummy
``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` values; the clients below honour
the endpoint override so the same code runs locally and in AWS unchanged.

Configuration is sourced from :mod:`survey_shared.config` (pydantic-settings).
"""

from __future__ import annotations

from functools import cache
from typing import Any

import boto3

from survey_shared.config import get_shared_settings


@cache
def client(service: str) -> Any:
    """Return a cached boto3 client, endpoint-aware for LocalStack."""
    s = get_shared_settings()
    return boto3.client(service, region_name=s.aws_region, endpoint_url=s.endpoint_url)


@cache
def resource(service: str) -> Any:
    """Return a cached boto3 resource, endpoint-aware for LocalStack."""
    s = get_shared_settings()
    return boto3.resource(service, region_name=s.aws_region, endpoint_url=s.endpoint_url)


def storage_bucket() -> str:
    return get_shared_settings().require_storage_bucket()


def jobs_table_name() -> str:
    return get_shared_settings().require_jobs_table()
