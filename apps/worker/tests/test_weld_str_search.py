"""Tests for _section_township_range_search — the S/T/R "everything else
recorded in this section" scan (distinct from the easement/ROW-only scan) —
and for the date sweep that gets it past the recorder's 100-row render cap."""

from __future__ import annotations

import pytest

from survey_art.id_extraction import ExtractedId
from survey_art.scrapers.weld_county import (
    _RESULT_ROW_CAP,
    ParcelInfo,
    _resolve_book_page_citations,
    _run_advanced_search,
    _search_all_rows,
    _section_township_range_search,
)

from .fakes import FakeSearchPage


def _rows(*receptions: str, doc_type: str = "WARRANTY DEED") -> list[dict]:
    return [
        {"reception": r, "doc_type": doc_type, "rec_date": "01/01/2020", "doc_id": ""}
        for r in receptions
    ]


@pytest.mark.asyncio
async def test_keeps_what_a_title_commitment_lists_and_drops_the_rest():
    """The PLS's document types, plus ROW takes recorded as warranty deeds —
    not financing, not the chain's sales of land, not another township's paper."""
    owner = "PETROLEUM EXPLORATION & MANAGEMENT LLC"
    parcel = ParcelInfo(account="R8995911", owner=owner, section="32", township="5N", range_="65W")
    s32 = ["Section: 32 Township: 5 Range: 65"]
    page = FakeSearchPage(
        [
            {
                "reception": "4508544",
                "doc_type": "WARRANTY DEED",
                "rec_date": "07/25/2019",
                "grantors": [owner],
                "grantees": ["WELD CO"],
                "legals": s32,
            },
            {
                "reception": "1564302",
                "doc_type": "OIL & GAS LEASE",
                "rec_date": "03/23/1971",
                "grantors": [owner],
                "grantees": ["HOOVLER PAUL V"],
                "legals": [],
            },
            {
                "reception": "4744164",
                "doc_type": "MINERAL DEED",
                "rec_date": "08/09/2021",
                "grantors": [owner],
                "grantees": ["HOLLOWAY EDWARD A"],
                "legals": ["Section: 19 Township: 3 Range: 66"],
            },
            {
                "reception": "1015046",
                "doc_type": "WARRANTY DEED",
                "rec_date": "09/29/1947",
                "grantors": [owner],
                "grantees": ["BEJARANO NAVOR"],
                "legals": [],
            },
            {
                "reception": "9",
                "doc_type": "DEED OF TRUST",
                "rec_date": "01/01/2020",
                "grantors": [owner],
                "grantees": ["NBH BANK"],
                "legals": s32,
            },
            {"reception": "1111", "doc_type": "PLAT", "rec_date": "01/01/2000", "legals": s32},
        ]
    )
    seen: set[str] = {"1111"}  # already downloaded via another route

    targets, index = await _section_township_range_search(page, parcel, seen)

    assert sorted(doc.reception for _, doc in targets) == ["1564302", "4508544"]
    assert seen == {"1111", "1564302", "4508544"}
    assert len(index) == 6  # everything found is listed, pulled or not
    # County-wide, not bounded by section, and searched without the LLC suffix.
    assert page.searches[0]["#field_BothNamesID"] == "PETROLEUM EXPLORATION & MANAGEMENT"
    assert page.searches[0]["#field_PLSSLegalID_DOT_Section"] == ""


