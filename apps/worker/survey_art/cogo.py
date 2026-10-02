"""Coordinate geometry for metes-and-bounds descriptions. Pure math, stdlib only.

Everything here is deterministic so it can be tested exhaustively. The model in
`deed_parse.py` only transcribes what a deed says; turning bearings and distances
into coordinates, closing the figure and finding the likely blunder all happen here.

Conventions:

* Coordinates are (x, y) = (easting, northing) in US survey feet.
* A direction is an **azimuth in degrees**, clockwise from north, in [0, 360).
* A curve's ``bulge`` follows the DXF convention: tan(delta/4), positive when
  the arc runs counter-clockwise (a curve to the left).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #

# One US survey foot is 1200/3937 m. Deeds written in metres convert to it,
# because that is the unit of the Colorado State Plane grid the drawing lands on.
_FEET_PER_UNIT = {
    "ft": 1.0,
    "in": 1 / 12,
    "yd": 3.0,
    "m": 3937 / 1200,
    "chain": 66.0,
    "rod": 16.5,
    "link": 0.66,
    "vara": 33 + 1 / 3,  # Texas vara; appears in southwest deeds
}
_UNIT_WORDS = [
    (r"chains?|ch\b|chs\b", "chain"),
    (r"links?|lks?\b|li\b", "link"),
    (r"rods?|poles?|perch(?:es)?|rd\b", "rod"),
    (r"varas?", "vara"),
    (r"yards?|yds?\b", "yd"),
    (r"met(?:er|re)s?|m\b", "m"),
    (r"inch(?:es)?|in\b|\"|”", "in"),
    (r"feet|foot|ft\b|'|’", "ft"),
]
_DISTANCE_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?|\.\d+)\s*(" + "|".join(p for p, _ in _UNIT_WORDS) + r")?",
    re.IGNORECASE,
)


def parse_distance(text: str) -> float:
    """Feet from a distance as written: '2,633.8 feet', '10 chains 2 links',
    "52.4'", '30.48 m'. A bare number is feet, which is how Colorado deeds
    occasionally print one ('995.83 to the Northeast corner')."""
    total = 0.0
    found = False
    last_end = None
    for match in _DISTANCE_RE.finditer(text):
        # "2 chains 15 links" is one distance; "30.0 feet to the ... Section 15"
        # is not, so stop at the first gap that is more than a separator.
        if last_end is not None and not re.fullmatch(
            r"[\s,]*(?:and|plus)?[\s,]*", text[last_end : match.start()], re.IGNORECASE
        ):
            break
        last_end = match.end()
        number, unit = match.group(1), match.group(2)
        value = float(number.replace(",", ""))
        key = "ft"
        if unit:
            key = next(k for p, k in _UNIT_WORDS if re.fullmatch(p, unit, re.IGNORECASE))
        total += value * _FEET_PER_UNIT[key]
        found = True
    if not found:
        raise ValueError(f"no distance in {text!r}")
    return total


def parse_angle(text: str) -> float:
    """Decimal degrees from 45°30'15", 45-30-15, 45 30 15, 45d30m15s or 45.504."""
    cleaned = text.replace("º", "°").replace("’", "'").replace("′", "'")
    cleaned = cleaned.replace("”", '"').replace("″", '"').replace("''", '"')
    numbers = re.findall(r"\d+(?:\.\d+)?", cleaned)
    if not numbers:
        raise ValueError(f"no angle in {text!r}")
    parts = [float(n) for n in numbers[:3]]
    degrees = (
        parts[0]
        + (parts[1] / 60 if len(parts) > 1 else 0)
        + (parts[2] / 3600 if len(parts) > 2 else 0)
    )
    return degrees


_QUADRANT_RE = re.compile(
    r"^\s*(north|south|n|s)\.?\s*(.*?)\s*(east|west|e|w)\.?\s*$", re.IGNORECASE | re.DOTALL
)
_CARDINAL = {"north": 0.0, "n": 0.0, "east": 90.0, "e": 90.0,
             "south": 180.0, "s": 180.0, "west": 270.0, "w": 270.0}  # fmt: skip


