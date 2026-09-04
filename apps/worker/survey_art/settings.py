"""Application settings loaded from environment variables (or .env for local dev)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings instance. Fails fast if required env vars are missing."""
    return Settings()
