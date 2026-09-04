"""Tests for survey_art package."""

from survey_art import __version__


def test_version() -> None:
    """Package has a version."""
    assert __version__ == "0.1.0"
