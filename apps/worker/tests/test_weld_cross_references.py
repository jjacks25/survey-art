"""Tests for `_expand_cross_references` — reading every downloaded document for
the other documents it cites, without re-extracting or re-fetching the same one twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from survey_art.id_extraction import ExtractedId, IdExtraction
from survey_art.scrapers import weld_county
from survey_art.scrapers.weld_county import _DocRecord, _expand_cross_references


def _doc(reception: str, doc_type: str = "EASEMENT") -> _DocRecord:
    return _DocRecord(reception, doc_type=doc_type)


class _FakeOverview:
    def __init__(self) -> None:
        self.sections: dict[str, object] = {}

    def set_section(self, section: str, value) -> None:
        self.sections[section] = value

    def get(self, section: str, default=None):
        return self.sections.get(section, default)


def _recording_download(calls: list[list[str]]):
    """A `_download_documents` stand-in that "saves" every target and records
    each batch of receptions it was asked for."""

    async def fake_download(targets, dest):
        calls.append([doc.reception for _, doc in targets])
        return [(role, doc, [Path(doc.reception)]) for role, doc in targets]

    return fake_download


@pytest.fixture(autouse=True)
def _no_extraction_cache(monkeypatch):
    """These tests are about the walk, not the cache. Stub the S3 round-trip so
    they neither reach the network nor need the cache to miss by accident."""
    monkeypatch.setattr(weld_county.jobs, "get_cached_extraction", lambda *a, **k: None)
    monkeypatch.setattr(weld_county.jobs, "put_cached_extraction", lambda *a, **k: None)
    monkeypatch.setattr(weld_county, "cache_fingerprint", lambda *a, **k: "test")


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
    fake_download = _recording_download(download_calls)

    monkeypatch.setattr("survey_art.scrapers.weld_county.extract_document_ids", fake_extract)
    monkeypatch.setattr("survey_art.scrapers.weld_county._download_documents", fake_download)

    ov = _FakeOverview()
    initial = [("alta", _doc("1"), [Path("1")])]
    known = {"1"}

    new_results, in_tok, out_tok = await _expand_cross_references(Path("/tmp"), ov, initial, known)

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
async def test_a_whole_depth_level_is_fetched_in_one_call(monkeypatch):
    """Two documents at the same depth citing different documents must produce a
    single batched download, not one per citing document.

    `_download_documents` launches a browser and logs in per call, so batching a
    level is what keeps that cost at one-per-level instead of one-per-document.
    """

    def fake_extract(path: Path) -> IdExtraction:
        cites = {"1": ["3"], "2": ["4"], "3": [], "4": []}[path.name]
        return IdExtraction(
            ids=[ExtractedId(id=r, id_type="reception_number") for r in cites],
            source="text_layer",
        )

    download_calls: list[list[str]] = []
    fake_download = _recording_download(download_calls)

    monkeypatch.setattr("survey_art.scrapers.weld_county.extract_document_ids", fake_extract)
    monkeypatch.setattr("survey_art.scrapers.weld_county._download_documents", fake_download)

    initial = [("alta", _doc("1"), [Path("1")]), ("vesting_deed", _doc("2"), [Path("2")])]
    new_results, _in_tok, _out_tok = await _expand_cross_references(
        Path("/tmp"), _FakeOverview(), initial, {"1", "2"}
    )

    assert download_calls == [["3", "4"]]
    assert {doc.reception for _, doc, _ in new_results} == {"3", "4"}


@pytest.mark.asyncio
async def test_stops_fetching_at_the_depth_limit(monkeypatch):
    """Each document cites one more forever; the walk must stop chasing citations
    once `_MAX_CROSS_REFERENCE_DEPTH` levels deep, and say so in the overview."""

    def fake_extract(path: Path) -> IdExtraction:
        nxt = str(int(path.name) + 1)
        return IdExtraction(
            ids=[ExtractedId(id=nxt, id_type="reception_number")], source="text_layer"
        )

    levels: list[list[str]] = []
    fake_download = _recording_download(levels)

    monkeypatch.setattr("survey_art.scrapers.weld_county.extract_document_ids", fake_extract)
    monkeypatch.setattr("survey_art.scrapers.weld_county._download_documents", fake_download)

    ov = _FakeOverview()
    await _expand_cross_references(Path("/tmp"), ov, [("alta", _doc("0"), [Path("0")])], {"0"})

    assert len(levels) == weld_county._MAX_CROSS_REFERENCE_DEPTH
    assert ov.sections["limits"] == {"cross_reference_depth_limit": True}


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

    new_results, in_tok, out_tok = await _expand_cross_references(Path("/tmp"), ov, initial, {"9"})

    assert extract_calls == []
    assert new_results == []


def test_citation_summary_counts_cited_documents_once_under_any_role():
    """Job b721dda1 showed "fetched 182 of 568": the 568 counted dates and well
    numbers as reception numbers, and the 182 missed cited documents another
    search had already downloaded."""
    from survey_art.scrapers.weld_county import _citation_summary

    cited = [
        {"id": i, "id_type": "reception_number"}
        for i in ("4508544", "4508544", "1497035", "149705", "1983622", "20151221")
    ]
    results = [
        ("owner_name_search", _doc("4508544"), [Path("x")]),  # cited, found by another search
        ("exception", _doc("1497035"), [Path("y")]),
        ("exception", _doc("149705"), []),  # a misread: the recorder has nothing
    ]

    assert _citation_summary(cited, results) == {
        "cited": 4,  # duplicates and the 8-digit date don't count
        "downloaded": 2,
        "not_in_recorder": ["149705"],
        "not_fetched": ["1983622"],
    }