def parse_bearing(text: str) -> float:
    """Azimuth from a direction as written.

    Handles quadrant bearings in all their printed forms ('N. 0° 59' 45" W.',
    'north 00°00'00" east', 'S45-30-15E'), cardinal words ('North', 'due west'),
    and plain azimuths ('Az 123°45'', '123.75')."""
    raw = " ".join(text.replace("due ", "").split())
    if raw.lower().rstrip(".") in _CARDINAL:
        return _CARDINAL[raw.lower().rstrip(".")]
    if m := _QUADRANT_RE.match(raw):
        ns, angle, ew = m.group(1)[0].upper(), m.group(2), m.group(3)[0].upper()
        theta = parse_angle(angle) if re.search(r"\d", angle) else 0.0
        if theta > 90:
            raise ValueError(f"quadrant angle over 90° in {text!r}")
        return {
            ("N", "E"): theta,
            ("S", "E"): 180 - theta,
            ("S", "W"): 180 + theta,
            ("N", "W"): (360 - theta) % 360,
        }[(ns, ew)]
    return parse_angle(raw) % 360


def format_bearing(azimuth: float) -> str:
    """Quadrant bearing to the nearest second, e.g. N 45°30'15" E."""
    az = azimuth % 360
    if az <= 90:
        ns, ew, theta = "N", "E", az
    elif az <= 180:
        ns, ew, theta = "S", "E", 180 - az
    elif az <= 270:
        ns, ew, theta = "S", "W", az - 180
    else:
        ns, ew, theta = "N", "W", 360 - az
    seconds = round(theta * 3600)
    d, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{ns} {d:02d}°{m:02d}'{s:02d}\" {ew}"


# --------------------------------------------------------------------------- #
# Courses                                                                      #
# --------------------------------------------------------------------------- #

Point = tuple[float, float]


def _offset(p: Point, azimuth: float, distance: float) -> Point:
    a = math.radians(azimuth)
    return (p[0] + distance * math.sin(a), p[1] + distance * math.cos(a))


def azimuth_between(a: Point, b: Point) -> float:
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360


