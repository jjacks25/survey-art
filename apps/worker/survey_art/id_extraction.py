"""Extract the record IDs an ALTA survey (or any recorded document) references.

SOP Step 3A.5: an ALTA survey's Schedule B-2 exceptions reference other recorded
documents — easements, rights-of-way, prior deeds — by reception number or
book/page. Those documents live outside the subject property's own Document
History, so the only way to find them is to read the survey and see what it cites.

Two paths, cheapest first:

1. **Text layer** (`pypdf`) — free and exact. Born-digital PDFs carry the text,
   so a labelled regex (`RECORDING NO: 1766550`, `BOOK 999 AT PAGE 426`) gets
   every ID with no LLM involved.
2. **Bedrock** (Claude Haiku) — only when there is no text layer. County recorder
   scans are raster-only, and a 36"x24" survey sheet has far too much fine print
   to survive being downsampled to a single model-sized image, so each page is
   split into tiles that stay legible (see `_TILE_MAX_NATIVE_PX`) and sent as
   images with a forced tool call.

Both paths funnel through the same `_classify()` normaliser, so a reception
number looks identical whichever way it was found.
"""

from __future__ import annotations

import io
import logging
import math
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import boto3
from botocore.config import Config
from PIL import Image
from pydantic import BaseModel
from pypdf import PdfReader

from survey_art.settings import get_settings

logger = logging.getLogger(__name__)

IdType = Literal["reception_number", "book_page", "other"]


class ExtractedId(BaseModel):
    """One record identifier referenced by a document."""

    id: str
    id_type: IdType
    context: str = ""
    raw: str = ""


class IdExtraction(BaseModel):
    """Result of reading one document for the IDs it references."""

    ids: list[ExtractedId] = []
    source: Literal["text_layer", "bedrock", "none"] = "none"
    input_tokens: int = 0
    output_tokens: int = 0

    def receptions(self) -> list[str]:
        return [i.id for i in self.ids if i.id_type == "reception_number"]

    def to_metadata(self) -> list[dict]:
        """Rows for overview.json — the frontend renders a list of dicts as a table."""
        return [i.model_dump() for i in self.ids]


# --------------------------------------------------------------------------- #
# Normalising                                                                  #
# --------------------------------------------------------------------------- #

# Matches the label forms seen on Weld ALTAs and title commitments:
# "RECORDING NO: 1766550.", "RECORDING NO.: 1766548", "REC. NO. 2786305",
# "AT RECEPTION NUMBER 2696065". The label is required so that unrelated numbers
# on the sheet (dates, bearings, acreages, ordinance numbers) aren't mistaken for
# records. The trailing group catches the "RECEPTION NOS. x AND y" idiom, where a
# single label covers two documents; it only fires on a number, so
# "...AT RECEPTION NUMBER 2873123 AND JANUARY 24, 2005..." doesn't trip it.
_RECEPTION_RE = re.compile(
    r"REC(?:EPTION|ORDING|ORDED)?\.?\s*(?:NOS?|NUM(?:BER)?S?|#)?\.?\s*:?\s*"
    r"(\d{5,9})\b(?:\s*(?:,|AND|&)\s*(\d{5,9})\b)?",
    re.IGNORECASE,
)
_BOOK_PAGE_RE = re.compile(
    r"BOOK\s*(?:NO\.?\s*)?(\d{1,6})\s*(?:,|\bAT\b)?\s*PAGE\s*(\d{1,6})\b",
    re.IGNORECASE,
)
# Weld reception numbers are 5-9 digits. Used only for a value the model returned
# without its printed label.
_BARE_RECEPTION_RE = re.compile(r"^\d{5,9}$")


def _classify(raw: str, context: str = "") -> list[ExtractedId]:
    """Turn one printed reference into normalised `ExtractedId`s.

    Returns a list because one printed label can cover two documents — the
    "RECEPTION NOS. 2369866 AND 2369867" idiom — and dropping the second one
    silently loses a real easement.
    """
    text = " ".join(raw.split()).strip().strip(".,;")
    if not text:
        return []
    if m := _BOOK_PAGE_RE.search(text):
        return [
            ExtractedId(
                id=f"Book {int(m[1])} Page {int(m[2])}",
                id_type="book_page",
                context=context,
                raw=text,
            )
        ]
    if m := _RECEPTION_RE.search(text):
        return [
            ExtractedId(id=number, id_type="reception_number", context=context, raw=text)
            for number in m.groups()
            if number
        ]
    if _BARE_RECEPTION_RE.match(text):
        return [ExtractedId(id=text, id_type="reception_number", context=context, raw=text)]
    return [ExtractedId(id=text, id_type="other", context=context, raw=text)]


