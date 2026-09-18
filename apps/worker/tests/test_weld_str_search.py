"""Tests for _section_township_range_search — the S/T/R "everything else
recorded in this section" scan (distinct from the easement/ROW-only scan) —
and for the date sweep that gets it past the recorder's 100-row render cap."""

from __future__ import annotations

import pytest

from survey_art.scrapers.weld_county import (
    _RESULT_ROW_CAP,
    ParcelInfo,
    _run_advanced_search,
    _search_all_rows,
    _section_township_range_search,
)
from survey_art.settings import get_settings


class _FakePage:
    """Stands in for the Playwright page driven by _run_advanced_search.

    `rows` is either a flat list (returned for every search) or a callable
    taking the filled-in form, so a test can vary results by date window.
    """

    def __init__(self, rows):
        self._rows = rows
        self.form: dict[str, str] = {}
        self.searches: list[dict[str, str]] = []
        self.clicks: list[str] = []

    async def goto(self, *a, **k):
        pass

    async def click(self, *a, **k):
        if a and a[0] == "button:has-text('Yes - Continue')":
            raise Exception("no dialog")
        self.clicks.append(a[0] if a else "")

    async def fill(self, selector, value, *a, **k):
        self.form[selector] = value

    async def wait_for_load_state(self, *a, **k):
        pass

    async def wait_for_timeout(self, *a, **k):
        pass

    async def evaluate(self, *a, **k):
        self.searches.append(dict(self.form))
        return self._rows(self.form) if callable(self._rows) else self._rows


def _rows(*receptions: str, doc_type: str = "WARRANTY DEED") -> list[dict]:
    return [
        {"reception": r, "doc_type": doc_type, "rec_date": "01/01/2020", "doc_id": ""}
        for r in receptions
    ]


@pytest.mark.asyncio
async def test_returns_unseen_rows_tagged_with_str_role():
    parcel = ParcelInfo(account="R123", section="15", township="5N", range_="67W")
    page = _FakePage(
        [
            {"reception": "1111", "doc_type": "WARRANTY DEED", "rec_date": "01/01/2020"},
            {"reception": "2222", "doc_type": "LIEN", "rec_date": "02/02/2021"},
        ]
    )
    seen: set[str] = {"2222"}  # already downloaded via another route

    targets, index = await _section_township_range_search(page, parcel, seen)

    assert [(role, doc.reception) for role, doc in targets] == [
        ("section_township_range_search", "1111")
    ]
    assert seen == {"1111", "2222"}
    # The index reports what the section holds, including rows other routes took.
    assert [r["reception"] for r in index] == ["1111", "2222"]


@pytest.mark.asyncio
async def test_downloads_the_survey_relevant_end_of_a_big_section_first():
    """The cap decides what a run gives up, so it must give up financing paper.

    S32-T5N-R65W has 872 documents and 289 of them are deeds of trust; keeping
    those instead of the section's 8 surveys would be the wrong 250.
    """
    parcel = ParcelInfo(account="R123", section="15", township="5N", range_="67W")
    page = _FakePage(
        _rows("1", "2", doc_type="DEED OF TRUST")
        + _rows("3", doc_type="SURVEY")
        + _rows("4", doc_type="RIGHT OF WAY EASEMENT")
        + _rows("5", doc_type="WARRANTY DEED")
    )
    settings = get_settings()
    original = settings.weld_section_download_limit
    settings.weld_section_download_limit = 3
    try:
        targets, index = await _section_township_range_search(page, parcel, set())
    finally:
        settings.weld_section_download_limit = original

    assert [doc.reception for _, doc in targets] == ["3", "4", "5"]
    assert len(index) == 5  # nothing is hidden from the metadata


@pytest.mark.asyncio
async def test_a_capped_search_is_split_by_date_until_it_is_not():
    """One search returns at most 100 rows and there is no next page, so a
    section with more than that needs slicing by recording date."""
    calls: list[tuple[str, str]] = []

    def rows_for(form: dict[str, str]) -> list[dict]:
        calls.append(
            (
                form["#field_RecordingDateID_DOT_StartDate"],
                form["#field_RecordingDateID_DOT_EndDate"],
            )
        )
        if len(calls) == 1:  # the whole 1865-to-today window comes back capped
            return _rows(*(str(i) for i in range(_RESULT_ROW_CAP)))
        return _rows(f"row{len(calls)}")

    page = _FakePage(rows_for)

    found = await _search_all_rows(page, section="15")

    assert len(calls) == 3  # the capped whole, then each half
    assert calls[0][0] == "01/01/1865"  # the recorder's certified-from date
    # The halves cover the whole window between them, and the rows kept are the
    # ones that came back from under the cap.
    assert calls[1][0] == calls[0][0] and calls[2][1] == calls[0][1]
    assert [r["reception"] for r in found] == ["row2", "row3"]


@pytest.mark.asyncio
async def test_each_search_clears_the_one_before_it():
    """Criteria live on the server, not in the form, so every search has to
    clear the last one — blank inputs are not enough. Measured: a section search
    after a name search returns 4 rows without the clear and 100 with it."""
    page = _FakePage([])

    await _run_advanced_search(page, search_name="PETROLEUM EXPLORATION & MANAGEMENT LLC")
    await _run_advanced_search(page, section="32", township="5N", range_="65W")

    clears = [c for c in page.clicks if "Clear Selections" in c]
    assert len(clears) == 2  # one per search, before anything is filled in
    assert page.form == {
        "#field_PLSSLegalID_DOT_Section": "32",
        "#field_PLSSLegalID_DOT_Township": "5",
        "#field_PLSSLegalID_DOT_Range": "65",
        "#field_PlattedLegalID_DOT_Subdivision": "",
        "#field_BothNamesID": "",
        "#field_RecordingDateID_DOT_StartDate": "",
        "#field_RecordingDateID_DOT_EndDate": "",
    }


@pytest.mark.asyncio
async def test_skips_search_when_str_incomplete():
    parcel = ParcelInfo(account="R123")
    assert await _section_township_range_search(_FakePage([]), parcel, set()) == ([], [])
