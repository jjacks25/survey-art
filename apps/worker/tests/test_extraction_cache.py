"""Tests for `_extract_cited_ids` — reusing a previous run's reading of a
recorded document instead of paying Bedrock to read it again.

This is the single biggest cost lever in a run (no Weld recorder PDF has a text
layer, so every one takes the vision path), so the behaviour worth pinning is
that a hit really does skip the model *and* reports zero tokens for this run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from survey_art.id_extraction import ExtractedId, IdExtraction
from survey_art.scrapers import weld_county
from survey_art.scrapers.weld_county import _extract_cited_ids


@pytest.fixture(autouse=True)
def _fixed_fingerprint(monkeypatch):
    monkeypatch.setattr(weld_county, "cache_fingerprint", lambda *a, **k: "fp")


@pytest.fixture
def store(monkeypatch) -> dict:
    """An in-memory stand-in for the S3 extraction cache."""
    written: dict[tuple[str, str], dict] = {}
    monkeypatch.setattr(
        weld_county.jobs, "get_cached_extraction", lambda fp, rec: written.get((fp, rec))
    )
    monkeypatch.setattr(
        weld_county.jobs,
        "put_cached_extraction",
        lambda fp, rec, payload: written.__setitem__((fp, rec), payload),
    )
    return written


def _extraction(*ids: str, source: str = "bedrock") -> IdExtraction:
    return IdExtraction(
        ids=[ExtractedId(id=i, id_type="reception_number") for i in ids],
        source=source,
        input_tokens=30_000,
        output_tokens=500,
    )


def test_a_miss_extracts_and_stores_the_result(store, monkeypatch):
    monkeypatch.setattr(weld_county, "extract_document_ids", lambda _p: _extraction("111"))

    result = _extract_cited_ids("999", Path("doc.pdf"))

    assert [i.id for i in result.ids] == ["111"]
    assert result.source == "bedrock"
    assert ("fp", "999") in store


def test_a_hit_skips_the_model_entirely(store, monkeypatch):
    monkeypatch.setattr(weld_county, "extract_document_ids", lambda _p: _extraction("111"))
    _extract_cited_ids("999", Path("doc.pdf"))  # populate

    calls = []

    def boom(_path):
        calls.append(_path)
        return _extraction("should-not-be-used")

    monkeypatch.setattr(weld_county, "extract_document_ids", boom)
    result = _extract_cited_ids("999", Path("doc.pdf"))

    assert calls == [], "a cached document must not be sent to Bedrock again"
    assert [i.id for i in result.ids] == ["111"]


def test_a_hit_reports_no_tokens_for_this_run(store, monkeypatch):
    """The cached ids are real, but this run didn't spend the tokens that found
    them — the cost breakdown reads these fields, so they have to be zero."""
    monkeypatch.setattr(weld_county, "extract_document_ids", lambda _p: _extraction("111"))
    _extract_cited_ids("999", Path("doc.pdf"))

    result = _extract_cited_ids("999", Path("doc.pdf"))

    assert result.source == "cache"
    assert (result.input_tokens, result.output_tokens) == (0, 0)


def test_a_document_that_could_not_be_read_is_not_cached(store, monkeypatch):
    """`source="none"` means the read failed (unreadable PDF, Bedrock outage) —
    caching that would make the failure permanent for this document."""
    monkeypatch.setattr(weld_county, "extract_document_ids", lambda _p: IdExtraction())

    _extract_cited_ids("999", Path("doc.pdf"))

    assert store == {}


def test_a_corrupt_cache_entry_falls_back_to_extracting(store, monkeypatch):
    monkeypatch.setattr(
        weld_county.jobs, "get_cached_extraction", lambda fp, rec: {"ids": "not-a-list"}
    )
    monkeypatch.setattr(weld_county, "extract_document_ids", lambda _p: _extraction("111"))

    result = _extract_cited_ids("999", Path("doc.pdf"))

    assert [i.id for i in result.ids] == ["111"]
