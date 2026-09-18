"""Tests for the owner-name search: its pure-logic filters (S/T/R exemption
packet) and the vesting-deed search every route now runs."""

from __future__ import annotations

import pytest

from survey_art.scrapers.weld_county import (
    ParcelInfo,
    _date_sort_key,
    _matches_exemption_filter,
    _matches_survey_filter,
    _matches_vesting_deed_label,
    _owner_name_search,
)


class _FakePage:
    """Returns rows per search, keyed by the criteria actually left in the form."""

    def __init__(self, by_query: dict[tuple[str, str], list[dict]]):
        self._by_query = by_query
        self.queries: list[tuple[str, str]] = []
        self._form: dict[str, str] = {}

    async def goto(self, *a, **k):
        pass

    async def click(self, *a, **k):
        if a and a[0] == "button:has-text('Yes - Continue')":
            raise Exception("no dialog")

    async def fill(self, selector, value, *a, **k):
        self._form[selector] = value

    async def wait_for_load_state(self, *a, **k):
        pass

    async def wait_for_timeout(self, *a, **k):
        pass

    async def evaluate(self, *a, **k):
        query = (
            self._form.get("#field_BothNamesID", ""),
            self._form.get("#field_PLSSLegalID_DOT_Section", ""),
        )
        self.queries.append(query)
        return self._by_query.get(query, [])


def _row(reception: str, doc_type: str, rec_date: str) -> dict:
    return {"reception": reception, "doc_type": doc_type, "rec_date": rec_date, "doc_id": ""}


_PARCEL = ParcelInfo(
    account="R8961716", owner="KRIER MICHAEL K", section="25", township="9N", range_="61W"
)


@pytest.mark.asyncio
async def test_searches_the_owner_name_against_the_parcels_section_first():
    page = _FakePage(
        {
            ("KRIER MICHAEL K", "25"): [
                _row("111", "WARRANTY DEED", "03-04-2011"),
                _row("222", "DEED OF TRUST", "05-04-2011"),
                _row("333", "JOINT TENANCY WARRANTY DEED", "06-01-2019"),
            ]
        }
    )

    targets = await _owner_name_search(page, _PARCEL, set())

    assert page.queries == [("KRIER MICHAEL K", "25")]  # no fallback needed
    # Everything recorded under the owner's name comes down; the deeds are
    # tagged and sorted first (most recent first) so the vesting deed is
    # obvious, but nothing is filtered out.
    assert [(role, doc.reception) for role, doc in targets] == [
        ("vesting_deed", "333"),
        ("vesting_deed", "111"),
        ("owner_name", "222"),
    ]


@pytest.mark.asyncio
async def test_falls_back_to_the_surname_then_to_a_countywide_search():
    # Tyler indexes some names "KRIER, MICHAEL K", so the assessor's spelling
    # can miss — the surname finds it, still bounded by the parcel's section.
    page = _FakePage({("KRIER", "25"): [_row("444", "WARRANTY DEED", "03-04-2011")]})

    targets = await _owner_name_search(page, _PARCEL, set())

    assert page.queries == [("KRIER MICHAEL K", "25"), ("KRIER", "25")]
    assert [doc.reception for _, doc in targets] == ["444"]


@pytest.mark.asyncio
async def test_keeps_what_the_earlier_queries_found_while_hunting_for_a_deed():
    page = _FakePage(
        {
            ("KRIER MICHAEL K", "25"): [_row("111", "LIEN", "03-04-2011")],
            ("KRIER", "25"): [_row("444", "WARRANTY DEED", "03-04-2011")],
        }
    )

    targets = await _owner_name_search(page, _PARCEL, set())

    assert [(role, doc.reception) for role, doc in targets] == [
        ("vesting_deed", "444"),
        ("owner_name", "111"),
    ]


@pytest.mark.asyncio
async def test_skips_documents_another_route_already_queued():
    page = _FakePage({("KRIER MICHAEL K", "25"): [_row("111", "WARRANTY DEED", "03-04-2011")]})
    seen = {"111"}

    assert await _owner_name_search(page, _PARCEL, seen) == []


@pytest.mark.asyncio
async def test_no_owner_on_record_is_not_an_error():
    page = _FakePage({})
    assert await _owner_name_search(page, ParcelInfo(account="R1"), set()) == []
    assert page.queries == []


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
    # Labels beyond the canonical four that still vest title — the recorder's
    # own type list is longer than the assessor's.
    assert _matches_vesting_deed_label("JOINT TENANCY WARRANTY DEED")
    assert _matches_vesting_deed_label("PERSONAL REPRESENTATIVES DEED")
    assert _matches_vesting_deed_label("BARGAIN AND SALE DEED")
    # Says DEED, conveys something other than the fee.
    assert not _matches_vesting_deed_label("DEED OF TRUST")
    assert not _matches_vesting_deed_label("MINERAL DEED")
    assert not _matches_vesting_deed_label("EASEMENT DEED")
    assert not _matches_vesting_deed_label("RELEASE OF DEED OF TRUST")


def test_matches_exemption_filter():
    assert _matches_exemption_filter("SUBDIVISION EXEMPTION")
    assert _matches_exemption_filter("AMENDED EXEMPTION")
    assert not _matches_exemption_filter("WARRANTY DEED")


def test_matches_survey_filter():
    assert _matches_survey_filter("ALTA SURVEY")
    assert _matches_survey_filter("AMENDED SURVEY")
    assert not _matches_survey_filter("EASEMENT")
