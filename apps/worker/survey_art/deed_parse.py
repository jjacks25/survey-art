"""Read a recorded document's land descriptions into structured courses.

The model's only job is transcription: copy every bearing, distance and curve
call as printed, say which corner the description is tied to, and say what kind
of description it is. Every number stays a string here; `cogo.py` parses and
solves them, so nothing about the geometry depends on what the model computed.

Input is the PDF's text layer when it has one (born-digital deeds), otherwise
the page scans, tiled the same way `id_extraction.py` tiles them, because a
transposed digit in a bearing is exactly the failure that tiling fixed there.
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel, Field, ValidationError
from pypdf import PdfReader

from survey_art import cogo
from survey_art.costs import bedrock_token_cost
from survey_art.id_extraction import (
    _TILE_FORMAT,
    _TILE_MAX_NATIVE_PX,
    _bedrock_client,
    _call_tool,
    _page_rasters,
    _thumbnail_bytes,
    _tile_bytes,
    _tiles,
)
from survey_art.settings import get_settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Schema                                                                        #
# --------------------------------------------------------------------------- #

CornerName = Literal["NW", "N", "NE", "E", "SE", "S", "SW", "W", "C"]


class CornerRef(BaseModel):
    """A PLSS corner: a corner of a section, or of an aliquot part of one."""

    section: int
    township: str = Field(description="e.g. '5N'")
    range: str = Field(description="e.g. '67W'")
    meridian: str = "6th"
    aliquot: str = Field(
        "",
        description=(
            "The part whose corner this is, as 'NW1/4' or 'SE1/4 SE1/4'; empty for the "
            "section itself. The north quarter corner of a section is aliquot '' corner 'N'; "
            "'the NE corner of the NW1/4' is aliquot 'NW1/4' corner 'NE'."
        ),
    )
    corner: CornerName = Field(description="N/E/S/W alone means the quarter corner / midpoint")


class AliquotPart(BaseModel):
    section: int
    township: str
    range: str
    meridian: str = "6th"
    aliquot: str = Field("", description="'' for the whole section, else e.g. 'S1/2' or 'NE1/4'")


class Basis(BaseModel):
    """'considering the west line of Section 15 to bear N 00°00'00" E'."""

    from_corner: CornerRef
    to_corner: CornerRef
    bearing: str


class CurveCall(BaseModel):
    direction: Literal["left", "right"]
    radius: str | None = None
    arc: str | None = None
    delta: str | None = None
    chord_bearing: str | None = None
    chord: str | None = None


class Course(BaseModel):
    role: Literal["tie_in", "boundary", "tie_out"] = "boundary"
    reverse: bool = Field(
        False,
        description=(
            "True when the bearing as printed runs against the direction of travel. The usual "
            "case: 'beginning at a point from which the NW corner bears N 1°56' W, 214.2 feet' "
            "is a tie_in from that corner with reverse=true."
        ),
    )
    kind: Literal["line", "curve"] = "line"
    bearing: str | None = Field(None, description="As printed, e.g. 'N. 0° 59' 45\" W.'")
    distance: str | None = Field(None, description="As printed with its unit, e.g. '2,633.8 feet'")
    curve: CurveCall | None = None
    monument: str | None = Field(None, description="Monument called for at the end of the course")
    ends_at: CornerRef | None = Field(
        None,
        description="The PLSS corner this course ends at, when the deed says so "
        "('to the northwest corner of Section 15')",
    )
    adjoiner: str | None = Field(None, description="Line or owner the course runs along, if named")


class LegalDescription(BaseModel):
    label: str = Field(description="Short name: 'Parcel 1', 'Parcel No. 44', 'Easement centerline'")
    shape: Literal["parcel", "centerline", "aliquot"]
    aliquot_parts: list[AliquotPart] = []
    start: CornerRef | None = Field(
        None, description="The corner the first course starts from (commencement or POB)"
    )
    end: CornerRef | None = Field(None, description="The corner a tie_out course runs to")
    basis: Basis | None = None
    courses: list[Course] = []
    width: str | None = Field(None, description="Total width of a centerline strip, e.g. '30 feet'")
    called_area: str | None = None
    derived: bool = Field(
        False,
        description=(
            "True if you turned a 'the South 950 feet of the East 485 feet of ...' style "
            "description into courses yourself"
        ),
    )


class DocumentExtract(BaseModel):
    descriptions: list[LegalDescription] = []
    notes: str = Field(
        "",
        description="One or two sentences: land the document describes that isn't in "
        "descriptions, and why not. Empty if there is none.",
    )


