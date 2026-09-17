"""Application settings loaded from environment variables (or .env for local dev).

County portal credentials come from AWS Secrets Manager, fetched directly by the app
at startup via pydantic-settings' `AWSSecretsManagerSettingsSource` — not injected as
container env vars by ECS. `APP_CONFIG_SECRET_ID` (a plain, non-secret env var set by
the backend stack to the secret's ARN) tells us which secret to fetch; local dev leaves
it unset, so the source is skipped and the plaintext `.env` defaults below apply as
normal. See infra/AGENTS.md's Secrets Manager gotcha for the CloudFormation side.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import (
    AWSSecretsManagerSettingsSource,
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Bedrock model ID or cross-region inference profile ID, e.g.
    # "us.anthropic.claude-haiku-4-5-20251001-v1:0". This is still the knob for
    # switching models — only the provider is hardcoded, not the model.
    model: str

    # Model that reads a scanned survey PDF for the record IDs it references
    # (`id_extraction.py`). Deliberately separate from `model` above: that one
    # drives the browser-use agents, this one does a single bounded document
    # read, and they have no reason to move together.
    id_extraction_model: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    # "demo" caps how many of the ALTA's referenced documents Step 3A.5 downloads
    # (`_DEMO_EXCEPTION_LIMIT` in scrapers/weld_county.py), so a demo run finishes in
    # a couple of minutes instead of ~30. Every ID still lands in overview.json's
    # `extracted_ids` — only the fetching is capped. Anything else means no cap.
    application_mode: str = "regular"

    # Bedrock uses IAM (the Fargate task role, or local `aws sso login`/profile
    # credentials via boto3's default chain) — no API key. Only the region is needed.
    aws_region: str = "us-west-2"

    # Weld County Clerk & Recorder portal (recording.weld.gov) — free registration required
    # Register at https://recording.weld.gov then set these in .env
    weld_recorder_username: str = ""
    weld_recorder_password: str = ""

    # Legacy eRecording credentials (old Java system — no longer used)
    weld_erecording_username: str = ""
    weld_erecording_password: str = ""

    # Denver County Clerk & Recorder (Kofile Tech) login
    co_denver_username: str = ""
    co_denver_password: str = ""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Earlier sources win, so a real env var/`.env` entry still overrides the
        # secret (handy for local testing against real creds without touching AWS).
        sources: tuple[PydanticBaseSettingsSource, ...] = (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
        secret_id = os.environ.get("APP_CONFIG_SECRET_ID", "")
        if secret_id:
            secrets_settings = AWSSecretsManagerSettingsSource(settings_cls, secret_id)
            sources = (
                init_settings,
                env_settings,
                dotenv_settings,
                secrets_settings,
                file_secret_settings,
            )
        return sources


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings instance. Fails fast if required env vars are missing."""
    return Settings()
