"""Application settings loaded from environment variables (or .env for local dev)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM provider selection.
    # Set LLM_PROVIDER to one of: openrouter, anthropic, nvidia, openai
    # If omitted, the first provider with a configured API key is used (openrouter → anthropic).
    llm_provider: str = ""

    # Model string — meaning depends on provider.
    # OpenRouter: "google/gemini-2.0-flash-exp:free", "meta/llama-3.3-70b-instruct:free", etc.
    # Anthropic:  "claude-sonnet-4-6", "claude-haiku-4-5", etc.
    # NVIDIA:     "meta/llama-3.3-70b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct", etc.
    model: str

    openrouter_api_key: str = ""
    anthropic_api_key: str = ""
    nvidia_api_key: str = ""

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
