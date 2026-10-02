"""Phase 4 (GLO) — the fallbacks the end-to-end test doesn't reach."""

from __future__ import annotations

from pathlib import Path

from survey_art.scrapers.glo_records import _match_download, _parse_answer


def test_answer_lines_tolerate_bullets_and_prose():
    rows = _parse_answer(
        "Here you go:\n- Field Notes | Vol. R0386 | x.pdf\nPATENT | COCOAA 0123\nDone."
    )
    assert rows == [("FIELD_NOTES", "Vol. R0386", "x.pdf"), ("PATENT", "COCOAA 0123", "")]


def test_downloads_the_agent_didnt_name_fall_back_to_the_file_name():
    assert _match_download(Path("/x/Patent_Image.pdf"), [])[0] == "PATENT"
    assert _match_download(Path("/x/field_note_page.jpg"), [])[0] == "FIELD_NOTES"
    assert _match_download(Path("/x/download.pdf"), [])[0] == "PLAT"
