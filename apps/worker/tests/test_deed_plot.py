"""Deed plotting: COGO math, PLSS subdivision, solving, and export.

No network and no model: the ground-truth deeds in `deed_ground_truth.json`
are solved directly, and a synthetic square section stands in for the BLM.
The model's transcription is scored separately, against the same file, by
`python -m survey_art.deed_plot --eval` (it costs Bedrock time).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import ezdxf
import pytest

from survey_art import cad_export, cogo, plss
from survey_art.deed_parse import (
    Course,
    CurveCall,
    DeedParse,
    DocumentExtract,
    LegalDescription,
    _clean,
)
from survey_art.deed_plot import PlotRun, SourceDoc, plot_folder, select_documents, solve

TRUTH = json.loads((Path(__file__).parent / "deed_ground_truth.json").read_text())["documents"]


def _descriptions(*receptions: str) -> list[tuple[str, LegalDescription]]:
    return [
        (r, LegalDescription.model_validate(d))
        for r in receptions
        for d in TRUTH[r]["descriptions"]
    ]


# A perfectly square 5280' section per section number, laid out on a grid, so
# locating corners needs no network.
def _square(section: int) -> plss.Quad:
    col, row = (section - 1) % 6, (section - 1) // 6
    x0, y0 = 100_000 - col * 5280, 200_000 - row * 5280
    return plss.Quad.from_corners(
        (x0, y0 + 5280), (x0 + 5280, y0 + 5280), (x0 + 5280, y0), (x0, y0)
    )


def _locate(ref):
    return plss.subdivide(_square(ref.section), ref.aliquot).corner(plss.corner_name(ref.corner))


# --------------------------------------------------------------------------- #
# Parsing                                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "azimuth"),
    [
        ("N. 0° 59' 45\" W.", 360 - (59 / 60 + 45 / 3600)),
        ("north 00°00'00\" east", 0.0),
        ("South 89°25’41” West", 180 + 89 + 25 / 60 + 41 / 3600),
        ("S45-30-00E", 134.5),
        ("North", 0.0),
        ("due West", 270.0),
        ("N 90° E", 90.0),
    ],
)
def test_parse_bearing(text, azimuth):
    assert cogo.parse_bearing(text) == pytest.approx(azimuth % 360, abs=1e-9)


@pytest.mark.parametrize(
    ("text", "feet"),
    [
        ("2,633.8 feet", 2633.8),
        ("214.2 feet, more or less, to the point of beginning", 214.2),
        ("995.83", 995.83),
        ("10 chains 2 links", 661.32),
        ("4 rods", 66.0),
        ("30.0 feet to the south line of Section 15", 30.0),
        ("100 m", 100 * 3937 / 1200),
    ],
)
def test_parse_distance(text, feet):
    assert cogo.parse_distance(text) == pytest.approx(feet)


def test_format_bearing_round_trips():
    for az in (0.0, 12.3456, 90.0, 134.5, 181.25, 270.0, 359.999):
        assert cogo.parse_bearing(cogo.format_bearing(az)) == pytest.approx(az % 360, abs=1 / 3600)


# --------------------------------------------------------------------------- #
# Curves, closure, blunders                                                     #
# --------------------------------------------------------------------------- #


def test_curve_from_radius_and_arc_matches_the_printed_chord():
    # Parcel 29 (1715553): R=1,295.9', L=904.7', chord S 71°50' W 886.5'.
    curve = cogo.solve_curve("right", radius=1295.9, arc=904.7, chord_azimuth=251 + 50 / 60)
    assert curve.chord == pytest.approx(886.5, abs=0.1)
    assert curve.delta == pytest.approx(40.0, abs=0.001)


def test_curve_from_tangent_and_delta():
    curve = cogo.solve_curve("left", radius=100, delta=90, tangent_azimuth=0)
    assert curve.chord_azimuth == pytest.approx(315)
    legs = cogo.run_legs((0, 0), [curve])
    assert legs[0].end == pytest.approx((-100, 100))


def test_closure_area_includes_curve_segments():
    # A half-disc: diameter east, then a semicircle back to the start.
    curve = cogo.solve_curve("left", radius=50, delta=180, chord_azimuth=270)
    legs = cogo.run_legs((0, 0), [(90.0, 100.0), curve])
    result = cogo.closure(legs)
    assert result.misclosure == pytest.approx(0, abs=1e-9)
    assert result.area_sqft == pytest.approx(math.pi * 50**2 / 2)


_PARCELS = _descriptions("1511417", "1512031", "1688529", "1715553", "2369866", "2943083")


@pytest.mark.parametrize(("reception", "desc"), _PARCELS)
def test_ground_truth_parcels_close_and_match_their_called_area(reception, desc):
    plotted = solve(desc, _locate, reception=reception, role="exception")
    assert plotted.closure is not None
    assert plotted.closure.misclosure < 0.15, reception
    if plotted.called_acres:
        assert plotted.area_acres == pytest.approx(plotted.called_acres, abs=0.002)
    assert plotted.georeferenced


def test_centerline_easement_area_is_length_times_width():
    [(_, desc)] = _descriptions("2786305")
    plotted = solve(desc, _locate, role="easement")
    assert plotted.area_acres == pytest.approx(3.708, abs=0.001)
    assert plotted.closure is None


def test_blunder_check_finds_a_flipped_quadrant():
    # A 3-4-5 triangle with its north course misread as south.
    back = math.degrees(math.atan2(-400, -300)) % 360
    bad = [(90.0, 400.0), (180.0, 300.0), (back, 500.0)]
    suspects = cogo.find_blunders(bad).suspects
    assert suspects[0].course == 1
    assert suspects[0].misclosure_after == pytest.approx(0, abs=1e-6)


def test_blunder_check_finds_a_bad_distance():
    bad = [(90.0, 500.0), (0.0, 300.0), (270.0, 550.0), (180.0, 300.0)]
    suspects = cogo.find_blunders(bad).suspects
    assert suspects[0].misclosure_after == pytest.approx(0, abs=1e-6)
    assert "550.00 -> 500.00" in suspects[0].change


def test_offset_polyline_is_parallel():
    side = cogo.offset_polyline([(0, 0), (100, 0), (100, 100)], 15)
    flat = [c for point in side for c in point]
    assert flat == pytest.approx([0, -15, 115, -15, 115, 100], abs=1e-9)


# --------------------------------------------------------------------------- #
# PLSS                                                                          #
# --------------------------------------------------------------------------- #


def test_plss_id():
    assert plss.plss_id("co", "6th P.M.", "5N", "67W") == "CO060050N0670W0"


def test_aliquot_subdivision():
    section = _square(15)
    se_se = plss.subdivide(section, "SE1/4 SE1/4")
    assert plss.polygon_area(se_se.ring()) == pytest.approx(5280**2 / 16)
    assert se_se.se == section.se
    assert plss.polygon_area(plss.subdivide(section, "S1/2").ring()) == pytest.approx(5280**2 / 2)
    assert plss.subdivide(section, "NW1/4").corner("NE") == section.n


@pytest.mark.parametrize("bad", ["N1/4", "NE1/2", "Lot 4", "S1/2 of the river"])
def test_aliquot_rejects_what_it_cannot_subdivide(bad):
    with pytest.raises(ValueError):
        plss.aliquot_tokens(bad)


def test_basis_of_bearings_rotates_onto_the_grid():
    # Turn the synthetic section 1° clockwise; 2369866's basis (west line bears
    # due north) should then rotate its courses by the same 1°.
    def turned(ref):
        x, y = _locate(ref)
        cx, cy = _square(15).sw
        a = math.radians(-1.0)
        dx, dy = x - cx, y - cy
        return (cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a))

    [(_, desc)] = _descriptions("2369866")
    plotted = solve(desc, turned)
    assert plotted.rotation == pytest.approx(1.0, abs=1e-6)
    assert plotted.closure.misclosure < 0.01


# --------------------------------------------------------------------------- #
# Model-output clean-up                                                         #
# --------------------------------------------------------------------------- #


def test_clean_drops_descriptions_without_usable_courses():
    word_strip = LegalDescription(
        label="West 30 feet of the East 50 feet",
        shape="parcel",
        courses=[
            Course(bearing="East", distance="20 feet"),
            Course(bearing="South", distance="to the point of beginning"),
        ],
    )
    sketch = LegalDescription(label="Exhibit A", shape="centerline", width="60 feet")
    out = _clean(DocumentExtract(descriptions=[word_strip, sketch]))
    assert out.descriptions == []
    assert "West 30 feet" in out.notes


def test_clean_starts_a_reverse_tie_at_the_corner_it_names():
    [(_, desc)] = _descriptions("1511417")
    nw = desc.start
    wrong = desc.model_copy(
        update={
            "start": nw.model_copy(update={"corner": "W"}),
            "courses": [desc.courses[0].model_copy(update={"ends_at": nw}), *desc.courses[1:]],
        }
    )
    [fixed] = _clean(DocumentExtract(descriptions=[wrong])).descriptions
    assert fixed.start == nw


def test_clean_never_reverses_a_tie_out():
    [(_, desc)] = _descriptions("2786305")
    courses = [*desc.courses[:-1], desc.courses[-1].model_copy(update={"reverse": True})]
    extract = DocumentExtract(descriptions=[desc.model_copy(update={"courses": courses})])
    [fixed] = _clean(extract).descriptions
    assert fixed.courses[-1].reverse is False


def test_curve_without_enough_data_is_reported_not_raised():
    desc = LegalDescription(
        label="x",
        shape="parcel",
        courses=[
            Course(bearing="N 0 E", distance="100 feet"),
            Course(kind="curve", curve=CurveCall(direction="right", radius="50 feet")),
        ],
    )
    plotted = solve(desc, _locate)
    assert plotted.status == "failed"
    assert "could not solve" in plotted.warnings[0]


# --------------------------------------------------------------------------- #
# A whole folder, end to end                                                    #
# --------------------------------------------------------------------------- #


def test_plot_folder_and_export(tmp_path):
    folder = tmp_path / "R1"
    folder.mkdir()
    for name in ("vesting_deed_4970002.pdf", "exception_1715553.pdf",
                 "exception_2369866.pdf", "exception_2369867.pdf", "exception_2786305.pdf",
                 "exception_1443884.pdf", "alta_4571638.pdf"):  # fmt: skip
        (folder / name).write_bytes(b"%PDF-1.4")

    def reader(doc: SourceDoc) -> DeedParse:
        truth = TRUTH.get(doc.reception) or TRUTH["2369866"]  # 2369867 repeats 2369866
        return DeedParse(
            extract=DocumentExtract.model_validate({"descriptions": truth["descriptions"]}),
            source="bedrock",
            input_tokens=10,
        )

    run = plot_folder(folder, locate=_locate, reader=reader)
    assert [r.doc.reception for r in run.results][0] == "4970002"  # subject first
    assert "4571638" not in [r.doc.reception for r in run.results]  # the ALTA isn't a deed
    by_reception = {r.doc.reception: r for r in run.results}
    assert by_reception["2369867"].plotted == []  # same land as 2369866, drawn once
    assert [p.status for p in by_reception["4970002"].plotted] == ["ok", "ok"]
    assert run.input_tokens == 60

    files = cad_export.export(run, tmp_path / "out", name="R1")
    dxf = ezdxf.readfile(files[0])
    assert {"V-PROP-LINE", "V-PROP-LINE-EXCP", "V-PROP-ESMT", "V-PROP-TIE"} <= {
        layer.dxf.name for layer in dxf.layers
    }
    blocks = {b.name for b in dxf.blocks if b.name.startswith("DOC-")}
    assert blocks == {"DOC-4970002", "DOC-1715553", "DOC-2369866", "DOC-2786305"}
    curve = [e for e in dxf.blocks["DOC-1715553"] if e.dxftype() == "LWPOLYLINE"]
    assert any(any(v[4] for v in e.get_points("xyseb")) for e in curve), "curve lost its bulge"

    qc = json.loads(files[3].read_text())
    rows = {d["reception"]: d for d in qc["documents"]}
    assert rows["1443884"]["descriptions"] == []
    assert rows["1715553"]["descriptions"][0]["areaAcres"] == pytest.approx(5.663, abs=0.001)
    assert "1443884" in files[2].read_text()  # the CSV lists what wasn't plotted, too


def test_select_documents_skips_non_geometric_categories(tmp_path):
    (tmp_path / "exception_1.pdf").write_bytes(b"")
    (tmp_path / "exception_2.pdf").write_bytes(b"")
    (tmp_path / "exception_3.pdf").write_bytes(b"")
    (tmp_path / "overview.json").write_text(json.dumps({"phase_3a": {"targets": [
        {"reception": "1", "doc_type": "Request for notification (mineral estate owner)"},
        {"reception": "2", "doc_type": "Parcel of land conveyed to Department of Highways"},
        {"reception": "3", "doc_type": "Easement - City of Greeley - Underground Pipeline"},
    ]}}))  # fmt: skip
    docs = select_documents(tmp_path)
    assert [(d.reception, d.role) for d in docs] == [("2", "exception"), ("3", "easement")]


def test_plot_run_with_nothing_still_exports(tmp_path):
    files = cad_export.export(PlotRun([]), tmp_path)
    assert all(f.exists() for f in files)
