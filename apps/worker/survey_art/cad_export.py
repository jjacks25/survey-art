"""Write a plotted property to CAD: a DXF, a PNEZD point file and the QC report.

The DXF targets AutoCAD Civil 3D (and opens in Carlson, Trimble Business Center,
BricsCAD and anything else that reads DXF R2010). Layers follow the US National
CAD Standard's survey discipline (``V-``). Every source document is its own
block, named by reception number and inserted at the origin, so a surveyor can
switch a whole document off or move it as a unit.

Coordinates are Colorado State Plane North (EPSG:2231), US survey feet, for every
description tied to a PLSS corner; see `deed_plot` for the ones that aren't.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment

from survey_art import cogo, plss
from survey_art.deed_plot import PlotRun, Plotted

# NCS layer names, with AutoCAD colour index and linetype.
LAYERS = {
    "V-PROP-LINE": (7, "Continuous"),  # subject (vesting deed) boundary
    "V-PROP-LINE-EXCP": (1, "DASHED"),  # exceptions out of the subject
    "V-PROP-ESMT": (3, "DASHED2"),  # easements
    "V-PROP-TIE": (8, "HIDDEN"),  # commencement / tie courses
    "V-PROP-TEXT": (2, "Continuous"),  # labels, area and closure notes
    "V-NODE": (4, "Continuous"),  # POB and PLSS corner points
}
_BOUNDARY_LAYER = {"subject": "V-PROP-LINE", "exception": "V-PROP-LINE-EXCP",
                   "easement": "V-PROP-ESMT"}  # fmt: skip

# Text sized for a section-scale sheet (1" = 200'); Civil 3D users rescale anyway.
TEXT_HEIGHT = 8.0


def _readable(azimuth: float) -> float:
    """CAD text rotation (counter-clockwise from east) that runs along a line
    of this azimuth and never reads upside down."""
    angle = (90 - azimuth) % 360
    return angle - 180 if 90 < angle <= 270 else angle


def _label(block, text: str, at: cogo.Point, rotation: float, *, above: bool = True) -> None:
    offset = TEXT_HEIGHT * 0.8 * (1 if above else -1)
    r = math.radians(rotation + 90)
    pos = (at[0] + offset * math.cos(r), at[1] + offset * math.sin(r))
    block.add_text(
        text, height=TEXT_HEIGHT, rotation=rotation, dxfattribs={"layer": "V-PROP-TEXT"}
    ).set_placement(pos, align=TextEntityAlignment.MIDDLE_CENTER)


def _leg_label(leg: cogo.Leg) -> str:
    if leg.curve:
        c = leg.curve
        return f"R={c.radius:.2f}' L={c.arc:.2f}' Δ={_dms(c.delta)}"
    return f"{cogo.format_bearing(leg.azimuth)}  {leg.length:.2f}'"


def _dms(degrees: float) -> str:
    seconds = round(degrees * 3600)
    d, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{d}°{m:02d}'{s:02d}\""


def _add_legs(block, legs: list[cogo.Leg], layer: str, *, labels: bool) -> None:
    if not legs:
        return
    points = [
        (leg.start[0], leg.start[1], 0, 0, leg.curve.bulge if leg.curve else 0) for leg in legs
    ]
    points.append((legs[-1].end[0], legs[-1].end[1], 0, 0, 0))
    block.add_lwpolyline(points, format="xyseb", dxfattribs={"layer": layer})
    if labels:
        for leg in legs:
            mid = ((leg.start[0] + leg.end[0]) / 2, (leg.start[1] + leg.end[1]) / 2)
            _label(block, _leg_label(leg), mid, _readable(leg.azimuth))


def _summary(p: Plotted) -> list[str]:
    lines = [p.label, f"Rec. No. {p.reception}"]
    if p.area_acres is not None:
        called = f" (called {p.called_acres:.3f})" if p.called_acres else ""
        lines.append(f"{p.area_acres:.3f} ac{called}")
    if p.closure:
        ratio = f"1:{p.closure.precision:,.0f}" if p.closure.precision else "exact"
        lines.append(f"Closure {p.closure.misclosure:.2f}' ({ratio})")
    if p.width:
        lines.append(f"{p.width:g}' wide on centerline")
    if not p.georeferenced:
        lines.append("NOT GEOREFERENCED")
    return lines


def _centroid(points: list[cogo.Point]) -> cogo.Point:
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


def write_dxf(run: PlotRun, path: Path) -> None:
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.FT
    doc.header["$MEASUREMENT"] = 0  # imperial
    for name, (color, linetype) in LAYERS.items():
        doc.layers.add(name, color=color, linetype=linetype)
    msp = doc.modelspace()

    for result in run.results:
        if not any(p.legs or p.ring for p in result.plotted):
            continue
        block = doc.blocks.new(name=f"DOC-{result.doc.reception}")
        for p in result.plotted:
            layer = _BOUNDARY_LAYER.get(p.role, "V-PROP-LINE-EXCP")
            _add_legs(block, p.tie_legs, "V-PROP-TIE", labels=True)
            _add_legs(block, p.tie_out_legs, "V-PROP-TIE", labels=True)
            if p.ring:
                for i in range(0, len(p.ring), 5):
                    block.add_lwpolyline(p.ring[i : i + 4], close=True, dxfattribs={"layer": layer})
            elif p.shape == "centerline":
                _add_legs(block, p.legs, layer, labels=True)
                if p.width:
                    line = [p.legs[0].start] + [leg.end for leg in p.legs]
                    for side in (p.width / 2, -p.width / 2):
                        block.add_lwpolyline(
                            cogo.offset_polyline(line, side), dxfattribs={"layer": layer}
                        )
            else:
                _add_legs(block, p.legs, layer, labels=True)
            if p.pob:
                block.add_point(p.pob, dxfattribs={"layer": "V-NODE"})
                _label(block, "POB", p.pob, 0, above=False)
            if p.tie_legs:
                block.add_point(p.tie_legs[0].start, dxfattribs={"layer": "V-NODE"})
            outline = p.ring[:4] or [leg.start for leg in p.legs]
            if outline:
                at = _centroid(outline)
                block.add_mtext(
                    "\\P".join(_summary(p)),
                    dxfattribs={"layer": "V-PROP-TEXT", "char_height": TEXT_HEIGHT},
                ).set_location(at, attachment_point=5)
        msp.add_blockref(block.name, (0, 0), dxfattribs={"layer": "0"})

    note = (
        f"Plotted from recorded documents. Coordinates: Colorado State Plane North "
        f"(EPSG:{plss.GRID_EPSG}), US survey feet, on BLM PLSS corners. "
        "Not a survey; check every description against the QC report."
    )
    extents = [pt for r in run.results for p in r.plotted for pt in _points(p)]
    if extents:
        x = min(e[0] for e in extents)
        y = min(e[1] for e in extents) - 10 * TEXT_HEIGHT
        msp.add_mtext(
            note, dxfattribs={"layer": "V-PROP-TEXT", "char_height": TEXT_HEIGHT, "width": 2000}
        ).set_location((x, y))
    doc.saveas(path)


def _points(p: Plotted) -> list[cogo.Point]:
    pts = list(p.ring)
    for legs in (p.tie_legs, p.legs, p.tie_out_legs):
        pts += [leg.start for leg in legs] + [leg.end for leg in legs]
    return pts


def write_points(run: PlotRun, path: Path) -> None:
    """PNEZD (point, northing, easting, elevation, description), the format
    Civil 3D and Carlson import without a format definition."""
    number = 1
    with path.open("w", newline="") as f:
        out = csv.writer(f)
        for result in run.results:
            for p in result.plotted:
                tag = f"{result.doc.reception} {p.label}"
                if p.tie_legs:
                    out.writerow([number, *_ne(p.tie_legs[0].start), 0, f"POC {tag}"])
                    number += 1
                vertices = p.ring or [leg.start for leg in p.legs]
                for i, pt in enumerate(vertices):
                    desc = f"POB {tag}" if i == 0 and p.legs else tag
                    out.writerow([number, *_ne(pt), 0, desc])
                    number += 1


def _ne(pt: cogo.Point) -> tuple[str, str]:
    return f"{pt[1]:.3f}", f"{pt[0]:.3f}"


def qc_rows(run: PlotRun) -> list[dict]:
    """One row per document, with its descriptions: what the UI's CAD tab shows."""
    rows = []
    for r in run.results:
        rows.append(
            {
                "reception": r.doc.reception,
                "role": r.doc.role,
                "title": r.doc.title,
                "source": r.parse.source,
                "error": r.parse.error,
                "notes": r.parse.extract.notes,
                "descriptions": [
                    {
                        "label": p.label,
                        "shape": p.shape,
                        "status": p.status,
                        "warnings": p.warnings,
                        "note": p.note,
                        "georeferenced": p.georeferenced,
                        "rotationDeg": round(p.rotation, 4),
                        "courses": len(p.legs),
                        "misclosureFt": round(p.closure.misclosure, 3) if p.closure else None,
                        "precision": round(p.closure.precision)
                        if p.closure and p.closure.precision
                        else None,
                        "areaAcres": round(p.area_acres, 3) if p.area_acres is not None else None,
                        "calledAcres": p.called_acres,
                        "tieOutMissFt": round(p.tie_out_miss, 2) if p.tie_out_miss else None,
                        "suspects": [
                            {
                                "course": s.course + 1,
                                "change": s.change,
                                "misclosureAfterFt": round(s.misclosure_after, 2),
                            }
                            for s in p.suspects
                        ],  # fmt: skip
                    }
                    for p in r.plotted
                ],
            }
        )
    return rows


