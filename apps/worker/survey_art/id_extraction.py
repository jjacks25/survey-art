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
   images with a forced tool call. Tile batches are independent requests and go
   out concurrently through one shared, bounded pool (`_BEDROCK_CONCURRENCY`).
   A page is first asked which way up it is (see `_page_turn`), because a tenth
   of these scans store a landscape sheet sideways and declare nothing.

Both paths funnel through the same `_classify()` normaliser, so a reception
number looks identical whichever way it was found.

On a real Weld property none of this is optional: recorder scans carry no text
layer at all, so path 1 never fires and every downloaded document pays for a
vision read. That is why the concurrency above matters — a run is hundreds of
Bedrock calls, not a handful. Callers on the event loop should push this whole
function to a thread (`asyncio.to_thread`); it blocks on PDF decoding as well as
on the network.
"""

from __future__ import annotations

import concurrent.futures
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
    # "cache" is a previous run's result replayed from S3 — the ids are whatever
    # produced them originally, but the token counts are 0, because this run
    # didn't spend them. Keep it that way: the cost breakdown reads these.
    source: Literal["text_layer", "bedrock", "cache", "none"] = "none"
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
#
# Raising it to 9 MP was retried after `_tile_bytes()` was fixed to antialias,
# on the theory that the extra tiles had only ever been compensating for the
# aliasing. They were not. Summed over an 8-document sample 9 MP looks level
# (138 vs 141 of 194 references), but that total is 72% two large documents;
# per document it is clearly worse, because a letter-size deed at ~3.8 MP stops
# being split at all and gets downscaled 0.55x instead of 0.78x, losing the one
# reception number it carries. Keep 3 MP.
_TILE_MAX_PX = 1_150_000
_TILE_MAX_NATIVE_PX = 3_000_000
_TILE_OVERLAP = 0.04  # so a line straddling a tile edge is whole in one of them
_MAX_PAGES = 25  # cost guard; ALTAs run 2-6 sheets
# Converse allows 20 per call, but fewer images per request measures better —
# the model attends to each tile more closely. On the 8-document sample, at the
# tile geometry above and with the antialiasing fix in `_tile_bytes`:
#
#     20 per request -> 134/194 references, 6 misread
#      4 per request -> 141/194 references, 9 misread
#
# The ALTA goes 34/36 -> 36/36 and the 12-page vesting deed 96/103 -> 103/103.
# Costs ~3% more input tokens, because the prompt is re-sent per request; the
# extra requests are absorbed by _BEDROCK_CONCURRENCY.
_MAX_IMAGES_PER_REQUEST = 4

# Tile batches are independent requests, so they go out concurrently rather than
# one page at a time. One shared pool for the whole process — not one per
# document — so that reading many documents at once (see `_expand_cross_references`
# in scrapers/weld_county.py) still can't put more than this many calls in flight.
#
# This is a courtesy cap, not a quota ceiling: a full 86-document Weld property is
# ~340 requests / ~1.6M input tokens, against an account limit of 10,000 requests
# and 5M tokens *per minute* for the Haiku 4.5 cross-region profile. Raising it
# buys little — the wall clock is already dominated by the slowest single document.
_BEDROCK_CONCURRENCY = 16
_bedrock_pool: concurrent.futures.ThreadPoolExecutor | None = None


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    """The shared Bedrock request pool, created on first use.

    Built lazily rather than at import time so that merely importing this module
    (the CLI, the API's test collection) doesn't spin up threads that never get
    used. Never shut down: it lives for the life of the worker process, which
    exits after one job.
    """
    global _bedrock_pool
    if _bedrock_pool is None:
        _bedrock_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=_BEDROCK_CONCURRENCY, thread_name_prefix="bedrock"
        )
    return _bedrock_pool


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
                                        "exactly, e.g. 'RECORDING NO: 0000000' or "
                                        "'BOOK 000 AT PAGE 000'."
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

# Every example identifier here is deliberately all-zeroes. They used to be real
# reception numbers copied off a Weld ALTA (1766550, BOOK 999 AT PAGE 426,
# 2696065) — and all three are real documents that this scraper downloads, so a
# model that leaned on the examples produced plausible, checkable, completely
# wrong citations. Measured on the R1611986 sample: 7 spurious emissions of those
# three ids across 8 documents that contain none of them, against 0 with the
# placeholders below. Keep example ids unmistakably fake.
_PROMPT = (
    "These images are tiles of one page of a land survey / title commitment. Read every "
    "one and find every reference to another recorded document: exception and easement "
    "items, rights-of-way, prior deeds, cross-referenced plats, notes and legends.\n\n"
    "Call record_references once with all of them. For each, copy the identifier and its "
    "printed label exactly as shown ('RECORDING NO: 0000000', 'BOOK 000 AT PAGE 000', "
    "'RECEPTION NUMBER 0000000') — do not reformat, renumber, or guess digits. The "
    "examples just show punctuation; never report a number you did not read in one of "
    "the images. Include brief context saying what the document is.\n\n"
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
            out.append(_tile_bytes(image.crop(box)))
    return out


# What the tiles go over the wire as. Bedrock prices an image by its dimensions,
# not its bytes, so this is free — and it has to be JPEG: an antialiased
# greyscale scan is pathological for PNG (~6x the bytes of the bitonal original),
# which is enough to overrun a Converse request body at 20 images per call.
_TILE_FORMAT = "jpeg"
_TILE_QUALITY = 85


def _fit(image: Image.Image, max_px: int) -> Image.Image:
    """Greyscale `image`, shrunk to at most `max_px` pixels.

    The `convert("L")` has to happen *before* the resize. County recorder scans
    are mode "1" (bitonal), and PIL resamples a mode "1" image by
    nearest-neighbour no matter which filter you ask for — so resizing first
    threw away ~57% of the rows and columns of a 36"x24" sheet with no averaging
    at all, breaking thin strokes and turning 8s into 6s. Converting first lets
    LANCZOS actually average, which is what makes a downscaled digit legible.
    Same output dimensions either way, so this costs nothing.

    Every downscale in this module goes through here, because every one of them
    is handed a bitonal recorder scan.
    """
    image = image.convert("L")
    width, height = image.size
    scale = min(1.0, math.sqrt(max_px / max(1, width * height)))
    if scale < 1.0:
        image = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS
        )
    return image


def _tile_bytes(tile: Image.Image) -> bytes:
    """Downscale one tile to the model's budget and encode it."""
    buf = io.BytesIO()
    _fit(tile, _TILE_MAX_PX).save(
        buf, format=_TILE_FORMAT.upper(), quality=_TILE_QUALITY, optimize=True
    )
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Which way up is the page?                                                     #
# --------------------------------------------------------------------------- #

# Weld recorder scans routinely store a landscape sheet in a portrait raster with
# the text running sideways, and **no page in the corpus sets `/Rotate`** (all 333
# are 0), so nothing in the file declares it. Measured over those 333 pages, 33
# (9.9%) are turned 90 degrees — and they are disproportionately the pages that
# matter, recorder exhibit tables with a RECEPTION NUMBER column. All twelve
# exhibit pages of exception_2873123.pdf are sideways.
#
# Sideways is not a quiet failure. The model does not return nothing; it invents
# plausible reception numbers. So "re-read the ones that came back empty" cannot
# work: 0 of 86 documents return zero references, and the worst offender
# (exception_2696065.pdf, a 41-reference Map of Survey) returns five, all made up.
#
# Turning the page instead: over the corpus, corpus-verified reception numbers —
# ids that name a PDF the scraper actually downloaded, so no human adjudication
# needed — go 218 -> 257 (+18%).
_TURN_PROBE_MAX_PX = 130_000  # ~360x360
_TURN_DIRECTION_MAX_PX = 65_000

# Two questions, because one prompt cannot answer both well. Asking "how many
# degrees?" gets the *presence* of rotation right and the direction wrong — it
# answers 90 for everything, including the pages that need 270. Asking "which of
# these reads normally?" gets the direction right. Scored against 33 hand-read
# pages (every page a detector flagged, plus 12 sampled at random):
#
#     3-way "which reads normally", 130k px   16/16 found, 2 false, 3 turned the wrong way
#     2-way "which reads normally",  65k px   direction 8/8 correct
#
# So: the 3-way call on every page to find them, the 2-way call on the ~10% it
# flags to orient them.
#
# Two counterintuitive results, both measured, both worth not re-litigating:
#
#   * **Smaller is better.** 65k px beats 260k beats 1M for the 2-way question.
#     Orientation is a gestalt property; shrink the page until only layout
#     survives and the model stops being distracted by the content. It also makes
#     the probe nearly free.
#   * **Never batch pages into one call.** Six pages per call scored 12/16 with
#     four false positives; one page per call scored 13/13. Same attention
#     dilution as `_MAX_IMAGES_PER_REQUEST`.
#
# An ink-projection heuristic (no model call at all) was tried first and rejected:
# 7/16 recall at 70% precision, firing on exception_3511023's dense upright
# township tables and missing exception_2873123's sideways exhibits entirely.
# Both questions are answered through this tool, and the wording of the one
# field is load-bearing: describing it as "the 1-based index of the image that
# reads normally" instead of the spelled-out list below flipped
# exception_2696065.pdf (a 41-reference Map of Survey, plainly sideways) from
# 90 back to 0, reproducibly. Change either string and re-score.
_TURN_TOOL_NAME = "pick_orientation"


def _turn_tool(choices: str) -> dict:
    return {
        "toolSpec": {
            "name": _TURN_TOOL_NAME,
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"readable": {"type": "integer", "description": choices}},
                    "required": ["readable"],
                }
            },
        }
    }


