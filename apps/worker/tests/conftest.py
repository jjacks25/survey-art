"""Test configuration.

Set harmless defaults for the required app settings so the suite is hermetic and
does not depend on a developer's local `.env`. `Settings.model` (MODEL) has no
default and is read via `get_settings()` in the pipeline; provide a dummy here so
dispatch tests that mock the scrapers don't fail at settings construction.
"""

from __future__ import annotations

import os

os.environ.setdefault("MODEL", "test-model")
# Pin the mode too: a developer's .env may say `demo`, which caps downloads.
os.environ.setdefault("APPLICATION_MODE", "regular")
os.environ.setdefault("LLM_PROVIDER", "openrouter")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

import pytest  # noqa: E402

from survey_art.scrapers import weld_county  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_weld_sweep_cache():
    """`scrape()` clears this per run; tests that call the search helpers
    directly would otherwise see an earlier test's fake recorder rows."""
    weld_county._sweep_cache.clear()
