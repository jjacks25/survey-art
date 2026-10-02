"""Turn an uploaded KMZ into the Weld account numbers it covers.

Two kinds of KMZ come in:

* A county GIS export of a parcel selection: the parcel polygon plus its attribute
  table in each Placemark's <ExtendedData>. The account/parcel number is right there
  (`extract_identifier`), and routes through the same lookup the Account/Parcel #
  search box does (`_resolve_parcel()` in `survey_art/scrapers/weld_county.py`).
* A surveyor's own drawing — a pipeline route, a boundary sketched in Google Earth —
  which is only geometry. `parcels_for_geometry` asks Weld's public parcel layer
  which parcels it touches. The "Greeley West Pipeline" sample is a 3-point line
  that crosses 5 parcels, none of which the file names.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO

# Untrusted input (user-uploaded KMZ) — stdlib xml.etree expands internal entities
# with no size cap, so a malicious KML could billion-laughs the API process.
# defusedxml.ElementTree is a drop-in replacement that rejects that.
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

_KML_NS = "{http://www.opengis.net/kml/2.2}"

# ExtendedData <Data name="..."> / <SimpleData name="..."> keys that county GIS
# exports commonly use for the assessor account/parcel number.
_FIELD_NAMES = re.compile(r"^(account(no)?|parcel(id|_?num)?|apn)$", re.IGNORECASE)

# Weld account numbers (RXXXXXXX) and bare parcel numbers — same patterns
# `_resolve_parcel()` matches on the manual search box.
_ACCOUNT_RE = re.compile(r"^R\d{5,9}$", re.IGNORECASE)
_PARCEL_RE = re.compile(r"^\d{10,14}$")


def _looks_like_identifier(value: str) -> bool:
    value = value.strip()
    return bool(_ACCOUNT_RE.match(value) or _PARCEL_RE.match(value))


def _kml_bytes_from_kmz(data: bytes) -> bytes:
    with zipfile.ZipFile(BytesIO(data)) as zf:
        kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
        if not kml_names:
            raise ValueError("no .kml entry found inside the KMZ")
        name = "doc.kml" if "doc.kml" in kml_names else kml_names[0]
        return zf.read(name)


def extract_identifier(data: bytes) -> str | None:
    """Return the best-guess account/parcel number from an uploaded KMZ.

    Looks at every Placemark's ExtendedData fields first (most reliable — comes
    straight from the county's attribute table), then falls back to the
    Placemark <name> if it happens to already be the account/parcel number.
    Returns None if nothing in the file looks like one.
    """
    try:
        kml = _kml_bytes_from_kmz(data)
        root = ElementTree.fromstring(kml)
    except (zipfile.BadZipFile, ValueError, ElementTree.ParseError, DefusedXmlException):
        return None

    for placemark in root.iter(f"{_KML_NS}Placemark"):
        for data_el in placemark.iter(f"{_KML_NS}Data"):
            field = data_el.get("name", "")
            value_el = data_el.find(f"{_KML_NS}value")
            value = (value_el.text or "").strip() if value_el is not None else ""
            if value and _FIELD_NAMES.match(field) and _looks_like_identifier(value):
                return value.upper()
        for data_el in placemark.iter(f"{_KML_NS}SimpleData"):
            field = data_el.get("name", "")
            value = (data_el.text or "").strip()
            if value and _FIELD_NAMES.match(field) and _looks_like_identifier(value):
                return value.upper()

    for placemark in root.iter(f"{_KML_NS}Placemark"):
        name_el = placemark.find(f"{_KML_NS}name")
        name = (name_el.text or "").strip() if name_el is not None else ""
        if _looks_like_identifier(name):
            return name.upper()

    return None


# Weld's public parcel layer — the same one the account map page
# (maps.weld.gov/mapanaccount) draws from. No key needed.
_PARCELS_QUERY_URL = (
    "https://services.arcgis.com/ewjSqmSyHJnkfBLL/arcgis/rest/services/"
    "Parcels_open_data/FeatureServer/0/query"
)
# Parcels a single upload may expand to. A route across the county can touch
# hundreds; each is its own job, so cap it rather than queue a surprise bill.
MAX_PARCELS = 50


def _geometries(root) -> list[tuple[str, list[list[float]]]]:
    """Every Point / LineString / Polygon outer ring as (kind, [[lon, lat], ...])."""
    found = []
    for kind, tag in (
        ("point", "Point"),
        ("line", "LineString"),
        ("polygon", "outerBoundaryIs"),
    ):
        for el in root.iter(f"{_KML_NS}{tag}"):
            coords_el = el.find(f".//{_KML_NS}coordinates")
            if coords_el is None or not coords_el.text:
                continue
            pts = [[float(v) for v in c.split(",")[:2]] for c in coords_el.text.split() if "," in c]
            if pts:
                found.append((kind, pts))
    return found


def _esri_geometry(kind: str, pts: list[list[float]]) -> tuple[str, dict]:
    sr = {"wkid": 4326}
    if kind == "point" or len(pts) == 1:
        return "esriGeometryPoint", {"x": pts[0][0], "y": pts[0][1], "spatialReference": sr}
    if kind == "polygon":
        return "esriGeometryPolygon", {"rings": [pts], "spatialReference": sr}
    return "esriGeometryPolyline", {"paths": [pts], "spatialReference": sr}


def parcels_for_geometry(data: bytes) -> list[dict]:
    """Every Weld parcel the KMZ's drawn geometry touches, as
    `{account, owner, situs, str_code}` rows sorted by account. Empty when the file has
    no geometry, can't be read, or the county's service is unreachable."""
    try:
        root = ElementTree.fromstring(_kml_bytes_from_kmz(data))
        shapes = _geometries(root)
    except (zipfile.BadZipFile, ValueError, ElementTree.ParseError, DefusedXmlException):
        return []

    by_account: dict[str, dict] = {}
    for kind, pts in shapes:
        geometry_type, geometry = _esri_geometry(kind, pts)
        body = urllib.parse.urlencode(
            {
                "geometry": json.dumps(geometry),
                "geometryType": geometry_type,
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "ACCOUNTNO,NAME,SITUS,STR",
                "returnGeometry": "false",
                "f": "json",
            }
        ).encode()
        try:
            with urllib.request.urlopen(_PARCELS_QUERY_URL, body, timeout=15) as resp:
                features = json.load(resp).get("features", [])
        except (OSError, ValueError):
            continue
        for f in features:
            a = f.get("attributes", {})
            account = (a.get("ACCOUNTNO") or "").strip().upper()
            if _ACCOUNT_RE.match(account):
                by_account.setdefault(
                    account,
                    {
                        "account": account,
                        "owner": (a.get("NAME") or "").strip(),
                        "situs": (a.get("SITUS") or "").strip(),
                        "str_code": (a.get("STR") or "").strip(),
                    },
                )
    return sorted(by_account.values(), key=lambda r: r["account"])[:MAX_PARCELS]
