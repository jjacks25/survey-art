"""PLSS section geometry from the BLM's national cadastral service, in State Plane feet.

Two jobs:

* **Anchor deeds.** Almost every rural Colorado description starts at, or ties
  to, a section or quarter corner ("from which the northwest corner of Section 15
  bears N 1°56' W, 214.2 feet"). Putting that corner at its real coordinates
  georeferences the whole description, and every exception and easement tied to
  the same section lands in the right place relative to the others.
* **Draw aliquot descriptions.** A vesting deed that says "Section 15" or "the
  S1/2 of Section 16" has no courses at all; its boundary *is* the PLSS geometry.

Source: BLM National PLSS CadNSDI, layer 2 (sections), queried with
``outSR=2231`` so the service hands back Colorado State Plane North (US ft)
directly, no projection library needed. Section polygons there carry the
quarter and sixteenth corners as vertices.

Aliquot parts are computed from the corners by the standard rule for a regular
section (quarter-quarter corners at the midpoints), using the BLM's own quarter
corners for the first split. ponytail: government lots and irregular sections
along township lines aren't handled; their BLM second-division polygons
(layer 3) are the upgrade path.
"""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass

import httpx

from survey_art.cogo import Point, distance_between, intersect

logger = logging.getLogger(__name__)

_SECTIONS_URL = (
    "https://gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer/2/query"
)
# Colorado State Plane North, US survey feet: the zone Weld County sits in.
# ponytail: one zone. Denver/Arapahoe/Jefferson are Central (2232); pick per
# county when a second county gets drawings.
GRID_EPSG = 2231

# BLM principal-meridian codes for the meridians that govern Colorado.
_MERIDIANS = {"6": "06", "sixth": "06", "new mexico": "23", "nm": "23", "ute": "31"}


def _meridian_code(meridian: str) -> str:
    key = re.sub(r"(th|st|nd|rd)?\s*(p\.?\s*m\.?|principal meridian)?$", "", meridian.lower())
    return _MERIDIANS.get(key.strip(" ."), "06")


def plss_id(state: str, meridian: str, township: str, range_: str) -> str:
    """BLM township identifier, e.g. CO060050N0670W0 for T5N R67W 6th PM."""
    t = re.fullmatch(r"\s*(\d+)\s*([NS])\s*", township.upper())
    r = re.fullmatch(r"\s*(\d+)\s*([EW])\s*", range_.upper())
    if not t or not r:
        raise ValueError(f"unrecognised township/range {township!r} {range_!r}")
    return f"{state.upper()}{_meridian_code(meridian)}{int(t[1]):03d}0{t[2]}{int(r[1]):03d}0{r[2]}0"


@dataclass(frozen=True)
class Quad:
    """A section or aliquot part: its four corners and the points between them.

    ``n``/``e``/``s``/``w`` are the midpoints of each side (for a section, the
    quarter corners) and ``c`` is the centre (where the quarter lines cross).
    """

    nw: Point
    ne: Point
    se: Point
    sw: Point
    n: Point
    e: Point
    s: Point
    w: Point
    c: Point

    @classmethod
    def from_corners(cls, nw: Point, ne: Point, se: Point, sw: Point, **known: Point) -> Quad:
        def mid(a: Point, b: Point) -> Point:
            return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)

        n = known.get("n", mid(nw, ne))
        e = known.get("e", mid(ne, se))
        s = known.get("s", mid(sw, se))
        w = known.get("w", mid(nw, sw))
        c = intersect(n, s, w, e) or mid(n, s)
        return cls(nw, ne, se, sw, n, e, s, w, c)

    def corner(self, name: str) -> Point:
        return getattr(self, name.lower())

    def ring(self) -> list[Point]:
        return [self.nw, self.ne, self.se, self.sw]

    def part(self, token: str) -> Quad:
        """One aliquot split: a quarter ('NE') or a half ('S')."""
        q = self
        splits = {
            "NE": lambda: Quad.from_corners(q.n, q.ne, q.e, q.c),
            "NW": lambda: Quad.from_corners(q.nw, q.n, q.c, q.w),
            "SE": lambda: Quad.from_corners(q.c, q.e, q.se, q.s),
            "SW": lambda: Quad.from_corners(q.w, q.c, q.s, q.sw),
            "N": lambda: Quad.from_corners(q.nw, q.ne, q.e, q.w, n=q.n, s=q.c),
            "S": lambda: Quad.from_corners(q.w, q.e, q.se, q.sw, n=q.c, s=q.s),
            "E": lambda: Quad.from_corners(q.n, q.ne, q.se, q.s, e=q.e, w=q.c),
            "W": lambda: Quad.from_corners(q.nw, q.n, q.s, q.sw, e=q.c, w=q.w),
        }
        return splits[token]()


