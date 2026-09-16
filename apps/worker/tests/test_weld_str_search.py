"""Tests for _section_township_range_search — the S/T/R "everything else
recorded in this section" scan (distinct from the easement/ROW-only scan)."""

from __future__ import annotations

import pytest

from survey_art.scrapers.weld_county import ParcelInfo, _section_township_range_search


class _FakePage:
    """Stands in for the Playwright page driven by _run_advanced_search."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def goto(self, *a, **k):
        pass

    async def click(self, *a, **k):
        if a and a[0] == "button:has-text('Yes - Continue')":
            raise Exception("no dialog")

    async def fill(self, *a, **k):
        pass

    async def wait_for_load_state(self, *a, **k):
        pass

    async def wait_for_timeout(self, *a, **k):
        pass

    async def evaluate(self, *a, **k):
        return self._rows


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

    targets = await _section_township_range_search(page, parcel, seen)

    assert [(role, doc.reception) for role, doc in targets] == [
        ("section_township_range_search", "1111")
    ]
    assert seen == {"1111", "2222"}


@pytest.mark.asyncio
async def test_skips_search_when_str_incomplete():
    parcel = ParcelInfo(account="R123")
    targets = await _section_township_range_search(_FakePage([]), parcel, set())
    assert targets == []
