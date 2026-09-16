"""Tests for `_expand_cross_references` — reading every downloaded document for
the other documents it cites, without re-extracting or re-fetching the same one twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from survey_art.id_extraction import ExtractedId, IdExtraction
from survey_art.scrapers.weld_county import _DocRecord, _expand_cross_references


def _doc(reception: str, doc_type: str = "EASEMENT") -> _DocRecord:
    return _DocRecord(
        reception=reception,
        rec_date="",
        doc_type=doc_type,
        grantor="",
        grantee="",
        url=f"https://recording.weld.gov/web/web/integration/document/{reception}",
    )


class _FakeOverview:
    def __init__(self) -> None:
        self.sections: dict[str, object] = {}

    def set_section(self, section: str, value) -> None:
        self.sections[section] = value


@pytest.mark.asyncio
async def test_cycle_does_not_infinite_loop_or_double_extract(monkeypatch):
    """Doc 1 cites doc 2, doc 2 cites doc 1 back plus new doc 3 — must terminate
    and extract each document exactly once."""
    extract_calls: list[str] = []

    def fake_extract(path: Path) -> IdExtraction:
        reception = path.name
        extract_calls.append(reception)
        cites = {"1": ["2"], "2": ["1", "3"], "3": []}[reception]
        return IdExtraction(
            ids=[ExtractedId(id=r, id_type="reception_number") for r in cites],
            source="text_layer",
        )

    download_calls: list[list[str]] = []

    async def fake_download(address, targets, doc_filter, dest, *, username, password):
        receptions = [doc.reception for _, doc in targets]
        download_calls.append(receptions)
        results = [(role, doc, [Path(doc.reception)]) for role, doc in targets]
        return results, 0.0, 0, 0

    monkeypatch.setattr("survey_art.scrapers.weld_county.extract_document_ids", fake_extract)
    monkeypatch.setattr("survey_art.scrapers.weld_county._download_documents", fake_download)

    ov = _FakeOverview()
    initial = [("alta", _doc("1"), [Path("1")])]
    known = {"1"}

    new_results, in_tok, out_tok = await _expand_cross_references(
        "123 Main St", None, Path("/tmp"), ov, initial, known, username="u", password="p"
    )

    # Extracted each of 1, 2, 3 exactly once, in discovery order, even though
    # doc 2 re-cites doc 1.
    assert extract_calls == ["1", "2", "3"]
    # Only the newly-discovered receptions were ever fetched — never re-fetches "1".
    assert download_calls == [["2"], ["3"]]
    assert {doc.reception for _, doc, _ in new_results} == {"2", "3"}
    assert known == {"1", "2", "3"}
    # Every citation found is recorded for the surveyor, including "1" (already
    # known/fetched) re-cited by doc 2 — extracted_ids is a citation log, not
    # just the set of newly-fetched receptions.
    assert [row["id"] for row in ov.sections["extracted_ids"]] == ["2", "1", "3"]


@pytest.mark.asyncio
async def test_skips_extraction_for_failed_downloads(monkeypatch):
    """A target with no saved paths (failed fetch) is never sent to extraction."""
    extract_calls: list[str] = []

    def fake_extract(path: Path) -> IdExtraction:
        extract_calls.append(path.name)
        return IdExtraction()

    monkeypatch.setattr("survey_art.scrapers.weld_county.extract_document_ids", fake_extract)

    ov = _FakeOverview()
    initial = [("exception", _doc("9"), [])]  # failed download: no paths

    new_results, in_tok, out_tok = await _expand_cross_references(
        "123 Main St", None, Path("/tmp"), ov, initial, {"9"}, username="u", password="p"
    )

    assert extract_calls == []
    assert new_results == []
