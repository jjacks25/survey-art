"""Registry of supported counties and their property-records entry points."""

from __future__ import annotations

from land_survey_scraper.geocode import County

# Supported counties and their primary entry-point URLs.
# scraper_key must match an entry in pipeline.COUNTY_SCRAPERS.
SUPPORTED_COUNTIES: list[dict] = [
    {
        "state": "CO",
        "county": "Weld",
        "scraper_key": "CO:weld",
        "urls": {
            "property_portal": "https://apps.weld.gov/propertyportal/",
            "recorder": "https://recording.weld.gov/web/user/disclaimer",
        },
    },
    {
        "state": "CO",
        "county": "Denver",
        "scraper_key": "CO:denver",
        "urls": {
            "assessor": "https://property.spatialest.com/co/denver",
            "recorder": "https://countyfusion3.kofiletech.us/countyweb/loginDisplay.action?countyname=Denver",
        },
    },
    {
        "state": "CO",
        "county": "Arapahoe",
        "scraper_key": "CO:arapahoe",
        "urls": {
            "assessor": "https://www.arapahoegov.com/assessor",
            "recorder": "https://recording.arapahoegov.com",
        },
    },
    {
        "state": "CO",
        "county": "Jefferson",
        "scraper_key": "CO:jefferson",
        "urls": {
            "records_search": "https://www.jeffco.us/1027/Records-Search",
            "assessor": "https://www.jeffco.us/assessor",
        },
    },
]

# In-memory registry built from SUPPORTED_COUNTIES at import time.
_registry: dict[str, dict] = {}


def _county_key(county: County) -> str:
    safe = county.name.lower().replace(" ", "_").replace(",", "").replace("'", "")
    return f"{county.state.upper()}|{safe}"


def _build_registry() -> None:
    for entry in SUPPORTED_COUNTIES:
        key = f"{entry['state'].upper()}|{entry['county'].lower()}"
        _registry[key] = entry


_build_registry()


def get_site(county: County) -> dict | None:
    """Return the registry entry for this county, or None if unsupported."""
    return _registry.get(_county_key(county))


def list_supported() -> list[str]:
    """Return human-readable list of supported counties."""
    return [f"{e['county']}, {e['state']}" for e in SUPPORTED_COUNTIES]
