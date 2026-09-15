"""Pull a Weld-style account/parcel number out of an uploaded KMZ.

County GIS hubs (Weld's included — see gishub.weldgov.com) export parcel selections as
KMZ: a zipped KML with the parcel polygon plus its attribute table in each
Placemark's <ExtendedData>. We don't need the geometry — the account/parcel number
already routes through the exact same lookup the Account/Parcel # search box does
(see `_resolve_parcel()` in `survey_art/scrapers/weld_county.py`), so all this does is
find that field and hand back a string.
"""

from __future__ import annotations

import re
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