@pytest.mark.asyncio
async def test_walks_the_chain_of_title_back_through_deed_grantors():
    """A deed into the owner names the one before as grantor, who is searched
    next — here through a deed indexed to the neighbouring section, as
    R8995911's own vesting deed is. A deed into the owner from somewhere else
    is not followed."""
    by_name = {
        "PETROLEUM EXPLORATION & MANAGEMENT": [
            {
                "reception": "4372901",
                "doc_type": "WARRANTY DEED",
                "rec_date": "02/02/2018",
                "grantors": ["THURMAN HAYS & CO LLP"],
                "grantees": ["PETROLEUM EXPLORATION & MANAGEMENT LLC"],
                "legals": ["Section: 31 Township: 5 Range: 65"],
            },
            {
                "reception": "4487834",
                "doc_type": "WARRANTY DEED",
                "rec_date": "05/09/2019",
                "grantors": ["INCLINE MINERALS LLC"],
                "grantees": ["PETROLEUM EXPLORATION & MANAGEMENT LLC"],
                "legals": ["Section: 18 Township: 4 Range: 66"],
            },
        ],
        "THURMAN HAYS & CO": [
            {
                "reception": "1138328",
                "doc_type": "RIGHT OF WAY",
                "rec_date": "09/15/1952",
                "grantors": ["THURMAN HAYS & CO"],
                "grantees": ["WELD CO COLORADO"],
                "legals": [],
            },
        ],
    }
    owner = "PETROLEUM EXPLORATION & MANAGEMENT LLC"
    parcel = ParcelInfo(account="R1", owner=owner, section="32", township="5N", range_="65W")
    page = FakeSearchPage(lambda form: by_name.get(form["#field_BothNamesID"], []))

    targets, _ = await _section_township_range_search(page, parcel, set())

    # Each chain owner by name, then the section on its own.
    assert [s["#field_BothNamesID"] for s in page.searches] == [*by_name, ""]
    assert page.searches[-1]["#field_PLSSLegalID_DOT_Section"] == "32"
    assert [doc.reception for _, doc in targets] == ["1138328"]


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

    page = FakeSearchPage(rows_for)

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
    page = FakeSearchPage([])

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
        "#field_BookPageID_DOT_Book": "",
        "#field_BookPageID_DOT_Page": "",
        "#field_RecordingDateID_DOT_StartDate": "",
        "#field_RecordingDateID_DOT_EndDate": "",
    }


@pytest.mark.asyncio
async def test_skips_search_when_str_incomplete():
    parcel = ParcelInfo(account="R123")
    assert await _section_township_range_search(FakeSearchPage([]), parcel, set()) == ([], [])


@pytest.mark.asyncio
async def test_book_page_citation_only_resolves_to_a_hit_from_the_cited_year():
    """Book numbers repeat across eras — R1611986's ALTA cites Book 1583 Page 294
    as a 1961 highway deed, and the recorder's hit for it is a 1996 deed of trust."""
    by_book = {
        "233": [
            {"reception": "135027", "doc_type": "WARRANTY DEED", "rec_date": "12/17/1908 12:00 AM"}
        ],
        "1583": [
            {"reception": "2526395", "doc_type": "DEED OF TRUST", "rec_date": "12/26/1996 11:49 AM"}
        ],
    }
    page = FakeSearchPage(lambda form: by_book.get(form["#field_BookPageID_DOT_Book"], []))
    items = [
        ExtractedId(
            id="Book 233 Page 185",
            id_type="book_page",
            context="Reservations by Union Pacific Railroad Company recorded December 17, 1908",
        ),
        ExtractedId(
            id="Book 1583 Page 294",
            id_type="book_page",
            context="Parcel conveyed to Department of Highways recorded April 27, 1961",
        ),
        ExtractedId(id="Book 999 Page 411", id_type="book_page", context="right of way deed"),
    ]
    known: set[str] = set()

    targets = await _resolve_book_page_citations(page, items, known)

    assert [(role, doc.reception) for role, doc in targets] == [("exception", "135027")]
    assert known == {"135027"}
    assert len(page.searches) == 2  # no year cited -> not searched at all


@pytest.mark.asyncio
async def test_a_repeated_sweep_reuses_the_first_one():
    """The easement scan and the section scan sweep the same S/T/R in one run;
    the second must not re-drive the recorder's form."""
    page = FakeSearchPage(_rows("1", "2"))

    first = await _search_all_rows(page, section="32", township="5N", range_="65W")
    searches = len(page.searches)
    second = await _search_all_rows(page, section="32", township="5N", range_="65W")

    assert second == first
    assert len(page.searches) == searches


@pytest.mark.asyncio
async def test_a_search_bounced_to_the_disclaimer_is_rerun(monkeypatch):
    """The bounce can land on the search submit itself, after the form loaded —
    which every R8995911 job from 2026-09-28 to 2026-10-02 died of."""
    monkeypatch.setattr("survey_art.scrapers.weld_county._DISCLAIMER_BACKOFF_S", 0)
    page = FakeSearchPage(_rows("1"))
    bounced = []

    async def evaluate(*a, **k):
        if not bounced:
            bounced.append(True)
            page.url = "https://recording.weld.gov/web/user/disclaimer"
            return []
        page.url = "https://recording.weld.gov/web/search/DOCSEARCH524S12"
        return _rows("1")

    class Ctx:
        async def clear_cookies(self, **k):
            pass

        async def add_cookies(self, cookies):
            pass

    page.evaluate = evaluate
    page.context = Ctx()

    rows = await _run_advanced_search(page, section="32")

    assert [r["reception"] for r in rows] == ["1"]
