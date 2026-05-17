"""Tests for land_survey_scraper package."""

from land_survey_scraper import __version__


def test_version() -> None:
    """Package has a version."""
    assert __version__ == "0.1.0"
