"""Tests for KMZ account/parcel extraction (app/kmz.py)."""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

from app import kmz

FIXTURE = Path(__file__).parent / "fixtures" / "weld_sample_parcel.kmz"


def _kmz_bytes(kml: str) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml)
    return buf.getvalue()


def test_extracts_account_from_real_weld_sample():
    assert kmz.extract_identifier(FIXTURE.read_bytes()) == "R0001886"


def test_extracts_from_extended_data_simpledata_variant():
    kml = """<?xml version="1.0"?>
    <kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
      <name>Parcel</name>
      <ExtendedData><SchemaData>
        <SimpleData name="PARCELID">1234567890</SimpleData>
      </SchemaData></ExtendedData>
    </Placemark></Document></kml>"""
    assert kmz.extract_identifier(_kmz_bytes(kml)) == "1234567890"


def test_falls_back_to_placemark_name():
    kml = """<?xml version="1.0"?>
    <kml xmlns="http://www.opengis.net/kml/2.2"><Document>
      <Placemark><name>R1611986</name></Placemark>
    </Document></kml>"""
    assert kmz.extract_identifier(_kmz_bytes(kml)) == "R1611986"


def test_returns_none_when_no_identifier_present():
    kml = """<?xml version="1.0"?>
    <kml xmlns="http://www.opengis.net/kml/2.2"><Document>
      <Placemark><name>My Property</name></Placemark>
    </Document></kml>"""
    assert kmz.extract_identifier(_kmz_bytes(kml)) is None


def test_returns_none_for_non_zip_garbage():
    assert kmz.extract_identifier(b"not a zip file") is None


PIPELINE = Path(__file__).parent / "fixtures" / "greeley_west_pipeline.kmz"


def test_drawn_route_names_no_account():
    assert kmz.extract_identifier(PIPELINE.read_bytes()) is None


def test_drawn_route_is_sent_as_one_polyline(monkeypatch):
    """A surveyor's own drawing has only geometry — the parcels come from asking
    the county's parcel layer what it crosses."""
    sent = []

    class _Resp(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(url, body, timeout):
        sent.append(dict(kmz.urllib.parse.parse_qsl(body.decode())))
        rows = [{"attributes": {"ACCOUNTNO": a, "NAME": "X"}} for a in ("R1611986", "R0331894")]
        return _Resp(kmz.json.dumps({"features": rows}).encode())

    monkeypatch.setattr(kmz.urllib.request, "urlopen", fake_urlopen)
    parcels = kmz.parcels_for_geometry(PIPELINE.read_bytes())

    assert [p["account"] for p in parcels] == ["R0331894", "R1611986"]
    assert len(sent) == 1 and sent[0]["geometryType"] == "esriGeometryPolyline"
    assert len(kmz.json.loads(sent[0]["geometry"])["paths"][0]) == 3
