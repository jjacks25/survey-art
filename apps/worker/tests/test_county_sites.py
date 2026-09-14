"""Tests for county_sites registry."""

from __future__ import annotations

import pytest

from survey_art.county_sites import get_site, list_supported
from survey_art.geocode import County


@pytest.mark.parametrize(
    "state,name",
    [
        ("CO", "Weld"),
        ("CO", "Denver"),
        ("CO", "Arapahoe"),
        ("CO", "Jefferson"),
    ],
)
def test_supported_counties_have_site_entry(state: str, name: str) -> None:
    site = get_site(County(state=state, name=name))
    assert site is not None
    assert "urls" in site
    assert site["scraper_key"]


def test_unsupported_county_returns_none() -> None:
    assert get_site(County(state="CO", name="Boulder")) is None


def test_list_supported_includes_all_counties() -> None:
    supported = list_supported()
    assert "Weld, CO" in supported
    assert "Denver, CO" in supported
    assert "Arapahoe, CO" in supported
    assert "Jefferson, CO" in supported