_TURN_PROBE_TOOL = _turn_tool("Which image reads normally: 1, 2 or 3")
_TURN_DIRECTION_TOOL = _turn_tool("1 if the first image reads normally, 2 if the second does")
_TURN_PROBE_PROMPT = (
    "Three copies of one scanned page: as supplied, turned anticlockwise, and "
    "turned clockwise.\n\n"
    "Exactly one has its printed text the right way up, reading left-to-right. "
    "Most pages are already correct, so answer 1 unless the text in image 1 "
    "clearly runs vertically.\n\n"
    "Say which image reads normally: 1, 2 or 3."
)
_TURN_DIRECTION_PROMPT = (
    "Two copies of the same scanned page, turned opposite ways.\n\n"
    "Exactly one of them has its printed text the right way up, reading "
    "left-to-right. The other is upside down: its lines of text are still "
    "horizontal, but every letter is inverted.\n\n"
    "Say which image reads normally, 1 or 2."
)


def _thumbnail_bytes(image: Image.Image, max_px: int) -> bytes:
    """A tiny greyscale JPEG of a whole page, for the orientation questions."""
    buf = io.BytesIO()
    _fit(image, max_px).save(buf, format="JPEG", quality=80, optimize=True)
    return buf.getvalue()


def _ask_which_reads(client, model: str, shots: list[bytes]) -> tuple[int, int, int]:
    """Index of the image the model says reads normally, plus tokens spent."""
    three_way = len(shots) == 3
    response = client.converse(
        modelId=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"text": _TURN_PROBE_PROMPT if three_way else _TURN_DIRECTION_PROMPT},
                    *({"image": {"format": "jpeg", "source": {"bytes": s}}} for s in shots),
                ],
            }
        ],
        inferenceConfig={"maxTokens": 512, "temperature": 0},
        toolConfig={
            "tools": [_TURN_PROBE_TOOL if three_way else _TURN_DIRECTION_TOOL],
            "toolChoice": {"tool": {"name": _TURN_TOOL_NAME}},
        },
    )
    usage = response.get("usage", {})
    tokens = (usage.get("inputTokens", 0), usage.get("outputTokens", 0))
    for block in response.get("output", {}).get("message", {}).get("content", []):
        tool_use = block.get("toolUse") or {}
        if tool_use.get("name") == _TURN_TOOL_NAME:
            try:
                pick = int(tool_use.get("input", {}).get("readable", 1))
            except (TypeError, ValueError):
                pick = 1
            return (pick if 1 <= pick <= len(shots) else 1), *tokens
    return 1, *tokens


