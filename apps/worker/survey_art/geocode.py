"""Resolve a free-form address to normalized components and county (state + name)."""

import re
from dataclasses import dataclass

import httpx

CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/address"
CENSUS_COORDINATES = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
ZIPPOPOTAM = "https://api.zippopotam.us/us"
USER_AGENT = "LandSurveyScraper/1.0 (property records research)"


@dataclass
class County:
    """County identification: state abbreviation and county name."""

    state: str
    name: str

    def key(self) -> str:
        """Stable key for paths and config: state_countyname (lowercase, no spaces).

        Strips trailing ' county' that the Census geocoder appends to names
        (e.g. 'Jefferson County' → 'CO_jefferson').
        """
        safe = self.name.lower().removesuffix(" county").replace(" ", "_").replace(",", "")
        return f"{self.state.upper()}_{safe}"


@dataclass
class GeocodedAddress:
    """Result of geocoding: normalized address components and county."""

    street: str
    city: str
    state: str
    zip_code: str
    county: County
    lat: float | None = None
    lon: float | None = None

    def one_line(self) -> str:
        """Single-line address for display."""
        return f"{self.street}, {self.city}, {self.state} {self.zip_code}"


_STATE_NAMES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


def _normalize_state(state: str) -> str:
    """Return a 2-letter state abbreviation, accepting full names or abbreviations."""
    s = state.strip()
    if len(s) == 2:
        return s.upper()
    return _STATE_NAMES.get(s.lower(), s)


def _parse_address_parts(addr: str) -> dict[str, str]:
    """Split an address string into street, city, state, and zip.

    Handles both the combined form ('123 Main St, City, ST 12345') and the
    separated form ('123 Main St, City, Colorado, 80401') where state and zip
    are in separate comma-delimited fields and the state may be a full name.
    """
    addr = addr.strip()
    parts = [p.strip() for p in addr.split(",")]
    street = parts[0] if len(parts) > 0 else ""
    city = parts[1] if len(parts) > 1 else ""

    # 4-part form: "Street, City, State, Zip"
    if len(parts) >= 4 and re.match(r"^\d{5}(-\d{4})?$", parts[-1].strip()):
        state = _normalize_state(parts[2])
        zip_code = parts[-1].strip()
    else:
        # 3-part form: "Street, City, ST 12345"
        state_zip = (parts[2] if len(parts) > 2 else "").split()
        state = _normalize_state(state_zip[0]) if state_zip else ""
        zip_code = state_zip[1] if len(state_zip) >= 2 else ""

    return {"street": street, "city": city, "state": state, "zip": zip_code}


def _census_county(geographies: dict) -> str | None:
    """County name from a Census geocoder `geographies` block, if it has one."""
    geos = geographies.get("Counties") or geographies.get("2020 Census Counties") or []
    return (geos[0].get("NAME") or geos[0].get("BASENAME")) if geos else None


def zip_to_county(zip_code: str) -> County | None:
    """Resolve a ZIP code to its county: Zippopotam gives the ZIP's centroid,
    then the Census coordinates API names the county there. No API keys needed.
    Returns None if either lookup fails."""
    headers = {"User-Agent": USER_AGENT}
    try:
        with httpx.Client(timeout=10.0, headers=headers) as client:
            r = client.get(f"{ZIPPOPOTAM}/{zip_code[:5]}")
            r.raise_for_status()
            place = r.json()["places"][0]
            r = client.get(
                CENSUS_COORDINATES,
                params={
                    "x": float(place["longitude"]),
                    "y": float(place["latitude"]),
                    "benchmark": "Public_AR_Current",
                    "vintage": "Current_Current",
                    "format": "json",
                },
            )
            r.raise_for_status()
            geographies = r.json()["result"]["geographies"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None
    name = _census_county(geographies)
    state = place.get("state abbreviation", "")
    return County(state=state, name=name) if name and state else None


def address_to_county(address: str) -> GeocodedAddress | None:
    """Resolve an address string to its county using the Census Bureau geocoder,
    falling back to ZIP-to-county when the address itself doesn't match.
    Returns None if the address cannot be resolved to a county.
    """
    parts = _parse_address_parts(address)
    state = parts["state"]
    if not state:
        return None

    if parts["street"]:
        params = {
            "street": parts["street"],
            "city": parts["city"],
            "state": state,
            "zip": parts["zip"],
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "layers": "14",
            "format": "json",
        }
        with httpx.Client(timeout=15.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(CENSUS_GEOCODER, params=params)
            resp.raise_for_status()
            matches = resp.json().get("result", {}).get("addressMatches") or []
        for match in matches[:1]:
            name = _census_county(match.get("geographies", {}))
            if name:
                coords = match.get("coordinates") or {}
                return GeocodedAddress(
                    street=match.get("matchedAddress", parts["street"]),
                    city=parts["city"],
                    state=state,
                    zip_code=parts["zip"],
                    county=County(state=state, name=name),
                    lat=coords.get("y"),
                    lon=coords.get("x"),
                )

    if len(parts["zip"]) >= 5 and (county := zip_to_county(parts["zip"])):
        return GeocodedAddress(
            street=parts["street"],
            city=parts["city"],
            state=county.state,
            zip_code=parts["zip"],
            county=county,
        )
    return None
