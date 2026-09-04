"""Tests for pipeline county dispatch logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from land_survey_scraper.geocode import County, GeocodedAddress
from land_survey_scraper.pipeline import COUNTY_SCRAPERS, run_async


def _geocoded(county_name: str, state: str = "CO") -> GeocodedAddress:
    return GeocodedAddress(
        street="123 Main St",
        city="Anytown",
        state=state,
        zip_code="80000",
        county=County(state=state, name=county_name),
    )


@pytest.mark.parametrize(
    "county_name,expected_key",
    [
        ("Weld", "CO_weld"),
        ("Denver", "CO_denver"),
        ("Arapahoe", "CO_arapahoe"),
        ("Jefferson", "CO_jefferson"),
    ],
)
def test_county_scrapers_registered(county_name: str, expected_key: str) -> None:
    assert expected_key in COUNTY_SCRAPERS


@pytest.mark.asyncio
async def test_run_async_dispatches_to_correct_scraper() -> None:
    mock_scrape = AsyncMock(return_value=([], None))
    with (
        patch("land_survey_scraper.pipeline.address_to_county") as mock_geocode,
        patch.dict("land_survey_scraper.pipeline.COUNTY_SCRAPERS", {"CO_weld": mock_scrape}),
    ):
        mock_geocode.return_value = _geocoded("Weld")
        saved, err = await run_async("123 Main St, Greeley, CO 80631", quiet=True)

    mock_scrape.assert_awaited_once()
    assert err is None


@pytest.mark.asyncio
async def test_run_async_unsupported_county_returns_error() -> None:
    with patch("land_survey_scraper.pipeline.address_to_county") as mock_geocode:
        mock_geocode.return_value = _geocoded("Boulder")
        saved, err = await run_async("123 Main St, Boulder, CO 80302", quiet=True)

    assert saved == []
    assert err is not None
    assert "not yet supported" in err


@pytest.mark.asyncio
async def test_run_async_geocode_failure_returns_error() -> None:
    with patch("land_survey_scraper.pipeline.address_to_county", return_value=None):
        saved, err = await run_async("bad address", quiet=True)

    assert saved == []
    assert "Could not resolve" in (err or "")


@pytest.mark.asyncio
async def test_run_async_county_override_bypasses_geocoding() -> None:
    mock_scrape = AsyncMock(return_value=([], None))
    with (
        patch("land_survey_scraper.pipeline.address_to_county") as mock_geocode,
        patch.dict("land_survey_scraper.pipeline.COUNTY_SCRAPERS", {"CO_weld": mock_scrape}),
    ):
        mock_geocode.return_value = _geocoded("Denver")  # geocode says Denver
        saved, err = await run_async(
            "123 Main St, Denver, CO 80202",
            quiet=True,
            county_override="CO_weld",  # but override forces Weld
        )

    mock_scrape.assert_awaited_once()
