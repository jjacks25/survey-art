"""Phase 4 (GLO) — reading the agent's answer and naming what it downloaded."""

from __future__ import annotations

from pathlib import Path

from survey_art.scrapers.glo_records import (
    _match_download,
    _parse_answer,
    _split_township_or_range,
)


def test_splits_sop_form_township_and_range():
    assert _split_township_or_range("5N") == ("5", "N")
    assert _split_township_or_range(" 67 w ") == ("67", "W")
    assert _split_township_or_range("") == ("", "")


def test_answer_lines_become_records_and_files_match_them():
    rows = _parse_answer(
        "Here you go:\n"
        "- PLAT | 2380 | 230823.pdf\n"
        "FIELD NOTES | Vol. R0386 pages 112-118 | R0386_112.pdf\n"
        "PATENT | COCOAA 012345 | patent.pdf\n"
        "Done."
    )
    assert [k for k, _, _ in rows] == ["PLAT", "FIELD_NOTES", "PATENT"]
    assert _match_download(Path("/x/230823.pdf"), rows) == ("PLAT", "2380")
    assert _match_download(Path("/x/R0386_112.pdf"), rows) == (
        "FIELD_NOTES",
        "Vol. R0386 pages 112-118",
    )
    assert _match_download(Path("/x/patent.pdf"), rows) == ("PATENT", "COCOAA 012345")


def test_unclaimed_downloads_fall_back_to_the_file_name():
    assert _match_download(Path("/x/Patent_Image.pdf"), [])[0] == "PATENT"
    assert _match_download(Path("/x/field_note_page.jpg"), [])[0] == "FIELD_NOTES"
    assert _match_download(Path("/x/download.pdf"), [])[0] == "PLAT"
