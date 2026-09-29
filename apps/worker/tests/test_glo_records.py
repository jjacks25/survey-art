"""Tests for glo_records — the Township/Range parsing GLO's search form needs."""

from __future__ import annotations

from survey_art.scrapers.glo_records import _split_township_or_range


def test_splits_sop_form_township_and_range():
    assert _split_township_or_range("5N") == ("5", "N")
    assert _split_township_or_range("67W") == ("67", "W")


def test_handles_whitespace_and_lowercase():
    assert _split_township_or_range(" 5 n ") == ("5", "N")


def test_falls_back_to_raw_value_when_unparseable():
    assert _split_township_or_range("") == ("", "")
    assert _split_township_or_range("garbage") == ("garbage", "")