def _page_turn(client, model: str, raster: Image.Image) -> tuple[int, int, int]:
    """Degrees to turn this page anticlockwise, plus the tokens it cost to decide.

    0 for the overwhelming majority of pages, which costs one call over a
    ~360x360 thumbnail. Never raises: a page whose orientation can't be
    established is read as supplied, exactly as before.
    """
    try:
        upright = _thumbnail_bytes(raster, _TURN_PROBE_MAX_PX)
        turned = [
            _thumbnail_bytes(raster.rotate(degrees, expand=True), _TURN_PROBE_MAX_PX)
            for degrees in (90, 270)
        ]
        pick, in_tokens, out_tokens = _ask_which_reads(client, model, [upright, *turned])
        if pick == 1:
            return 0, in_tokens, out_tokens

        # It is turned; the probe's own answer for *which way* is unreliable, so
        # ask the narrower question over a smaller pair.
        pair = [
            _thumbnail_bytes(raster.rotate(degrees, expand=True), _TURN_DIRECTION_MAX_PX)
            for degrees in (90, 270)
        ]
        pick, extra_in, extra_out = _ask_which_reads(client, model, pair)
        return (90 if pick == 1 else 270), in_tokens + extra_in, out_tokens + extra_out
    except Exception as exc:
        logger.warning("id_extraction: orientation probe failed, reading page as-is: %s", exc)
        return 0, 0, 0


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
                    *({"image": {"format": _TILE_FORMAT, "source": {"bytes": t}}} for t in tiles),
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
    in_tokens = out_tokens = 0

    rasters = list(_page_rasters(reader))
    turns = list(_pool().map(lambda r: _page_turn(client, model, r), rasters))

    # A page the probe flags is read BOTH ways and the better answer kept, rather
    # than simply turned. The probe's false positive rate is low but not zero
    # (2 of 18 flags on the hand-read sample), and turning an upright page loses
    # every reference on it — measured, twice, before this guard existed. The
    # extra pass is only paid on the ~10% of pages that get flagged, which was
    # 130 of a 1093-tile property.
    jobs: list[tuple[tuple[int, int], list[bytes]]] = []
    for page_no, (raster, (turn, probe_in, probe_out)) in enumerate(
        zip(rasters, turns, strict=True), start=1
    ):
        in_tokens += probe_in
        out_tokens += probe_out
        renders = [(0, raster)]
        if turn:
            logger.info(
                "id_extraction: page %d looks turned %d° — reading it both ways", page_no, turn
            )
            renders.append((turn, raster.rotate(turn, expand=True)))
        for degrees, image in renders:
            tiles = _tiles(image)
            logger.info(
                "id_extraction: reading page %d (turned %d°) as %d tile(s)",
                page_no,
                degrees,
                len(tiles),
            )
            jobs += [
                ((page_no, degrees), tiles[start : start + _MAX_IMAGES_PER_REQUEST])
                for start in range(0, len(tiles), _MAX_IMAGES_PER_REQUEST)
            ]

    def read(job: tuple[tuple[int, int], list[bytes]]) -> tuple[list[ExtractedId], int, int]:
        (page_no, degrees), tiles = job
        try:
            return _read_page(client, model, tiles)
        except Exception as exc:
            # One bad page shouldn't lose the pages that did work.
            logger.warning(
                "id_extraction: Bedrock failed on page %d (turned %d°): %s", page_no, degrees, exc
            )
            return [], 0, 0

    # `map` yields in submission order however the calls interleave, so page
    # order — and with it `_dedupe`'s "first occurrence wins", which is what
    # keeps the earliest and best context — is the same as reading serially.
    renders: dict[tuple[int, int], list[ExtractedId]] = {}
    for (key, _), (page_ids, page_in, page_out) in zip(jobs, _pool().map(read, jobs), strict=True):
        renders.setdefault(key, []).extend(page_ids)
        in_tokens += page_in
        out_tokens += page_out

    def labelled(key: tuple[int, int]) -> int:
        """References that parsed as an actual record citation.

        Counting *all* references instead loses real reception numbers: a
        wrongly-turned render still emits plenty of free text, and on the 33
        flagged pages of the R1611986 corpus that junk won the count often
        enough to cost six corpus-verified receptions (35 -> 34 against a
        baseline of 4). Junk lands in `_classify`'s "other" bucket, so not
        counting it is the whole fix.
        """
        return sum(1 for item in renders[key] if item.id_type != "other")

    found: list[ExtractedId] = []
    for page_no in sorted({page for page, _ in renders}):
        # Most citations wins; a tie goes to the page as supplied.
        best = max(
            (key for key in renders if key[0] == page_no),
            key=lambda key: (labelled(key), -key[1]),
        )
        if best[1]:
            logger.info("id_extraction: page %d read better turned %d°", page_no, best[1])
        found.extend(renders[best])

    return IdExtraction(
        ids=_dedupe(found), source="bedrock", input_tokens=in_tokens, output_tokens=out_tokens
    )