class DeedParse(BaseModel):
    extract: DocumentExtract = DocumentExtract()
    source: Literal["text_layer", "bedrock", "cache", "none"] = "none"
    input_tokens: int = 0
    output_tokens: int = 0
    # Priced per call, because one document mixes models (the page filter and
    # the reader). Zero for a cache hit: this run didn't spend it.
    cost_usd: float = 0.0
    error: str | None = None


# --------------------------------------------------------------------------- #
# Prompt                                                                        #
# --------------------------------------------------------------------------- #

_TOOL_NAME = "record_land_descriptions"
_TOOL = {
    "toolSpec": {
        "name": _TOOL_NAME,
        "description": "Record every land description in the document, transcribed exactly.",
        "inputSchema": {"json": DocumentExtract.model_json_schema()},
    }
}

_PROMPT = """\
You are transcribing the land descriptions in a recorded Colorado document (a deed, \
easement, right-of-way grant or similar) so a surveyor's software can plot them. \
Call record_land_descriptions once.

What counts as a description:
- shape "parcel": a closed metes-and-bounds boundary.
- shape "centerline": a strip easement given as a centerline plus a width. It does not close.
- shape "aliquot": land described only by PLSS parts ("Section 15", "the S1/2 of \
Section 16", "the SE1/4 SE1/4"). Put each part in aliquot_parts with aliquot written \
like "SE1/4 SE1/4" or "S1/2", innermost part first as in the deed; "" means the whole \
section. Leave courses empty.
- "The South 950 feet of the East 485 feet of the SE1/4 of Section 16" style land: \
write it as a parcel whose start is the matching corner, with courses using the \
cardinal words North/South/East/West, and set derived true.
Anything else — a hand sketch, "a 100' x 100' site in the NW/4NE/4", a strip described \
only in words — goes in notes, not descriptions. A document with no land description \
at all (a mortgage, a notice, an agreement) returns an empty list.

Rules for courses:
- Copy every bearing, distance, radius, arc, delta and chord exactly as printed, with \
its units. Never compute, round, convert or correct a number, and never guess a digit. \
If a number is unreadable, write what you can see with ? for the unreadable digits.
- Courses go in order. A "commencing at ... thence ..." run to the true point of \
beginning is role tie_in. "Beginning at a point from which the X corner bears B, D" \
is one tie_in course from that corner with reverse true. A closing reference after \
the last course ("from which the NE corner bears B, D") is role tie_out, with that \
corner as end.
- start is the PLSS corner the first course leaves from: the commencement corner, \
the corner the beginning point is tied to ("beginning at a point on the west line, \
from which the northwest corner bears..." starts at the northwest corner), or the \
beginning point itself when it is a corner. Leave it null if the description is \
tied to something that is not a PLSS corner (a lot corner, a road).
- Curves: kind "curve", with whatever of radius, arc, delta, chord_bearing and chord \
the deed gives, and direction left or right.
- ends_at: when a course runs to a named PLSS corner ("to the northeast corner of \
the NW1/4 of Section 15"), give that corner.
- basis: only when the deed explicitly states one ("considering the west line of \
Section 15 to bear North 00°00'00\" East", "bearings are based on..."): the two corners \
that line runs between, in the direction of that bearing. A course that merely runs \
along a section line is not a basis.

Record each separate description in the document, including every parcel of an \
exhibit with several. If the same description is printed twice, record it once. \
Keep notes to one or two sentences about land you left out; don't mention pages or images."""

# Only the pages that carry a description are read closely: a 12-page vesting
# deed has its land on one exhibit page, and the rest is signatures and
# boilerplate. A cheap per-page question picks them; one request then reads all
# of them together, so a description that runs over a page break stays whole.
_FILTER_TOOL = {
    "toolSpec": {
        "name": "page_has_land_description",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {"answer": {"type": "boolean"}},
                "required": ["answer"],
            }
        },
    }
}
_FILTER_PROMPT = (
    "Is there any land description on this scanned page: metes and bounds ('thence "
    "N 45° E, 100 feet'), a section/township/range or aliquot part ('the S1/2 of "
    "Section 16, Township 5 North'), a centerline easement, or an exhibit describing "
    "land? Answer true if there is any, even a partial one. Answer false for "
    "signatures, notary blocks, cover sheets and terms with no land in them."
)
_FILTER_MAX_PX = 1_150_000
_FILTER_SKIP_BELOW_PAGES = 3  # read short documents whole; the filter isn't worth it

