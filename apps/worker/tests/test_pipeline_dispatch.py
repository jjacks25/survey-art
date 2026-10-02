"""Tests for pipeline county dispatch. The happy path (account number + county
override, no geocode) runs end to end in test_end_to_end.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from survey_art.geocode import County, GeocodedAddress
from survey_art.pipeline import run_async


async def _run(address: str, geocoded_county: str | None, **kwargs):
    """`run_async` with geocoding stubbed to `geocoded_county` (None = no match)
    and Weld's scraper mocked. Returns `(weld_scrape_mock, run_async result)`."""
    geocoded = geocoded_county and GeocodedAddress(
        street="123 Main St",
        city="Anytown",
        state="CO",
        zip_code="80000",
        county=County(state="CO", name=geocoded_county),
    )
    scrape = AsyncMock(return_value=([], None, 0.0, 0, 0))
    with (
        patch("survey_art.pipeline.address_to_county", return_value=geocoded),
        patch.dict("survey_art.pipeline.COUNTY_SCRAPERS", {"CO_weld": scrape}),
    ):
        return scrape, await run_async(address, quiet=True, **kwargs)


async def test_unsupported_county_returns_error() -> None:
    _, (saved, err, *_) = await _run("123 Main St, Boulder, CO 80302", "Boulder")
    assert saved == []
    assert "not yet supported" in err


async def test_geocode_failure_returns_error() -> None:
    _, (saved, err, *_) = await _run("bad address", None)
    assert saved == []
    assert "Could not resolve" in err


async def test_county_override_beats_the_geocoded_county() -> None:
    scrape, (_, err, *_) = await _run(
        "123 Main St, Denver, CO 80202", "Denver", county_override="CO_weld"
    )
    scrape.assert_awaited_once()
    assert err is None
