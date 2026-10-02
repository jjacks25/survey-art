"""Turn a property's recorded documents into a CAD drawing and a QC report.

Pipeline, per document: `deed_parse` transcribes its land descriptions, this
module solves each one with `cogo` and places it on the PLSS grid via `plss`,
then `cad_export` writes the DXF, a point file and the QC report.

Run it locally on a scraped folder:

    uv run --directory apps/worker python -m survey_art.deed_plot tmp/CO_weld/R1611986
    uv run --directory apps/worker python -m survey_art.deed_plot --eval \\
        tmp/CO_weld/R1611986 tests/deed_ground_truth.json

Placement. A description tied to a section or quarter corner starts at that
corner's State Plane coordinates from the BLM. Its bearings are then turned onto
the grid: by the deed's own basis of bearings when it states one, otherwise by
the corners its courses run to ("...to the northwest corner of Section 15"). A
description that names no PLSS corner can't be placed, so it is drawn at a local
origin and flagged.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from survey_art import cogo, plss
from survey_art.deed_parse import (
    CornerRef,
    Course,
    DeedParse,
    DocumentExtract,
    LegalDescription,
    cache_fingerprint,
    parse_document,
)
from survey_art.doc_classify import (
    FINANCING,
    LIENS,
    MINERAL,
    NOTICES,
    SURVEYS,
    classify,
)

logger = logging.getLogger(__name__)
narration = logging.getLogger("survey_art.narration")

# Where a description with no PLSS tie is drawn, in local feet.
LOCAL_ORIGIN: cogo.Point = (5000.0, 5000.0)

# QC thresholds. A deed that misses by less than either of these is "ok".
# 0.5 ft covers old highway deeds called to a tenth of a foot (they close to
# ~0.1 ft); 1:10,000 is the usual standard for a boundary description.
CLOSURE_OK_FT = 0.5
PRECISION_OK = 10_000
AREA_OK_FRACTION = 0.01
# A basis-of-bearings or corner-fit rotation bigger than this is far more
# likely a misread corner than a real difference in bases, so it isn't applied.
MAX_ROTATION_DEG = 3.0

# Categories that never carry a plottable boundary of their own. Everything else
# is read: "Parcel of land conveyed to the Department of Highways" classifies as
# Other, and it is exactly the kind of exception the drawing is for.
_SKIP_CATEGORIES = {FINANCING, LIENS, MINERAL, NOTICES, SURVEYS}


# --------------------------------------------------------------------------- #
# Solving one description                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class Plotted:
    """One legal description, solved and placed."""

    reception: str
    role: str  # "subject" | "exception" | "easement"
    label: str
    shape: str
    tie_legs: list[cogo.Leg] = field(default_factory=list)
    legs: list[cogo.Leg] = field(default_factory=list)
    tie_out_legs: list[cogo.Leg] = field(default_factory=list)
    ring: list[cogo.Point] = field(default_factory=list)  # aliquot parcels
    width: float | None = None
    closure: cogo.Closure | None = None
    area_acres: float | None = None
    called_acres: float | None = None
    rotation: float = 0.0
    georeferenced: bool = False
    tie_out_miss: float | None = None
    suspects: list[cogo.Suspect] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    note: str = ""  # why a description was deliberately left off the drawing

    @property
    def status(self) -> str:
        """'ok', 'check' (plotted but a QC test failed), 'info' (deliberately
        not drawn, see ``note``) or 'failed' (couldn't be plotted)."""
        if not self.legs and not self.ring:
            return "info" if self.note and not self.warnings else "failed"
        if self.warnings:
            return "check"
        return "ok"

    @property
    def pob(self) -> cogo.Point | None:
        return self.legs[0].start if self.legs else None


Locator = Callable[[CornerRef], cogo.Point | None]


def _acres(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(acres?|ac\b|square feet|sq\.?\s*ft|sf\b)", text, re.I)
    if not m:
        return None
    value = float(m[1].replace(",", ""))
    return value if m[2].lower().startswith("ac") else value / 43_560


def _line(course: Course) -> tuple[float, float]:
    if not course.bearing or not course.distance:
        raise ValueError("line course is missing its bearing or distance")
    az = cogo.parse_bearing(course.bearing)
    return ((az + 180) % 360 if course.reverse else az), cogo.parse_distance(course.distance)


def _curve(course: Course, tangent: float | None) -> cogo.Curve:
    c = course.curve
    if c is None:
        raise ValueError("curve course has no curve data")

    def num(text: str | None, parse) -> float | None:
        return parse(text) if text else None

    return cogo.solve_curve(
        c.direction,
        radius=num(c.radius, cogo.parse_distance),
        arc=num(c.arc, cogo.parse_distance),
        delta=num(c.delta, cogo.parse_angle),
        chord=num(c.chord, cogo.parse_distance),
        chord_azimuth=num(c.chord_bearing, cogo.parse_bearing),
        tangent_azimuth=tangent,
    )


def parse_courses(desc: LegalDescription) -> list[tuple[float, float] | cogo.Curve]:
    """Every course of ``desc`` parsed, in order. A curve with no chord bearing
    takes its tangent from the course before it."""
    parsed: list[tuple[float, float] | cogo.Curve] = []
    tangent: float | None = None
    for course in desc.courses:
        if course.kind == "curve":
            curve = _curve(course, tangent)
            parsed.append(curve)
            tangent = curve.tangent_out
        else:
            line = _line(course)
            parsed.append(line)
            tangent = line[0]
    return parsed


def _rotated(course: tuple[float, float] | cogo.Curve, by: float):
    if isinstance(course, cogo.Curve):
        return cogo.Curve(
            course.direction, course.radius, course.delta, cogo.rotate(course.chord_azimuth, by)
        )
    return (cogo.rotate(course[0], by), course[1])


def _wrap(angle: float) -> float:
    return (angle + 180) % 360 - 180


def solve(
    desc: LegalDescription, locate: Locator, *, reception: str = "", role: str = ""
) -> Plotted:
    """Solve one description and place it. Never raises; problems become warnings."""
    out = Plotted(reception, role, desc.label, desc.shape)
    out.called_acres = _acres(desc.called_area)
    try:
        _solve_into(out, desc, locate)
    except (ValueError, ZeroDivisionError) as exc:
        out.warnings.append(f"could not solve: {exc}")
    return out


def _solve_into(out: Plotted, desc: LegalDescription, locate: Locator) -> None:
    if desc.shape == "aliquot" and out.role != "subject":
        # "An easement across the SW1/4" or "a request for notice on all of
        # Section 15" names the land the document touches, not a boundary of
        # its own. Drawing a quarter section on the easement layer would read
        # as a 160-acre easement, so it's listed instead.
        parts = ", ".join(
            f"{p.aliquot or 'all of'} Sec. {p.section} T{p.township} R{p.range}"
            for p in desc.aliquot_parts
        )
        out.note = f"lies within {parts}; no boundary of its own to draw"
        return
    if desc.shape == "aliquot":
        for part in desc.aliquot_parts:
            ref = CornerRef(**part.model_dump(), corner="NW")
            corners = [
                locate(ref.model_copy(update={"corner": c})) for c in ("NW", "NE", "SE", "SW")
            ]
            if any(c is None for c in corners):
                out.warnings.append(
                    f"no PLSS geometry for {part.aliquot or 'all of'} Section {part.section} "
                    f"T{part.township} R{part.range}"
                )
                continue
            # ponytail: several parts are drawn as separate rings, not unioned;
            # "the N1/2 and the SE1/4" shows the shared line. Union when it matters.
            out.ring.extend(corners)  # type: ignore[arg-type]
            out.ring.append(corners[0])  # type: ignore[arg-type]
        if out.ring:
            out.georeferenced = True
            rings = [out.ring[i : i + 5] for i in range(0, len(out.ring), 5)]
            out.area_acres = sum(plss.polygon_area(r[:4]) for r in rings) / 43_560
        _check_area(out)
        return

    # 1. Every course as (azimuth, distance) or a solved curve, on the deed's
    #    own bearing basis.
    roles = [c.role for c in desc.courses]
    parsed = parse_courses(desc)

    # 2. Where does it start?
    start = locate(desc.start) if desc.start else None
    out.georeferenced = start is not None
    if start is None:
        start = LOCAL_ORIGIN
        out.warnings.append(
            "not tied to a PLSS corner we could find, so drawn at a local origin"
            if desc.start is None
            else "its PLSS corner could not be found, so drawn at a local origin"
        )

    # 3. Turn the bearings onto the grid.
    if out.georeferenced:
        out.rotation = _rotation(desc, parsed, start, locate, out.warnings)
        parsed = [_rotated(c, out.rotation) for c in parsed]

    # 4. Lay the courses out.
    here = start
    for kind, legs_out in (("tie_in", out.tie_legs), ("boundary", out.legs),
                           ("tie_out", out.tie_out_legs)):  # fmt: skip
        chunk = [c for c, r in zip(parsed, roles, strict=True) if r == kind]
        legs_out.extend(cogo.run_legs(here, chunk))
        if legs_out:
            here = legs_out[-1].end

    if not out.legs:
        out.warnings.append("no boundary courses")
        return

    # 5. QC.
    boundary = [c for c, r in zip(parsed, roles, strict=True) if r == "boundary"]
    if desc.shape == "parcel":
        out.closure = cogo.closure(out.legs)
        out.area_acres = out.closure.area_acres
        if out.closure.misclosure > CLOSURE_OK_FT and (out.closure.precision or 0) < PRECISION_OK:
            out.warnings.append(
                f"does not close: misses by {out.closure.misclosure:.2f} ft "
                f"(1:{out.closure.precision or 0:,.0f})"
            )
            out.suspects = cogo.find_blunders(boundary).suspects
    else:
        out.width = cogo.parse_distance(desc.width) if desc.width else None
        if out.width:
            out.area_acres = sum(leg.length for leg in out.legs) * out.width / 43_560
    _check_area(out)

    if desc.end and out.tie_out_legs and out.georeferenced:
        target = locate(desc.end)
        if target is not None:
            out.tie_out_miss = cogo.distance_between(out.tie_out_legs[-1].end, target)
            if out.tie_out_miss > max(5.0, 0.002 * sum(leg.length for leg in out.legs)):
                out.warnings.append(
                    f"closing tie lands {out.tie_out_miss:.1f} ft from the corner it names"
                )


def _check_area(out: Plotted) -> None:
    if out.called_acres and out.area_acres is not None:
        off = abs(out.area_acres - out.called_acres) / out.called_acres
        if off > AREA_OK_FRACTION:
            out.warnings.append(
                f"computed {out.area_acres:.3f} ac against {out.called_acres:.3f} ac called "
                f"({off:.1%} off)"
            )


def _rotation(
    desc: LegalDescription,
    parsed: list,
    start: cogo.Point,
    locate: Locator,
    warnings: list[str],
) -> float:
    """Degrees to turn the deed's bearings onto the State Plane grid.

    The deed's basis of bearings when it states one. Otherwise the corners its
    courses run to: lay the courses out unrotated from ``start`` and compare the
    direction to each named corner with the BLM's. Zero when there's nothing to
    compare against, which is honest: the deed's basis is unknown, and the
    QC report says the description wasn't rotated.
    """
    if desc.basis:
        a, b = locate(desc.basis.from_corner), locate(desc.basis.to_corner)
        if a is None or b is None:
            warnings.append("basis of bearings names a corner we could not find; not rotated")
            return 0.0
        rotation = _wrap(cogo.azimuth_between(a, b) - cogo.parse_bearing(desc.basis.bearing))
    else:
        legs = cogo.run_legs(start, parsed)
        diffs: list[tuple[float, float]] = []
        for course, leg in zip(desc.courses, legs, strict=True):
            target = locate(course.ends_at) if course.ends_at else None
            reach = cogo.distance_between(start, leg.end)
            if target is None or reach < 100 or cogo.distance_between(start, target) < 100:
                continue
            diffs.append(
                (_wrap(cogo.azimuth_between(start, target) - cogo.azimuth_between(start, leg.end)),
                 reach)
            )  # fmt: skip
        if not diffs:
            return 0.0
        rotation = sum(d * w for d, w in diffs) / sum(w for _, w in diffs)
    if abs(rotation) > MAX_ROTATION_DEG:
        warnings.append(
            f"bearings would need turning {rotation:+.2f}° to fit the PLSS corners; "
            "that is more likely a misread corner, so it was not applied"
        )
        return 0.0
    return rotation


# --------------------------------------------------------------------------- #
# Locating PLSS corners                                                         #
# --------------------------------------------------------------------------- #


def make_locator(state: str = "CO") -> Locator:
    """A `Locator` backed by the BLM service. Unreachable or unknown sections
    return None (and are only asked for once)."""
    failed: set[tuple] = set()

    def locate(ref: CornerRef) -> cogo.Point | None:
        key = (ref.meridian, ref.township, ref.range, ref.section)
        if key in failed:
            return None
        try:
            section = plss.fetch_section(state, *key)
            return plss.subdivide(section, ref.aliquot).corner(plss.corner_name(ref.corner))
        except LookupError as exc:
            logger.warning("deed_plot: %s", exc)
        except ValueError as exc:
            logger.warning("deed_plot: cannot locate %s: %s", ref.model_dump(), exc)
            return None
        except Exception as exc:  # noqa: BLE001 — network trouble means "not georeferenced"
            logger.warning("deed_plot: BLM PLSS lookup failed for %s: %s", key, exc)
        failed.add(key)
        return None

    return locate


# --------------------------------------------------------------------------- #
# Choosing and reading documents                                                #
# --------------------------------------------------------------------------- #


@dataclass
class SourceDoc:
    reception: str
    role: str  # "subject" | "exception" | "easement"
    path: Path
    title: str = ""


def select_documents(folder: Path) -> list[SourceDoc]:
    """The vesting deed plus every exception or easement worth reading.

    Uses the run's overview.json for what each exception is; falls back to
    the file names (``vesting_deed_*.pdf``, ``exception_*.pdf``) without it.
    """
    titles: dict[str, str] = {}
    overview = folder / "overview.json"
    if overview.is_file():
        try:
            data = json.loads(overview.read_text())
            for t in (data.get("phase_3a") or {}).get("targets", []):
                titles[str(t.get("reception", "")).lstrip("0")] = t.get("doc_type", "")
            for d in data.get("documents") or []:
                titles.setdefault(str(d.get("reception", "")).lstrip("0"), d.get("doc_type", ""))
        except (ValueError, AttributeError) as exc:
            logger.warning("deed_plot: unreadable overview.json: %s", exc)

    docs: list[SourceDoc] = []
    for path in sorted(folder.glob("*.pdf")):
        m = re.match(r"(vesting_deed|exception)_0*(\d+)\.pdf$", path.name, re.I)
        if not m:
            continue
        reception = m[2]
        title = titles.get(reception, "")
        if m[1].lower() == "vesting_deed":
            docs.append(SourceDoc(reception, "subject", path, title or "Vesting deed"))
            continue
        category = classify(title) if title else ""
        if category in _SKIP_CATEGORIES:
            continue
        role = (
            "easement"
            if "easement" in category.lower() or "easement" in title.lower()
            else ("exception")
        )
        docs.append(SourceDoc(reception, role, path, title))
    # The subject first: a later document repeating its land ("all of Section
    # 15") is then the duplicate that gets dropped, not the vesting deed.
    docs.sort(key=lambda d: d.role != "subject")
    return docs


def read_document(doc: SourceDoc, *, use_cache: bool = True) -> DeedParse:
    """`parse_document`, behind the same S3 cache the citation reader uses."""
    if not use_cache:
        return parse_document(doc.path)
    from survey_shared import jobs  # local: the CLI runs without AWS settings

    fingerprint = cache_fingerprint()
    try:
        cached = jobs.get_cached_extraction(fingerprint, doc.reception)
    except Exception:  # noqa: BLE001 — no AWS wiring locally means no cache
        cached = None
    if cached is not None:
        try:
            return DeedParse(extract=DocumentExtract.model_validate(cached), source="cache")
        except ValueError:
            logger.warning("deed_plot: ignoring malformed cached parse for %s", doc.reception)
    result = parse_document(doc.path)
    if result.source in ("bedrock", "text_layer"):
        try:
            jobs.put_cached_extraction(fingerprint, doc.reception, result.extract.model_dump())
        except Exception:  # noqa: BLE001
            pass
    return result


# --------------------------------------------------------------------------- #
# Whole property                                                                #
# --------------------------------------------------------------------------- #


@dataclass
class DocumentResult:
    doc: SourceDoc
    parse: DeedParse
    plotted: list[Plotted] = field(default_factory=list)


@dataclass
class PlotRun:
    results: list[DocumentResult]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def plot_folder(
    folder: Path,
    *,
    state: str = "CO",
    use_cache: bool = True,
    locate: Locator | None = None,
    reader: Callable[[SourceDoc], DeedParse] | None = None,
) -> PlotRun:
    """Read, solve and place every relevant document in ``folder``."""
    import concurrent.futures

    docs = select_documents(folder)
    narration.info(
        f"Reading the land descriptions in {len(docs)} recorded document(s) "
        "(the vesting deed, exceptions and easements)..."
    )
    locate = locate or make_locator(state)
    reader = reader or (lambda d: read_document(d, use_cache=use_cache))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        parses = list(pool.map(reader, docs))

    run = PlotRun([])
    seen: set[str] = set()
    for doc, parse in zip(docs, parses, strict=True):
        run.input_tokens += parse.input_tokens
        run.output_tokens += parse.output_tokens
        run.cost_usd += parse.cost_usd
        result = DocumentResult(doc, parse)
        for desc in parse.extract.descriptions:
            # The same land conveyed twice (a conservator's deed and a warranty
            # deed recorded together) is drawn once.
            key = json.dumps(desc.model_dump(exclude={"label"}), sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            result.plotted.append(solve(desc, locate, reception=doc.reception, role=doc.role))
        run.results.append(result)

    statuses = [p.status for r in run.results for p in r.plotted]
    narration.info(
        f"Drew {statuses.count('ok') + statuses.count('check')} land description(s) from "
        f"{len(docs)} document(s): {statuses.count('ok')} pass every check, "
        f"{statuses.count('check')} need a look, {statuses.count('failed')} couldn't be "
        f"plotted, and {statuses.count('info')} only name the area they lie in."
    )
    return run


# --------------------------------------------------------------------------- #
# CLI                                                                           #
# --------------------------------------------------------------------------- #


def _same_course(a, b) -> bool:
    """Two parsed courses agree to the printed precision (1" and 0.01 ft)."""
    if isinstance(a, cogo.Curve) != isinstance(b, cogo.Curve):
        return False
    if isinstance(a, cogo.Curve):
        return (abs(a.radius - b.radius) < 0.01 and abs(a.delta - b.delta) < 1 / 3600
                and abs(_wrap(a.chord_azimuth - b.chord_azimuth)) < 1 / 3600)  # fmt: skip
    return abs(_wrap(a[0] - b[0])) < 0.5 / 3600 and abs(a[1] - b[1]) < 0.005


def compare(want: LegalDescription, got: LegalDescription, locate: Locator) -> list[str]:
    """What the model got wrong, against a hand-read description. Empty = a match.

    Scores the transcription, not the spelling: courses are compared after
    parsing, and corners by where they resolve, so "the NW corner of the
    NE1/4" and "the NW corner of the NW1/4 NE1/4" are the same corner.
    """
    problems: list[str] = []
    if want.shape != got.shape:
        problems.append(f"shape {got.shape}, expected {want.shape}")
    if want.shape == "aliquot":
        a, b = solve(want, locate), solve(got, locate)
        if len(a.ring) != len(b.ring) or any(
            cogo.distance_between(p, q) > 1 for p, q in zip(a.ring, b.ring, strict=False)
        ):
            problems.append("aliquot parts differ")
        return problems
    if want.derived:
        # The model chose these courses, so only the shape they make counts:
        # the same rectangle walked the other way round is the same answer.
        a, b = solve(want, locate), solve(got, locate)
        pa, pb = [leg.start for leg in a.legs], [leg.start for leg in b.legs]
        if len(pa) != len(pb) or any(min(cogo.distance_between(p, q) for q in pb) > 1 for p in pa):
            problems.append("derived shape differs")
        return problems
    for name in ("start", "end"):
        w, g = getattr(want, name), getattr(got, name)
        pw, pg = (locate(w) if w else None), (locate(g) if g else None)
        if (pw is None) != (pg is None) or (pw and pg and cogo.distance_between(pw, pg) > 1):
            problems.append(f"{name} corner differs")
    try:
        cw, cg = parse_courses(want), parse_courses(got)
    except ValueError as exc:
        return [*problems, f"unparseable course: {exc}"]
    rw = [(c.role, c.reverse) for c in want.courses]
    rg = [(c.role, c.reverse) for c in got.courses]
    if len(cw) != len(cg):
        problems.append(f"{len(cg)} courses, expected {len(cw)}")
    else:
        for n, (a, b, ra, rb) in enumerate(zip(cw, cg, rw, rg, strict=True), 1):
            if not _same_course(a, b) or ra != rb:
                problems.append(f"course {n} differs")
    return problems


def _evaluate(folder: Path, truth_path: Path) -> int:
    """Score the model's transcription against hand-read ground truth."""
    truth = json.loads(truth_path.read_text())["documents"]
    locate = make_locator()
    misses = 0
    tokens = [0, 0]
    for reception, expected in truth.items():
        path = next(folder.glob(f"*_{reception}.pdf"), None)
        if path is None:
            print(f"{reception}: no PDF in {folder}")
            continue
        parse = read_document(SourceDoc(reception, "exception", path), use_cache=False)
        tokens[0] += parse.input_tokens
        tokens[1] += parse.output_tokens
        want = [LegalDescription.model_validate(d) for d in expected["descriptions"]]
        got = parse.extract.descriptions
        if len(got) != len(want):
            problems = [f"{len(got)} descriptions, expected {len(want)}"]
        else:
            problems = [p for w, g in zip(want, got, strict=True) for p in compare(w, g, locate)]
        misses += bool(problems)
        print(f"{reception}: {'ok' if not problems else '; '.join(problems)}")
    print(f"\n{len(truth) - misses}/{len(truth)} documents match; "
          f"{tokens[0]:,} in / {tokens[1]:,} out tokens")  # fmt: skip
    return 1 if misses else 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    from survey_art import cad_export

    parser = argparse.ArgumentParser(prog="python -m survey_art.deed_plot")
    parser.add_argument("folder", type=Path, help="a scraped property folder of PDFs")
    parser.add_argument("truth", type=Path, nargs="?", help="ground-truth JSON (with --eval)")
    parser.add_argument("--eval", action="store_true", help="score the model against truth")
    parser.add_argument("--out", type=Path, help="output folder (default: <folder>/drawing)")
    parser.add_argument("--no-cache", action="store_true", help="skip the S3 parse cache")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.eval:
        if not args.truth:
            parser.error("--eval needs a ground-truth file")
        return _evaluate(args.folder, args.truth)

    out = args.out or args.folder / "drawing"
    # Locally there's no S3 cache, so keep each parse next to the output:
    # re-running after a solver or drawing change then costs nothing.
    parses = out / "parses"
    parses.mkdir(parents=True, exist_ok=True)

    def read_local(doc: SourceDoc) -> DeedParse:
        saved = parses / f"{doc.reception}.json"
        if saved.is_file() and not args.no_cache:
            return DeedParse(extract=DocumentExtract.model_validate_json(saved.read_text()),
                             source="cache")  # fmt: skip
        result = read_document(doc, use_cache=False)
        if result.source in ("bedrock", "text_layer"):
            saved.write_text(result.extract.model_dump_json(indent=1, exclude_defaults=True))
        return result

    run = plot_folder(args.folder, reader=read_local)
    files = cad_export.export(run, out, name=args.folder.name)
    for f in files:
        print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
