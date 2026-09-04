"""Resolve a free-form address to normalized components and county (state + name)."""

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

    def one_line(self) -> str:
        """Single-line address for display."""
        return f"{self.street}, {self.city}, {self.state} {self.zip_code}"


def _fips_to_state(fips: str) -> str | None:
    """Map Census 2-digit state FIPS code to 2-letter abbreviation."""
    fips_map = {
        "01": "AL",
        "02": "AK",
        "04": "AZ",
        "05": "AR",
        "06": "CA",
        "08": "CO",
        "09": "CT",
        "10": "DE",
        "11": "DC",
        "12": "FL",
        "13": "GA",
        "15": "HI",
        "16": "ID",
        "17": "IL",
        "18": "IN",
        "19": "IA",
        "20": "KS",
        "21": "KY",
        "22": "LA",
        "23": "ME",
        "24": "MD",
        "25": "MA",
        "26": "MI",
        "27": "MN",
        "28": "MS",
        "29": "MO",
        "30": "MT",
        "31": "NE",
        "32": "NV",
        "33": "NH",
        "34": "NJ",
        "35": "NM",
        "36": "NY",
        "37": "NC",
        "38": "ND",
        "39": "OH",
        "40": "OK",
        "41": "OR",
        "42": "PA",
        "44": "RI",
        "45": "SC",
        "46": "SD",
        "47": "TN",
        "48": "TX",
        "49": "UT",
        "50": "VT",
        "51": "VA",
        "53": "WA",
        "54": "WV",
        "55": "WI",
        "56": "WY",
    }
    return fips_map.get(fips.zfill(2))


def _parse_address_parts(addr: str) -> dict[str, str]:
    """Split '123 Main St, City, ST 12345' into street, city, state, zip."""
    addr = addr.strip()
    parts = [p.strip() for p in addr.split(",")]
    street = parts[0] if len(parts) > 0 else ""
    city = parts[1] if len(parts) > 1 else ""
    state_zip = parts[2] if len(parts) > 2 else ""
    state_zip = state_zip.strip().split()
    state = state_zip[0] if len(state_zip) >= 1 else ""
    zip_code = state_zip[1] if len(state_zip) >= 2 else ""
    return {"street": street, "city": city, "state": state, "zip": zip_code}


def zip_to_county(zip_code: str, state: str | None = None) -> County | None:
    """
    Resolve a ZIP code (and optional state) to county using Zippopotam + Census.
    Zippopotam returns lat/lon for the ZIP; Census coordinates API returns county.
    No API keys required. Returns None if lookup fails.
    """
    zip_code = (zip_code or "").strip()
    if not zip_code or len(zip_code) < 5:
        return None
    zip_code = zip_code[:5]
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=10.0) as client:
        try:
            r = client.get(f"{ZIPPOPOTAM}/{zip_code}", headers=headers)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
    places = data.get("places") or []
    if not places:
        return None
    place = places[0]
    try:
        lon = float(place.get("longitude", 0))
        lat = float(place.get("latitude", 0))
    except (TypeError, ValueError):
        return None
    state_abbr = (place.get("state abbreviation") or state or "").strip()
    if len(state_abbr) != 2 and state:
        state_abbr = state.strip()[:2].upper()

    params = {
        "x": lon,
        "y": lat,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }
    with httpx.Client(timeout=10.0) as client:
        try:
            r = client.get(CENSUS_COORDINATES, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
    geos = (
        data.get("result", {}).get("geographies", {}).get("Counties")
        or data.get("result", {}).get("geographies", {}).get("2020 Census Counties")
        or []
    )
    if not geos:
        return None
    geo = geos[0]
    county_name = geo.get("NAME") or geo.get("BASENAME") or "Unknown"
    state_fips = str(geo.get("STATE", "")).strip()
    if state_fips and len(state_fips) <= 2:
        state_abbr = _fips_to_state(state_fips) or state_abbr
    if not state_abbr:
        return None
    return County(state=state_abbr, name=county_name)


def address_to_county(address: str) -> GeocodedAddress | None:
    """
    Resolve an address string to county using the Census Bureau Geocoder.
    If full-address geocoding fails, falls back to ZIP-to-county (Zippopotam + Census).
    Returns None if the address cannot be resolved to a county.
    """
    parts = _parse_address_parts(address)
    if not parts["state"]:
        return None

    # 1) Try full address geocoding first
    if parts["street"]:
        params = {
            "street": parts["street"],
            "city": parts.get("city") or "",
            "state": parts["state"],
            "zip": parts.get("zip") or "",
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "layers": "14",
            "format": "json",
        }
        headers = {"User-Agent": USER_AGENT}
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(CENSUS_GEOCODER, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        matches = data.get("result", {}).get("addressMatches") or []
        if matches:
            match = matches[0]
            geographies = match.get("geographies", {})
            geos = (
                geographies.get("Counties")
                or geographies.get("2020 Census Counties")
                or []
            )
            if not geos:
                geos = match.get("geographies", {}).get("States") or []
                if not geos:
                    pass
                else:
                    state_abbr = geos[0].get("STATE", "")
                    return GeocodedAddress(
                        street=match.get("matchedAddress", parts["street"]),
                        city=parts["city"],
                        state=state_abbr,
                        zip_code=parts.get("zip", ""),
                        county=County(state=state_abbr, name="Unknown"),
                    )
            else:
                geo = geos[0]
                county_name = geo.get("NAME") or geo.get("BASENAME") or "Unknown"
                state_fips = str(geo.get("STATE", "")).strip()
                state_abbr = (
                    parts["state"]
                    if len(parts["state"]) == 2
                    else (_fips_to_state(state_fips) or parts["state"])
                )
                return GeocodedAddress(
                    street=match.get("matchedAddress", parts["street"]),
                    city=parts["city"],
                    state=state_abbr,
                    zip_code=parts.get("zip", ""),
                    county=County(state=state_abbr, name=county_name),
                )

    # 2) Fallback: ZIP (and state) to county
    zip_code = parts.get("zip") or ""
    if zip_code and parts["state"]:
        county = zip_to_county(zip_code, parts["state"])
        if county:
            return GeocodedAddress(
                street=parts["street"],
                city=parts["city"],
                state=county.state,
                zip_code=zip_code,
                county=county,
            )
    return None