_ALIQUOT_RE = re.compile(r"(NE|NW|SE|SW|N|S|E|W)\s*(1/4|1/2|/4|/2|¼|½|4|2)")


def aliquot_tokens(aliquot: str) -> list[str]:
    """'N1/2 NW1/4' -> ['N', 'NW'], in the order written (innermost first).

    Raises on anything that isn't a clean run of quarters and halves, so a
    description we can't subdivide is reported rather than drawn wrong.
    """
    text = re.sub(r"\b(OF|THE|AND|SECTION|SEC\.?)\b|[,.]", " ", aliquot.upper())
    tokens: list[str] = []
    pos = 0
    for m in _ALIQUOT_RE.finditer(text):
        if text[pos : m.start()].strip():
            break
        letters, frac = m.group(1), m.group(2)
        is_half = frac in ("1/2", "/2", "½", "2")
        if is_half != (len(letters) == 1):
            raise ValueError(f"{letters}{frac} is not an aliquot part")
        tokens.append(letters)
        pos = m.end()
    if text[pos:].strip():
        raise ValueError(f"cannot subdivide by {aliquot!r}")
    return tokens


def subdivide(section: Quad, aliquot: str) -> Quad:
    """The part of ``section`` an aliquot call names. '' is the whole section."""
    quad = section
    for token in reversed(aliquot_tokens(aliquot)):
        quad = quad.part(token)
    return quad


# --------------------------------------------------------------------------- #
# BLM fetch                                                                     #
# --------------------------------------------------------------------------- #


def _quad_from_ring(ring: list[Point]) -> Quad:
    """Name a section polygon's corners.

    The four section corners are the vertices furthest out along each diagonal.
    A quarter corner is the vertex nearest the midpoint of its side; if the
    polygon happens not to carry one there, fall back to the midpoint.
    """
    nw = min(ring, key=lambda p: p[0] - p[1])
    ne = max(ring, key=lambda p: p[0] + p[1])
    se = max(ring, key=lambda p: p[0] - p[1])
    sw = min(ring, key=lambda p: p[0] + p[1])

    def quarter(a: Point, b: Point) -> Point:
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        nearest = min(ring, key=lambda p: distance_between(p, mid))
        return nearest if distance_between(nearest, mid) < 0.05 * distance_between(a, b) else mid

    return Quad.from_corners(
        nw, ne, se, sw, n=quarter(nw, ne), e=quarter(ne, se), s=quarter(sw, se), w=quarter(nw, sw)
    )


@functools.cache
def fetch_section(state: str, meridian: str, township: str, range_: str, section: int) -> Quad:
    """One section's geometry from the BLM. Raises if the service has no such section."""
    resp = httpx.get(
        _SECTIONS_URL,
        params={
            "where": (
                f"PLSSID='{plss_id(state, meridian, township, range_)}' "
                f"AND FRSTDIVNO='{int(section):02d}' AND FRSTDIVTYP='SN'"
            ),
            "outFields": "FRSTDIVID",
            "outSR": str(GRID_EPSG),
            "f": "json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    features = resp.json().get("features") or []
    if not features:
        raise LookupError(f"BLM has no Section {section}, T{township} R{range_}")
    # A section split by a road or survey can come back as several rings; the
    # outer boundary is the one with the largest extent.
    rings = [r for f in features for r in f["geometry"]["rings"]]
    ring = max(rings, key=lambda r: _extent(r))
    return _quad_from_ring([(float(x), float(y)) for x, y in ring])


def _extent(ring: list) -> float:
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


def corner_name(corner: str) -> str:
    """Normalise how a model or a deed names a corner to Quad's attribute names."""
    words = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "CENTER": "C", "CENTRE": "C"}
    text = corner.upper().replace("1/4", "").replace("QUARTER", "").replace("CORNER", "")
    for word, letter in words.items():
        text = text.replace(word, letter)
    letters = re.sub(r"[^NSEWC]", "", text)
    if letters in {"NW", "NE", "SE", "SW", "N", "E", "S", "W", "C"}:
        return letters
    if letters in {"WN", "EN", "ES", "WS"}:
        return letters[::-1]
    raise ValueError(f"unrecognised corner {corner!r}")


def polygon_area(ring: list[Point]) -> float:
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1]))) / 2