def write_qc(run: PlotRun, json_path: Path, csv_path: Path) -> None:
    rows = qc_rows(run)
    json_path.write_text(json.dumps({"crs": f"EPSG:{plss.GRID_EPSG}", "documents": rows}, indent=1))
    with csv_path.open("w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["Reception", "Role", "Document", "Description", "Status", "Area (ac)",
                      "Called (ac)", "Misclosure (ft)", "Precision", "Rotation (deg)",
                      "Issues"])  # fmt: skip
        for row in rows:
            if not row["descriptions"]:
                reason = row["error"] or row["notes"] or "no plottable land description"
                out.writerow([row["reception"], row["role"], row["title"], "", "not plotted",
                              "", "", "", "", "", reason])  # fmt: skip
            for d in row["descriptions"]:
                issues = (
                    ([d["note"]] if d["note"] else [])
                    + d["warnings"]
                    + [f"suspect course {s['course']}: {s['change']}" for s in d["suspects"]]
                )
                out.writerow([
                    row["reception"], row["role"], row["title"], d["label"], d["status"],
                    d["areaAcres"], d["calledAcres"], d["misclosureFt"],
                    f"1:{d['precision']:,}" if d["precision"] else "", d["rotationDeg"],
                    "; ".join(issues),
                ])  # fmt: skip


def export(run: PlotRun, out_dir: Path, *, name: str = "property") -> list[Path]:
    """Write every output for ``run`` into ``out_dir``; returns the files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dxf, points = out_dir / f"{name}_deeds.dxf", out_dir / f"{name}_points.csv"
    qc_json, qc_csv = out_dir / "qc.json", out_dir / f"{name}_qc_report.csv"
    write_dxf(run, dxf)
    write_points(run, points)
    write_qc(run, qc_json, qc_csv)
    return [dxf, points, qc_csv, qc_json]
