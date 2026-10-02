"""Phase 5 (road right-of-way) — the parts that don't need the network."""

from __future__ import annotations

from survey_art.scrapers.weld_road_row import (
    _BOCC_ROAD_NOTE,
    _BOCC_ROAD_TYPE,
    _milepost,
    road_row_references,
)


def test_milepost_is_the_measure_nearest_the_parcel():
    parcel = {"rings": [[[-104.87, 40.40], [-104.87, 40.41], [-104.86, 40.41]]]}
    route = {"paths": [[[-104.90, 40.40, 1.0], [-104.871, 40.405, 4.4], [-104.80, 40.40, 9.0]]]}
    assert _milepost(route, parcel) == 4.4


def test_road_exceptions_are_marked_found_or_not():
    ids = [
        # R1611986's ALTA: Book 86 Page 273 resolves to the 1889 BOCC record.
        {
            "id": "Book 86 Page 273",
            "id_type": "book_page",
            "context": "Right of way for County Roads recorded October 14, 1889",
        },
        {
            "id": "1511418",
            "id_type": "reception_number",
            "context": "right of way for Colorado State Highway No. 16",
        },
        {
            "id": "Book 956 Page 71",
            "id_type": "book_page",
            "context": "Parcel conveyed to County of Weld and State of Colorado",
        },
        {"id": "2661201", "id_type": "reception_number", "context": "20' sewer easement"},
    ]
    rows = road_row_references(ids, {"Book 86 Page 273": "34283"}, {"34283", "1511418"})
    assert [(r["id"], r["status"]) for r in rows] == [
        ("Book 86 Page 273", "downloaded"),
        ("1511418", "downloaded"),
        ("Book 956 Page 71", "not located"),
    ]


def test_bocc_road_filter():
    # Real S15-T5N-R67W rows: only the 1936 road file is a road record.
    assert _BOCC_ROAD_TYPE.search("RDF - Road File Only")
    assert _BOCC_ROAD_NOTE.search("HWY257")
    assert not _BOCC_ROAD_TYPE.search("USR - Use by Special Review")
    assert not _BOCC_ROAD_TYPE.search("TXCERT - Tax Sale Certificate")
    assert not _BOCC_ROAD_NOTE.search("USR291\nGRAVEL PIT OPERATION")
