"""Tests for `survey_art.id_extraction` — SOP Step 3A.5 ID harvesting."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from survey_art import id_extraction
from survey_art.id_extraction import (
    ExtractedId,
    IdExtraction,
    _classify,
    _grid,
    _tiles,
    extract_document_ids,
    parse_ids_from_text,
)


class FakePage:
    def __init__(self, text: str = "", images: list | None = None):
        self._text = text
        self.images = images or []

    def extract_text(self) -> str:
        return self._text


class FakeReader:
    def __init__(self, *pages: FakePage):
        self.pages = list(pages)


class TestParseIdsFromText:
    def test_reads_the_label_forms_a_weld_alta_actually_uses(self):
        text = """
        RECORDING NO:        1696478.
        RECORDING NO.:       1766548
        REC. NO. 2786305
        ...RECORDED MARCH 3, 1937 IN BOOK 1006 AT PAGE 281.
        RECORDING NO.: BOOK 233 AT PAGE 185.
        ASSIGNMENTS RECORDED AUGUST 9, 2001 AT RECEPTION NUMBER 2873123
        """

        ids = parse_ids_from_text(text)

        assert [i.id for i in ids if i.id_type == "reception_number"] == [
            "1696478",
            "1766548",
            "2786305",
            "2873123",
        ]
        assert [i.id for i in ids if i.id_type == "book_page"] == [
            "Book 1006 Page 281",
            "Book 233 Page 185",
        ]

    def test_ignores_numbers_that_are_not_record_references(self):
        # Dates, bearings and section/township/range all live next to the real
        # references on an ALTA; only a labelled number counts.
        text = """
        RECORDING DATE: SEPTEMBER 14, 1978
        AFFECTS: W2 SECTION 15 AND E2 SECTION 16.
        T. 5 N., R. 67 W. OF THE 6TH P.M.
        USR 823 AS SET FORTH BELOW
        PARCEL CONTAINS 621.97 ACRES
        """

        assert parse_ids_from_text(text) == []

    def test_handles_every_phrasing_on_a_real_weld_alta(self):
        """Transcribed verbatim from reception 4571638 (Weld account R1611986).

        Every label variant that sheet actually uses, plus the numbers on it that
        must NOT be picked up. Regenerate by reading the sheet, not by pasting
        parser output — the point is to pin the parser to the document.
        """
        text = """
        RECORDING NO:        1696478.
        RECORDING NO.:       1766548.
        RECORDING NO :       2149215.
        RECORDING NO.:       BOOK 233 AT PAGE 185.
        RECORDING NO:        BOOK 86 AT PAGE 273.
        NOTE: MODIFIED BY RESOLUTION ABANDONING ROAD RIGHT OF WAY RECORDED MARCH 3,
        1937 IN BOOK 1006 AT PAGE 281.
        ALSO EXCEPT A PARCEL OF LAND CONVEYED TO THE DEPARTMENT OF HIGHWAYS BY DEED
        RECORDED JANUARY 2, 1968 IN BOOK 589 AT RECEPTION NO. 1511417.
        ...BY DEEDS RECORDED JANUARY 19, 1994 IN BOOK 1423 AT RECEPTION NOS. 2369866
        AND 2369867.
        ASSIGNMENTS AND CONVEYANCES OF EASEMENTS RECORDED IN CONNECTION THEREWITH
        AUGUST 9, 2001 AT RECEPTION NUMBER 2873123 AND JANUARY 24, 2005 AT RECEPTION
        NUMBER 3255507.
        A.O.C. #1 - MONUMENT NOT ACCEPTED FROM MAP OF SURVEY, RECEPTION NUMBER 2696065.
        RECORDING DATE:      DECEMBER 15, 2000
        AFFECTS:             W2 SECTION 15 AND E2 SECTION 16.
        T.  5 N., R. 67 W. OF THE 6TH P.M.
        TERMS CONTAINED IN ORDINANCE NO. 56, 2000 REGARDING THE GOLD HILL ANNEXATION
        PER THE FEMA FLOOD INSURANCE RATE MAPS (FIRM), MAP NO. 08123C1495E
        COLORADO PROFESSIONAL LAND SURVEYOR NO. 38638
        PROJECT NO: EDW000001.10
        PARCEL CONTAINS 621.97 ACRES, INCLUDING 4.60 ACRES OF RIGHT-OF-WAY.
        """

        ids = parse_ids_from_text(text)

        assert [i.id for i in ids if i.id_type == "reception_number"] == [
            "1696478",
            "1766548",
            "2149215",
            "1511417",
            "2369866",
            "2369867",
            "2873123",
            "3255507",
            "2696065",
        ]
        assert [i.id for i in ids if i.id_type == "book_page"] == [
            "Book 233 Page 185",
            "Book 86 Page 273",
            "Book 1006 Page 281",
        ]
        assert not [i for i in ids if i.id_type == "other"]

    def test_dedupes_repeats_keeping_first_occurrence(self):
        ids = parse_ids_from_text("RECORDING NO: 1766550\nREC. NO. 1766550")

        assert [i.id for i in ids] == ["1766550"]


class TestClassify:
    def test_bare_number_is_taken_as_a_reception(self):
        assert _classify("1766551") == [
            ExtractedId(id="1766551", id_type="reception_number", raw="1766551")
        ]

    def test_labelled_value_keeps_only_the_number(self):
        assert [i.id for i in _classify("RECORDING NO: 1766551.")] == ["1766551"]

    def test_one_label_covering_two_documents_yields_both(self):
        # Dropping the second number silently loses a real easement.
        found = _classify("RECEPTION NOS. 2369866 AND 2369867")

        assert [i.id for i in found] == ["2369866", "2369867"]

    def test_book_page_is_normalised(self):
        (found,) = _classify("BOOK 999 AT PAGE 426")

        assert (found.id, found.id_type) == ("Book 999 Page 426", "book_page")

    def test_unrecognised_format_is_kept_as_other(self):
        # Worth storing for the surveyor even though nothing can auto-fetch it.
        (found,) = _classify("PLAT NO. 4-B")

        assert (found.id, found.id_type) == ("PLAT NO. 4-B", "other")

    def test_empty_value_is_dropped(self):
        assert _classify("  .  ") == []


class TestTiling:
    def test_small_page_is_a_single_tile(self):
        assert _grid(1200, 900) == (1, 1)

    def test_large_survey_sheet_is_split_until_each_tile_is_legible(self):
        # A 36"x24" sheet scanned at 300 DPI — the real Weld ALTA shape. The exact
        # grid follows _TILE_MAX_NATIVE_PX, which is tuned against measured
        # extraction accuracy; only the budget invariant is pinned here.
        cols, rows = _grid(10792, 7227)

        assert (10792 / cols) * (7227 / rows) <= id_extraction._TILE_MAX_NATIVE_PX
        assert ((10792 / (cols - 1)) * (7227 / rows)) > id_extraction._TILE_MAX_NATIVE_PX

    def test_tiles_cover_the_page_and_fit_the_model_budget(self):
        tiles = _tiles(Image.new("L", (10792, 7227), color=255))

        cols, rows = _grid(10792, 7227)
        assert len(tiles) == cols * rows
        for png in tiles:
            width, height = Image.open(io.BytesIO(png)).size
            assert width * height <= id_extraction._TILE_MAX_PX


class TestExtractDocumentIds:
    def test_prefers_the_text_layer_and_never_calls_bedrock(self, tmp_path: Path):
        pdf = tmp_path / "alta.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        reader = FakeReader(FakePage("RECORDING NO: 1766550"), FakePage("REC. NO. 2786305"))

        with (
            patch.object(id_extraction, "PdfReader", return_value=reader),
            patch.object(id_extraction, "_bedrock_client") as client,
        ):
            result = extract_document_ids(pdf)

        client.assert_not_called()
        assert result.source == "text_layer"
        assert result.receptions() == ["1766550", "2786305"]
        assert (result.input_tokens, result.output_tokens) == (0, 0)

    def test_falls_back_to_bedrock_when_the_pdf_is_a_scan(self, tmp_path: Path):
        pdf = tmp_path / "alta.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        client = MagicMock()
        client.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "name": "record_references",
                                "input": {
                                    "references": [
                                        {
                                            "value": "RECORDING NO: 1766550",
                                            "context": "Easement - City of Greeley",
                                        },
                                        {"value": "BOOK 233 AT PAGE 185"},
                                        # tiles overlap, so repeats are expected
                                        {"value": "1766550"},
                                    ]
                                },
                            }
                        }
                    ]
                }
            },
            "usage": {"inputTokens": 1500, "outputTokens": 120},
        }

        with (
            patch.object(id_extraction, "PdfReader", return_value=FakeReader(FakePage())),
            patch.object(id_extraction, "_bedrock_client", return_value=client),
            patch.object(
                id_extraction, "_page_rasters", return_value=iter([Image.new("L", (900, 700))])
            ),
        ):
            result = extract_document_ids(pdf)

        assert result.source == "bedrock"
        assert result.receptions() == ["1766550"]
        assert [i.id for i in result.ids] == ["1766550", "Book 233 Page 185"]
        assert result.ids[0].context == "Easement - City of Greeley"
        assert (result.input_tokens, result.output_tokens) == (1500, 120)

    def test_forces_the_tool_call_and_caps_output_tokens(self, tmp_path: Path):
        """An unset maxTokens reserves the model's whole budget and invites throttling."""
        pdf = tmp_path / "alta.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": []}}, "usage": {}}

        with (
            patch.object(id_extraction, "PdfReader", return_value=FakeReader(FakePage())),
            patch.object(id_extraction, "_bedrock_client", return_value=client),
            patch.object(
                id_extraction, "_page_rasters", return_value=iter([Image.new("L", (900, 700))])
            ),
        ):
            extract_document_ids(pdf)

        kwargs = client.converse.call_args.kwargs
        assert kwargs["toolConfig"]["toolChoice"] == {"tool": {"name": "record_references"}}
        assert kwargs["inferenceConfig"]["maxTokens"] == 8192
        assert [b for b in kwargs["messages"][0]["content"] if "image" in b]

    def test_one_bad_page_does_not_lose_the_others(self, tmp_path: Path):
        pdf = tmp_path / "alta.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        client = MagicMock()
        client.converse.side_effect = [
            RuntimeError("throttled"),
            {
                "output": {
                    "message": {
                        "content": [
                            {
                                "toolUse": {
                                    "name": "record_references",
                                    "input": {"references": [{"value": "2786305"}]},
                                }
                            }
                        ]
                    }
                },
                "usage": {"inputTokens": 10, "outputTokens": 2},
            },
        ]

        with (
            patch.object(id_extraction, "PdfReader", return_value=FakeReader(FakePage())),
            patch.object(id_extraction, "_bedrock_client", return_value=client),
            patch.object(
                id_extraction,
                "_page_rasters",
                return_value=iter([Image.new("L", (900, 700)), Image.new("L", (900, 700))]),
            ),
        ):
            result = extract_document_ids(pdf)

        assert result.receptions() == ["2786305"]

    def test_unreadable_pdf_returns_empty_rather_than_failing_the_scrape(self, tmp_path: Path):
        result = extract_document_ids(tmp_path / "missing.pdf")

        assert result == IdExtraction()
