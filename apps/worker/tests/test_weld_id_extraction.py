"""Tests for SOP Step 3A.5 — turning the ALTA's referenced IDs into downloads."""

from __future__ import annotations

import pytest

from survey_art.id_extraction import ExtractedId, IdExtraction
from survey_art.scrapers.weld_county import (
    _DEMO_EXCEPTION_LIMIT,
    _select_phase_3a_exception_targets,
)
from survey_art.settings import get_settings


def _extraction(*ids: ExtractedId) -> IdExtraction:
    return IdExtraction(ids=list(ids), source="bedrock")


@pytest.fixture
def application_mode(monkeypatch):
    """Set APPLICATION_MODE, working around the lru_cache on get_settings()."""

    def _set(mode: str) -> None:
        monkeypatch.setenv("APPLICATION_MODE", mode)
        get_settings.cache_clear()

    yield _set
    monkeypatch.undo()
    get_settings.cache_clear()


class TestSelectPhase3AExceptionTargets:
    def test_only_reception_numbers_become_downloads(self):
        extraction = _extraction(
            ExtractedId(id="1766551", id_type="reception_number", context="Water line easement"),
            # No integration URL takes a book/page or a free-text reference, so
            # these stay in the metadata but aren't fetched.
            ExtractedId(id="Book 233 Page 185", id_type="book_page"),
            ExtractedId(id="PLAT NO. 4-B", id_type="other"),
        )

        targets = _select_phase_3a_exception_targets(extraction, known_receptions=set())

        assert [doc.reception for _, doc in targets] == ["1766551"]
        role, doc = targets[0]
        assert role == "exception"
        assert doc.url == "https://recording.weld.gov/web/web/integration/document/1766551"
        assert doc.doc_type == "Water line easement"

    def test_skips_documents_already_being_downloaded(self):
        extraction = _extraction(ExtractedId(id="4970002", id_type="reception_number"))

        targets = _select_phase_3a_exception_targets(extraction, known_receptions={"4970002"})

        assert targets == []

    def test_dedupes_repeated_ids(self):
        extraction = _extraction(
            ExtractedId(id="1766551", id_type="reception_number"),
            ExtractedId(id="1766551", id_type="reception_number"),
        )

        targets = _select_phase_3a_exception_targets(extraction, known_receptions=set())

        assert len(targets) == 1

    def test_no_ids_means_no_extra_downloads(self):
        assert _select_phase_3a_exception_targets(IdExtraction(), known_receptions=set()) == []


class TestApplicationModeDemo:
    @staticmethod
    def _many(count: int) -> IdExtraction:
        return _extraction(
            *(ExtractedId(id=str(4970000 + n), id_type="reception_number") for n in range(count))
        )

    def test_demo_mode_caps_the_downloads(self, application_mode):
        application_mode("demo")

        targets = _select_phase_3a_exception_targets(self._many(20), known_receptions=set())

        assert len(targets) == _DEMO_EXCEPTION_LIMIT

    def test_regular_mode_downloads_everything(self, application_mode):
        application_mode("regular")

        targets = _select_phase_3a_exception_targets(self._many(20), known_receptions=set())

        assert len(targets) == 20

    def test_demo_mode_below_the_cap_is_untouched(self, application_mode):
        application_mode("demo")

        targets = _select_phase_3a_exception_targets(self._many(2), known_receptions=set())

        assert len(targets) == 2
