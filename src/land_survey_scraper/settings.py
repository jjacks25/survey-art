"""Application settings loaded from environment variables (or .env for local dev)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM — set MODEL in .env to any OpenRouter or Anthropic model string.
    # If OPENROUTER_API_KEY is set, the model is served via OpenRouter (supports free models).
    # Otherwise falls back to Anthropic directly using ANTHROPIC_API_KEY.
    model: str
    openrouter_api_key: str = ""
    anthropic_api_key: str = ""

    # Weld County eRecording shared surveyor login (only required for Weld County scraping)
    weld_erecording_username: str = ""
    weld_erecording_password: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings instance. Fails fast if required env vars are missing."""
    return Settings()