def _dedupe(ids: Iterator[ExtractedId] | list[ExtractedId]) -> list[ExtractedId]:
    """First occurrence wins, so the earliest (best) context is the one kept."""
    seen: set[tuple[str, str]] = set()
    out: list[ExtractedId] = []
    for item in ids:
        key = (item.id_type, item.id)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def parse_ids_from_text(text: str) -> list[ExtractedId]:
    """Pull every labelled record reference out of a block of document text."""
    return _dedupe(
        item
        for pattern in (_BOOK_PAGE_RE, _RECEPTION_RE)
        for match in pattern.finditer(text)
        for item in _classify(match[0])
    )


# --------------------------------------------------------------------------- #
# Path 1 — text layer (free)                                                   #
# --------------------------------------------------------------------------- #


def _extract_from_text_layer(reader: PdfReader) -> list[ExtractedId]:
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return parse_ids_from_text(text)


# --------------------------------------------------------------------------- #
# Path 2 — Bedrock vision (scanned documents only)                             #
# --------------------------------------------------------------------------- #

# A tile is downscaled to fit _TILE_MAX_PX (above ~1.15 MP the model downsamples
# it anyway), so _TILE_MAX_NATIVE_PX sets the effective resolution loss.
#
# Tuned against sheet 1 of Weld ALTA 4571638, scored on 36 hand-read reception
# numbers (tests/test_id_extraction.py pins the transcription):
#
#     9 MP (~2.8x, 9 tiles)  -> 20/36 found, 11 wrong    $0.026/sheet
#     3 MP (~1.6x, 30 tiles) -> 31/36 found,  2 wrong    $0.058/sheet
#
# Legible to a human is not legible enough for the model: every error at 9 MP was
# a digit transposition of a real number (1767982 read as 1766982), and a wrong
# reception can still fetch a real — but wrong — document, which is worse than
# finding nothing. Re-run the sheet-1 score before raising this.
_TILE_MAX_PX = 1_150_000
_TILE_MAX_NATIVE_PX = 3_000_000
_TILE_OVERLAP = 0.04  # so a line straddling a tile edge is whole in one of them
_MAX_PAGES = 25  # cost guard; ALTAs run 2-6 sheets
_MAX_IMAGES_PER_REQUEST = 20  # Converse hard limit; a 300-DPI sheet needs 9

_TOOL_NAME = "record_references"
_EXTRACT_TOOL = {
    "toolSpec": {
        "name": _TOOL_NAME,
        "description": "Record every reference to another recorded document found in the images.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "references": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "value": {
                                    "type": "string",
                                    "description": (
                                        "The identifier with its printed label, copied "
                                        "exactly, e.g. 'RECORDING NO: 1766550' or "
                                        "'BOOK 999 AT PAGE 426'."
                                    ),
                                },
                                "context": {
                                    "type": "string",
                                    "description": (
                                        "What the document is and who it runs to, e.g. "
                                        "'Easement - water line - City of Greeley'."
                                    ),
                                },
                            },
                            "required": ["value"],
                        },
                    }
                },
                "required": ["references"],
            }
        },
    }
}

_PROMPT = (
    "These images are tiles of one page of a land survey / title commitment. Read every "
    "one and find every reference to another recorded document: exception and easement "
    "items, rights-of-way, prior deeds, cross-referenced plats, notes and legends.\n\n"
    "Call record_references once with all of them. For each, copy the identifier and its "
    "printed label exactly as shown ('RECORDING NO: 1766550', 'BOOK 999 AT PAGE 426', "
    "'RECEPTION NUMBER 2696065') — do not reformat, renumber, or guess digits. Include "
    "brief context saying what the document is.\n\n"
    "Tiles overlap, so the same reference may appear twice — report it each time you see "
    "it; duplicates are removed later. Skip dates, bearings, distances, section/township/"
    "range numbers and ordinance numbers. If a tile has no references, that's fine — call "
    "record_references with whatever the others contain, or an empty list."
)


def _page_rasters(reader: PdfReader) -> Iterator[Image.Image]:
    """Yield the scanned image behind each page.

    ponytail: county recorder scans are exactly one full-page raster per page, so
    the embedded image *is* the page — no PDF renderer needed. Takes the largest
    image if a page ever carries several. Vector PDFs never get here; their text
    layer is parsed instead.
    """
    for page in reader.pages[:_MAX_PAGES]:
        images = list(page.images)
        if not images:
            continue
        try:
            yield max(images, key=lambda i: len(i.data)).image
        except Exception as exc:  # unsupported filter/colourspace
            logger.warning("id_extraction: could not decode page image: %s", exc)


def _grid(width: int, height: int) -> tuple[int, int]:
    """Fewest (cols, rows) that keep each tile under `_TILE_MAX_NATIVE_PX`."""
    cols = rows = 1
    while (width / cols) * (height / rows) > _TILE_MAX_NATIVE_PX:
        if width / cols >= height / rows:
            cols += 1
        else:
            rows += 1
    return cols, rows