# Pages go to the model as full-width horizontal bands, not a grid. A grid
# (id_extraction's `_tiles`) splits each line of text down the middle, and on a
# deed that put a course's bearing in one tile and its distance in the next: on
# 1512031 the model paired four courses with their neighbours' distances. A
# band keeps every line whole; the overlap is wide enough that a line on a band
# edge is complete in one of the two.
_BAND_OVERLAP = 0.12
_MAX_IMAGES = 20  # Converse's per-request limit
_MAX_PAGES = 20
_PROMPT_VERSION = 3
_READ_TIMEOUT_S = 300


def cache_fingerprint(model: str | None = None) -> str:
    """Namespace for cached parses: everything that would change the answer."""
    model = model or get_settings().deed_parse_model
    return f"deeds_v{_PROMPT_VERSION}_{model}_{_TILE_MAX_NATIVE_PX}".replace("/", "_").replace(
        ":", "_"
    )


def _bands(image: Image.Image) -> list[bytes]:
    """Split a page into full-width, overlapping bands of about the tile budget.

    ponytail: a sheet much wider than a letter page (a plat) would come out as
    thin slivers, so those fall back to the grid; plats rarely carry the
    written description this module reads.
    """
    width, height = image.size
    if width > 1.5 * 2600:
        return _tiles(image)
    band = max(1, int(_TILE_MAX_NATIVE_PX / width))
    step = max(1, int(band * (1 - _BAND_OVERLAP)))
    out: list[bytes] = []
    top = 0
    while True:
        out.append(_tile_bytes(image.crop((0, top, width, min(height, top + band)))))
        if top + band >= height:
            return out
        top += step


def _text_layer(reader: PdfReader) -> str:
    try:
        return "\n".join(page.extract_text() or "" for page in reader.pages[:_MAX_PAGES]).strip()
    except Exception as exc:  # noqa: BLE001 — a bad text layer just means "use the scans"
        logger.warning("deed_parse: text-layer read failed: %s", exc)
        return ""


def _readable(course: Course) -> bool:
    """Whether a course's numbers parse. A distance has to start with a number:
    "to the point of beginning" or "to a point 950 feet north of..." is the
    model describing a course it couldn't reduce, not a transcription."""
    try:
        if course.kind == "curve":
            return course.curve is not None
        if not course.bearing or not course.distance:
            return False
        cogo.parse_bearing(course.bearing)
        cogo.parse_distance(course.distance)
        return bool(re.match(r"\s*[\d.]", course.distance))
    except ValueError:
        return False


def _complete(desc: LegalDescription) -> bool:
    """Whether a description has enough in it to draw.

    The prompt says to put word-only strips and sketches in notes, and the model
    still returns some of them as descriptions with no courses. Deciding here,
    in code, is what makes "nothing to plot" reliable.
    """
    if desc.shape == "aliquot":
        return bool(desc.aliquot_parts)
    boundary = [c for c in desc.courses if c.role == "boundary"]
    return len(boundary) >= 2 and all(_readable(c) for c in desc.courses)


def _normalise(desc: LegalDescription) -> LegalDescription:
    """Fix the two tie mistakes the model makes that the deed's grammar settles.

    * A reverse tie_in ("beginning at a point from which the NW corner bears...")
      starts at the corner it names. The model often names that corner on the
      course (``ends_at``) but picks a different ``start`` — on 1511417 the
      west quarter corner, because the point lies "on the west line".
    * A tie_out is always printed from the last point to the corner, so it is
      never reversed.
    """
    courses = [
        c.model_copy(update={"reverse": False}) if c.role == "tie_out" else c for c in desc.courses
    ]
    start = desc.start
    first = courses[0] if courses else None
    if first and first.role == "tie_in" and first.reverse and first.ends_at:
        start = first.ends_at
        courses[0] = first.model_copy(update={"ends_at": None})
    return desc.model_copy(update={"courses": courses, "start": start})


def _clean(extract: DocumentExtract) -> DocumentExtract:
    descriptions = [_normalise(d) for d in extract.descriptions]
    kept = [d for d in descriptions if _complete(d)]
    dropped = [d.label for d in descriptions if not _complete(d)]
    notes = extract.notes.strip()
    if dropped:
        notes = f"{notes} Not plotted (no usable courses): {'; '.join(dropped)}.".strip()
    return DocumentExtract(descriptions=kept, notes=notes)