_THUMBNAIL_MAX_WIDTH = 400


def make_thumbnail(pdf_path: Path) -> bytes | None:
    """JPEG thumbnail of `pdf_path`'s first page, or None if it can't be made
    (no embedded page raster — a vector PDF rather than a county scan — or an
    unreadable file). Reuses `_page_rasters()`: the same "the embedded image
    *is* the page" shortcut `_extract_with_bedrock` relies on, so no PDF
    renderer is needed here either. Never raises — a thumbnail is a nice-to-
    have for the Results grid, not something that should fail an upload.
    """
    try:
        reader = PdfReader(pdf_path)
        raster = next(_page_rasters(reader), None)
    except Exception as exc:
        logger.warning("make_thumbnail: could not read %s: %s", pdf_path, exc)
        return None
    if raster is None:
        return None
    # Convert before resizing, for the reason spelled out in `_fit`: these
    # rasters are bitonal and PIL would otherwise resample them by
    # nearest-neighbour. It matters more here than for a tile — a thumbnail is a
    # ~0.04x downscale, so without averaging almost every stroke lands between
    # samples and the result is noise rather than a shrunken page.
    raster = raster.convert("L")
    raster.thumbnail((_THUMBNAIL_MAX_WIDTH, raster.height), Image.LANCZOS)
    buf = io.BytesIO()
    raster.convert("RGB").save(buf, format="JPEG", quality=70)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #


def cache_fingerprint(model: str | None = None) -> str:
    """Key namespace for cached extractions — everything that would change the
    answer for the same document.

    Tile geometry is in here because it's the quality knob (see
    `_TILE_MAX_NATIVE_PX`): re-tuning it must not keep serving results the old
    settings produced, and bumping it is how you invalidate the cache.

    So is the wire encoding. A render-only change (the bitonal-resize fix in
    `_tile_bytes`) leaves the geometry untouched but changes what the model
    actually sees, and serving the old answers would hide the improvement.

    And so is the orientation pass, for the same reason: it changes the answer
    for a tenth of the pages in the corpus without touching tile geometry.
    """
    model = model or get_settings().id_extraction_model
    render = f"{_TILE_MAX_NATIVE_PX}_{_TILE_MAX_PX}_{_TILE_FORMAT}{_TILE_QUALITY}"
    return f"{model}_{render}_turn{_TURN_PROBE_MAX_PX}".replace("/", "_").replace(":", "_")


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