def _tiles(image: Image.Image) -> list[bytes]:
    """Split a page raster into overlapping, model-sized PNG tiles."""
    width, height = image.size
    cols, rows = _grid(width, height)
    tile_w, tile_h = width / cols, height / rows
    pad_x, pad_y = tile_w * _TILE_OVERLAP, tile_h * _TILE_OVERLAP

    out: list[bytes] = []
    for row in range(rows):
        for col in range(cols):
            box = (
                max(0, int(col * tile_w - pad_x)),
                max(0, int(row * tile_h - pad_y)),
                min(width, int((col + 1) * tile_w + pad_x)),
                min(height, int((row + 1) * tile_h + pad_y)),
            )
            out.append(_png_bytes(image.crop(box)))
    return out


def _png_bytes(tile: Image.Image) -> bytes:
    width, height = tile.size
    scale = min(1.0, math.sqrt(_TILE_MAX_PX / max(1, width * height)))
    if scale < 1.0:
        tile = tile.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    tile.convert("L").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _bedrock_client():
    region = get_settings().aws_region
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        # Locally the whole stack runs against LocalStack via AWS_ENDPOINT_URL, but
        # LocalStack has no Bedrock — point this one client back at real AWS so the
        # containerised worker reads PDFs the same way the Fargate one does.
        endpoint_url=f"https://bedrock-runtime.{region}.amazonaws.com",
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _read_page(client, model: str, tiles: list[bytes]) -> tuple[list[ExtractedId], int, int]:
    response = client.converse(
        modelId=model,
        messages=[
            {
                "role": "user",
                "content": [
                    *({"image": {"format": "png", "source": {"bytes": t}}} for t in tiles),
                    {"text": _PROMPT},
                ],
            }
        ],
        # Always set explicitly: an unset maxTokens reserves the model's full
        # output budget against the account quota and invites throttling.
        inferenceConfig={"maxTokens": 8192, "temperature": 0},
        toolConfig={"tools": [_EXTRACT_TOOL], "toolChoice": {"tool": {"name": _TOOL_NAME}}},
    )

    usage = response.get("usage", {})
    tokens = (usage.get("inputTokens", 0), usage.get("outputTokens", 0))

    for block in response.get("output", {}).get("message", {}).get("content", []):
        tool_use = block.get("toolUse") or {}
        if tool_use.get("name") != _TOOL_NAME:
            continue
        refs = tool_use.get("input", {}).get("references") or []
        if not isinstance(refs, list):
            logger.warning("id_extraction: malformed references payload: %r", refs)
            return [], *tokens
        found = [
            item
            for r in refs
            if isinstance(r, dict)
            for item in _classify(str(r.get("value", "")), str(r.get("context") or ""))
        ]
        return found, *tokens

    logger.warning(
        "id_extraction: no tool call in response (stopReason=%s)", response.get("stopReason")
    )
    return [], *tokens


def _extract_with_bedrock(reader: PdfReader, model: str) -> IdExtraction:
    client = _bedrock_client()
    found: list[ExtractedId] = []
    in_tokens = out_tokens = 0

    for page_no, raster in enumerate(_page_rasters(reader), start=1):
        tiles = _tiles(raster)
        logger.info("id_extraction: reading page %d as %d tile(s)", page_no, len(tiles))
        for start in range(0, len(tiles), _MAX_IMAGES_PER_REQUEST):
            batch = tiles[start : start + _MAX_IMAGES_PER_REQUEST]
            try:
                page_ids, page_in, page_out = _read_page(client, model, batch)
            except Exception as exc:
                # One bad page shouldn't lose the pages that did work.
                logger.warning("id_extraction: Bedrock failed on page %d: %s", page_no, exc)
                continue
            found.extend(page_ids)
            in_tokens += page_in
            out_tokens += page_out

    return IdExtraction(
        ids=_dedupe(found), source="bedrock", input_tokens=in_tokens, output_tokens=out_tokens
    )


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #


def extract_document_ids(pdf_path: Path, *, model: str | None = None) -> IdExtraction:
    """Return every record ID referenced by `pdf_path`, cheapest path first.

    Never raises: this enriches an already-downloaded document, so a missing
    file, an undecodable PDF or an unreachable Bedrock must not fail the scrape.
    An empty `IdExtraction` is the failure mode.
    """
    try:
        reader = PdfReader(pdf_path)
    except Exception as exc:
        logger.warning("id_extraction: could not open %s: %s", pdf_path, exc)
        return IdExtraction()

    try:
        ids = _extract_from_text_layer(reader)
    except Exception as exc:
        logger.warning("id_extraction: text-layer read failed for %s: %s", pdf_path, exc)
        ids = []

    if ids:
        logger.info(
            "id_extraction: %d reference(s) from the text layer of %s (no LLM call)",
            len(ids),
            pdf_path.name,
        )
        return IdExtraction(ids=ids, source="text_layer")

    model = model or get_settings().id_extraction_model
    logger.info("id_extraction: %s has no text layer — reading it with %s", pdf_path.name, model)
    try:
        return _extract_with_bedrock(reader, model)
    except Exception as exc:
        logger.warning("id_extraction: Bedrock extraction failed for %s: %s", pdf_path, exc)
        return IdExtraction()