def distance_between(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


@dataclass
class Curve:
    """A solved circular curve. ``delta`` is the central angle in degrees."""

    direction: str  # "left" | "right"
    radius: float
    delta: float
    chord_azimuth: float

    @property
    def arc(self) -> float:
        return self.radius * math.radians(self.delta)

    @property
    def chord(self) -> float:
        return 2 * self.radius * math.sin(math.radians(self.delta) / 2)

    @property
    def sign(self) -> int:
        return 1 if self.direction == "right" else -1

    @property
    def tangent_in(self) -> float:
        return (self.chord_azimuth - self.sign * self.delta / 2) % 360

    @property
    def tangent_out(self) -> float:
        return (self.chord_azimuth + self.sign * self.delta / 2) % 360

    @property
    def bulge(self) -> float:
        return -self.sign * math.tan(math.radians(self.delta) / 4)

    @property
    def segment_area(self) -> float:
        """Area between the arc and its chord."""
        d = math.radians(self.delta)
        return self.radius**2 / 2 * (d - math.sin(d))


def solve_curve(
    direction: str,
    *,
    radius: float | None = None,
    arc: float | None = None,
    delta: float | None = None,
    chord: float | None = None,
    chord_azimuth: float | None = None,
    tangent_azimuth: float | None = None,
) -> Curve:
    """Solve a curve from whatever the deed gives.

    Needs the size (two of radius / arc / delta / chord) and the orientation:
    the chord bearing, or else the incoming tangent (the previous course, for a
    tangent curve). Deeds over-determine curves all the time; when they do, the
    radius and the arc length win, because those are what surveyors compute
    from and the chord is usually derived and rounded.
    """
    if direction not in ("left", "right"):
        raise ValueError(f"curve direction must be left or right, got {direction!r}")
    if delta is None:
        if radius and arc:
            delta = math.degrees(arc / radius)
        elif radius and chord:
            delta = math.degrees(2 * math.asin(min(1.0, chord / (2 * radius))))
        elif arc and chord:
            # Solve chord/arc = sin(d/2)/(d/2) for d by bisection.
            target, lo, hi = chord / arc, 1e-9, 2 * math.pi - 1e-9
            for _ in range(100):
                mid = (lo + hi) / 2
                if math.sin(mid / 2) / (mid / 2) > target:
                    lo = mid
                else:
                    hi = mid
            delta = math.degrees((lo + hi) / 2)
        else:
            raise ValueError("curve needs two of radius, arc, delta, chord")
    if radius is None:
        d = math.radians(delta)
        if arc:
            radius = arc / d
        elif chord:
            radius = chord / (2 * math.sin(d / 2))
        else:
            raise ValueError("curve needs a radius, arc or chord as well as its delta")
    if chord_azimuth is None:
        if tangent_azimuth is None:
            raise ValueError("curve needs a chord bearing, or a tangent course before it")
        sign = 1 if direction == "right" else -1
        chord_azimuth = (tangent_azimuth + sign * delta / 2) % 360
    return Curve(direction, radius, delta, chord_azimuth)


@dataclass
class Leg:
    """One solved course: a straight line, or a curve when ``curve`` is set."""

    start: Point
    end: Point
    azimuth: float  # of the line, or of the chord for a curve
    length: float  # along the line or the arc
    curve: Curve | None = None


def run_legs(start: Point, courses: list[tuple[float, float] | Curve]) -> list[Leg]:
    """Lay each course end to end from ``start``. A course is (azimuth, distance)
    for a line, or a solved `Curve`."""
    legs: list[Leg] = []
    here = start
    for course in courses:
        if isinstance(course, Curve):
            end = _offset(here, course.chord_azimuth, course.chord)
            legs.append(Leg(here, end, course.chord_azimuth, course.arc, course))
        else:
            azimuth, dist = course
            end = _offset(here, azimuth, dist)
            legs.append(Leg(here, end, azimuth, dist))
        here = end
    return legs


def rotate(azimuth: float, by: float) -> float:
    return (azimuth + by) % 360


# --------------------------------------------------------------------------- #
# Closure                                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class Closure:
    misclosure: float  # feet
    misclosure_azimuth: float  # direction from the last point back to the first
    perimeter: float
    area_sqft: float

    @property
    def precision(self) -> float | None:
        """Denominator of the 1:N ratio, or None for a perfect close."""
        return self.perimeter / self.misclosure if self.misclosure > 1e-9 else None

    @property
    def area_acres(self) -> float:
        return self.area_sqft / 43_560


def closure(legs: list[Leg]) -> Closure:
    """Misclosure, perimeter and area of a boundary.

    Area is the coordinate (shoelace) method over the chords, plus or minus the
    circular segment of each curve, closed on the chord from the last point back
    to the first. That closing chord is how every COGO package reports the area
    of a figure that doesn't quite close.
    """
    if not legs:
        return Closure(0.0, 0.0, 0.0, 0.0)
    first, last = legs[0].start, legs[-1].end
    pts = [leg.start for leg in legs] + [last]
    twice = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1], strict=True))
    signed = twice / 2  # positive for counter-clockwise
    for leg in legs:
        if leg.curve:
            # A curve to the left bulges to the right of its chord: outward
            # for a counter-clockwise figure, so it adds area there.
            signed += leg.curve.segment_area * (1 if leg.curve.direction == "left" else -1)
    return Closure(
        misclosure=distance_between(last, first),
        misclosure_azimuth=azimuth_between(last, first),
        perimeter=sum(leg.length for leg in legs),
        area_sqft=abs(signed),
    )


@dataclass
class Suspect:
    """One single-field change to one course that would make the figure close."""

    course: int  # 0-based, among the boundary courses
    change: str
    misclosure_after: float


@dataclass
class BlunderReport:
    suspects: list[Suspect] = field(default_factory=list)


