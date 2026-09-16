"""Tests for the owner-name search's pure-logic filters (S/T/R exemption packet)."""

from __future__ import annotations

from survey_art.scrapers.weld_county import (
    _date_sort_key,
    _matches_exemption_filter,
    _matches_survey_filter,
    _matches_vesting_deed_label,
)


def test_date_sort_key_handles_both_formats():
    # Document History uses MM-DD-YYYY; Advanced Search rows use MM/DD/YYYY with a time suffix.
    assert _date_sort_key("07-09-2024") == (2024, 7, 9)
    assert _date_sort_key("07/09/2024 02:04 PM") == (2024, 7, 9)
    assert _date_sort_key("") == (0, 0, 0)
    assert _date_sort_key("garbage") == (0, 0, 0)


def test_date_sort_key_orders_most_recent_last():
    dates = ["01-01-2020", "07/09/2024 02:04 PM", "12-31-2019"]
    assert max(dates, key=_date_sort_key) == "07/09/2024 02:04 PM"


def test_matches_vesting_deed_label():
    assert _matches_vesting_deed_label("QUIT CLAIM DEED")
    assert _matches_vesting_deed_label("special warranty deed  ")
    assert not _matches_vesting_deed_label("DEED OF TRUST")


def test_matches_exemption_filter():
    assert _matches_exemption_filter("SUBDIVISION EXEMPTION")
    assert _matches_exemption_filter("AMENDED EXEMPTION")
    assert not _matches_exemption_filter("WARRANTY DEED")


def test_matches_survey_filter():
    assert _matches_survey_filter("ALTA SURVEY")
    assert _matches_survey_filter("AMENDED SURVEY")
    assert not _matches_survey_filter("EASEMENT")