def _ask(client, model: str, content: list[dict]) -> tuple[DocumentExtract, int, int]:
    answer, in_tok, out_tok = _call_tool(client, model, content, _TOOL, max_tokens=16_000)
    if answer is None:
        return DocumentExtract(), in_tok, out_tok
    try:
        return _clean(DocumentExtract.model_validate(answer)), in_tok, out_tok
    except ValidationError as exc:
        logger.warning("deed_parse: model answer did not fit the schema: %s", exc)
        return DocumentExtract(), in_tok, out_tok


def _pages_with_land(client, pages: list[Image.Image]) -> tuple[list[int], int, int]:
    """Indexes of the pages the cheap model thinks carry a land description."""
    if len(pages) < _FILTER_SKIP_BELOW_PAGES:
        return list(range(len(pages))), 0, 0
    model = get_settings().id_extraction_model

    def ask(page: Image.Image) -> tuple[bool, int, int]:
        try:
            answer, i, o = _call_tool(
                client,
                model,
                [{"image": {"format": "jpeg", "source": {"bytes": _thumbnail_bytes(page,
                  _FILTER_MAX_PX)}}}, {"text": _FILTER_PROMPT}],
                _FILTER_TOOL,
                max_tokens=256,
            )  # fmt: skip
            return bool((answer or {"answer": True}).get("answer", True)), i, o
        except Exception as exc:  # noqa: BLE001 — when unsure, read the page
            logger.warning("deed_parse: page filter failed, reading the page anyway: %s", exc)
            return True, 0, 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(ask, pages))
    keep = [i for i, (yes, _, _) in enumerate(answers) if yes]
    return keep, sum(a[1] for a in answers), sum(a[2] for a in answers)


def parse_document(pdf_path: Path, *, model: str | None = None) -> DeedParse:
    """Every land description in ``pdf_path``. Never raises; failure is
    ``source="none"`` with ``error`` set."""
    model = model or get_settings().deed_parse_model
    try:
        reader = PdfReader(pdf_path)
    except Exception as exc:  # noqa: BLE001
        return DeedParse(error=f"could not open PDF: {exc}")

    try:
        # Reading twenty bands of a long exhibit and writing out every course can
        # run past botocore's 60 s default read timeout (it did, on 2873123).
        client = _bedrock_client(read_timeout=_READ_TIMEOUT_S)
        text = _text_layer(reader)
        if len(text) > 200:
            extract, in_tok, out_tok = _ask(
                client, model, [{"text": f"{_PROMPT}\n\nDocument text:\n\n{text}"}]
            )
            return DeedParse(
                extract=extract,
                source="text_layer",
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=bedrock_token_cost(model, in_tok, out_tok),
            )

        rasters = list(_page_rasters(reader))[:_MAX_PAGES]
        if not rasters:
            return DeedParse(error="no text layer and no page images")
        keep, in_tok, out_tok = _pages_with_land(client, rasters)
        usd = bedrock_token_cost(get_settings().id_extraction_model, in_tok, out_tok)
        if not keep:
            return DeedParse(
                source="bedrock", input_tokens=in_tok, output_tokens=out_tok, cost_usd=usd
            )
        logger.info("deed_parse: %s — reading page(s) %s of %d", pdf_path.name,
                    ", ".join(str(i + 1) for i in keep), len(rasters))  # fmt: skip

        images = [b for i in keep for b in _bands(rasters[i])]
        # ponytail: past Converse's 20-image cap, read in slices (a description
        # crossing a slice boundary can split); hasn't happened on a deed yet.
        extracts = []
        for start in range(0, len(images), _MAX_IMAGES):
            extract, i, o = _ask(
                client,
                model,
                [
                    {"text": _PROMPT + "\n\nThe images are overlapping horizontal bands of "
                     "the pages, top to bottom, in page order."},
                    *({"image": {"format": _TILE_FORMAT, "source": {"bytes": b}}}
                      for b in images[start : start + _MAX_IMAGES]),
                ],
            )  # fmt: skip
            extracts.append(extract)
            in_tok, out_tok = in_tok + i, out_tok + o
            usd += bedrock_token_cost(model, i, o)
        merged = (
            extracts[0]
            if len(extracts) == 1
            else DocumentExtract(
                descriptions=[d for e in extracts for d in e.descriptions],
                notes=" ".join(e.notes for e in extracts if e.notes),
            )
        )
        return DeedParse(extract=merged, source="bedrock", input_tokens=in_tok,
                         output_tokens=out_tok, cost_usd=usd)  # fmt: skip
    except Exception as exc:  # noqa: BLE001 — one unreadable document must not fail the drawing
        logger.warning("deed_parse: failed on %s: %s", pdf_path.name, exc)
        return DeedParse(error=str(exc))