def find_blunders(courses: list[tuple[float, float] | Curve], limit: int = 3) -> BlunderReport:
    """Rank the single-course mistakes that best explain a misclosure.

    The classic blunder check: a figure that misses by more than rounding
    usually has exactly one bad call. For each line course, try

    * the distance that absorbs the misclosure along that course
      (a dropped or doubled digit, 1,028.7 read as 1,082.7);
    * the bearing turned so the course absorbs the misclosure across it;
    * the quadrant letters flipped (N for S, E for W), the most common
      transcription mistake on old deeds.

    and rank by how well each closes the figure. Curves are left alone: they
    are over-determined, so a single bad field in one shows up as a conflict
    between its own parts instead.
    """
    base_legs = run_legs((0.0, 0.0), courses)
    gap = closure(base_legs)
    if gap.misclosure < 1e-6:
        return BlunderReport()
    suspects: list[Suspect] = []

    def misclose_with(i: int, replacement: tuple[float, float]) -> float:
        trial = list(courses)
        trial[i] = replacement
        legs = run_legs((0.0, 0.0), trial)
        return distance_between(legs[-1].end, legs[0].start)

    for i, course in enumerate(courses):
        if isinstance(course, Curve):
            continue
        az, dist = course
        # Misclosure vector (last -> first), split along and across this course.
        mx = gap.misclosure * math.sin(math.radians(gap.misclosure_azimuth))
        my = gap.misclosure * math.cos(math.radians(gap.misclosure_azimuth))
        ux, uy = math.sin(math.radians(az)), math.cos(math.radians(az))
        along = mx * ux + my * uy
        if dist + along > 0:
            new = (az, dist + along)
            after = misclose_with(i, new)
            suspects.append(Suspect(i, f"distance {dist:.2f} -> {dist + along:.2f} ft", after))
        # Rotate this course so its end point moves across by the perpendicular part.
        new_az = math.degrees(math.atan2(dist * ux + mx, dist * uy + my)) % 360
        after = misclose_with(i, (new_az, dist))
        suspects.append(
            Suspect(i, f"bearing {format_bearing(az)} -> {format_bearing(new_az)}", after)
        )
        for label, flipped in (
            ("N/S flipped", (180 - az) % 360),
            ("E/W flipped", (360 - az) % 360),
        ):
            after = misclose_with(i, (flipped, dist))
            suspects.append(
                Suspect(i, f"bearing {format_bearing(az)} -> {format_bearing(flipped)} ({label})",
                        after)
            )  # fmt: skip

    suspects.sort(key=lambda s: s.misclosure_after)
    return BlunderReport([s for s in suspects if s.misclosure_after < gap.misclosure][:limit])


# --------------------------------------------------------------------------- #
# Offsets (strip easements described by a centerline)                          #
# --------------------------------------------------------------------------- #


def offset_polyline(points: list[Point], distance: float) -> list[Point]:
    """Parallel offset of an open polyline, positive to the right, mitred at
    each vertex. For strip easements: the deed gives the centerline and a
    width, and the side lines are what gets drawn.

    ponytail: straight segments only and no clipping to property lines ("side
    lines shortened or extended to intersect..."), which a surveyor does in
    CAD in seconds; add clipping if the side lines need to be exact.
    """
    if len(points) < 2:
        return list(points)

    def shifted(a: Point, b: Point) -> tuple[Point, Point]:
        az = math.radians(azimuth_between(a, b) + 90)
        dx, dy = distance * math.sin(az), distance * math.cos(az)
        return (a[0] + dx, a[1] + dy), (b[0] + dx, b[1] + dy)

    segs = [shifted(a, b) for a, b in zip(points, points[1:], strict=False)]
    out = [segs[0][0]]
    for (a1, a2), (b1, b2) in zip(segs, segs[1:], strict=False):
        out.append(intersect(a1, a2, b1, b2) or a2)
    out.append(segs[-1][1])
    return out


def intersect(a1: Point, a2: Point, b1: Point, b2: Point) -> Point | None:
    """Where line a1-a2 meets line b1-b2 (infinite lines), or None if parallel."""
    d = (a2[0] - a1[0]) * (b2[1] - b1[1]) - (a2[1] - a1[1]) * (b2[0] - b1[0])
    if abs(d) < 1e-12:
        return None
    t = ((b1[0] - a1[0]) * (b2[1] - b1[1]) - (b1[1] - a1[1]) * (b2[0] - b1[0])) / d
    return (a1[0] + t * (a2[0] - a1[0]), a1[1] + t * (a2[1] - a1[1]))
