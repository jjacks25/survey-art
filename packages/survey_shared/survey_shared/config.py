"""Shared runtime configuration (pydantic-settings) for the API and worker.

Fields are read from environment variables (or a local ``.env``). Names match the
values the CloudFormation backend stack injects into the Lambda/Fargate task and
the docker-compose local stack. Consumers call the accessor properties, which
fail fast with a clear error if a required value is missing for that component.

The accessor is named ``get_shared_settings()``, not ``get_settings()``, because the
worker imports it alongside ``survey_art.settings.get_settings`` (scraper/LLM config and
county portal credentials) — two same-named accessors returning different objects is a
mis-import waiting to happen.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class SharedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Deployment environment. Same values as the CloudFormation templates'
    # `Environment` parameter (see infra/AGENTS.md) — Literal so a typo fails at
    # startup rather than silently reading as some third environment.
    environment: Literal["dev", "prod"] = "dev"

    # AWS wiring. AWS_ENDPOINT_URL is set only for local dev against LocalStack.
    aws_region: str = "us-west-2"
    aws_endpoint_url: str = ""
    # Only set locally: the browser-reachable form of aws_endpoint_url (e.g.
    # http://localhost:4566). LocalStack signs presigned S3 URLs with whatever
    # host the client used to talk to it — inside docker-compose that's the
    # `localstack` service hostname, which the browser can't resolve. Presigned
    # URLs get rewritten to this host before being returned to the frontend.
    aws_public_endpoint_url: str = ""

    # Resource names injected by the backend stack; optional here because not every
    # component needs all of them (the worker doesn't send to SQS, etc.).
    storage_bucket: str = ""
    jobs_table: str = ""
    job_queue_url: str = ""
    cluster_arn: str = ""

    @property
    def endpoint_url(self) -> str | None:
        return self.aws_endpoint_url or None

    @property
    def public_endpoint_url(self) -> str | None:
        return self.aws_public_endpoint_url or None

    def require_storage_bucket(self) -> str:
        if not self.storage_bucket:
            raise RuntimeError("STORAGE_BUCKET is not set")
        return self.storage_bucket

    def require_jobs_table(self) -> str:
        if not self.jobs_table:
            raise RuntimeError("JOBS_TABLE is not set")
        return self.jobs_table

    def require_job_queue_url(self) -> str:
        if not self.job_queue_url:
            raise RuntimeError("JOB_QUEUE_URL is not set")
        return self.job_queue_url

    def require_cluster_arn(self) -> str:
        if not self.cluster_arn:
            raise RuntimeError("CLUSTER_ARN is not set")
        return self.cluster_arn


@lru_cache(maxsize=1)
def get_shared_settings() -> SharedSettings:
    return SharedSettings()
