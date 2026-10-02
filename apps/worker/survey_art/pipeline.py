"""Orchestrate: address -> geocode -> county scraper -> download."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from pathlib import Path

from survey_art.console import county_resolved, files_table, model_banner, run_cost
from survey_art.geocode import County, GeocodedAddress, address_to_county
from survey_art.scrapers import arapahoe_county, denver_county, jefferson_county, weld_county
from survey_art.settings import get_settings

# Maps County.key() -> scrape coroutine function. Every scraper is called as
# `scrape(geocoded, tmp_dir)` and returns (saved, error, cost_usd, in_tokens, out_tokens).
COUNTY_SCRAPERS: dict[str, Callable[..., Coroutine]] = {
    "CO_weld": weld_county.scrape,
    "CO_denver": denver_county.scrape,
    "CO_arapahoe": arapahoe_county.scrape,
    "CO_jefferson": jefferson_county.scrape,
}


async def run_async(
    address: str,
    *,
    tmp_dir: Path = Path("tmp"),
    quiet: bool = False,
    county_override: str | None = None,
    str_input: str = "",
    owner_input: str = "",
    sop_strict: bool = False,
) -> tuple[list[Path], str | None, float, int, int]:
    """Geocode `address`, dispatch to its county's scraper, and return
    `(saved_paths, error_message, bedrock_cost_usd, input_tokens, output_tokens)`."""
    if not quiet:
        model_banner(get_settings().model)

    geocoded = address_to_county(address)
    if not geocoded:
        if not county_override:
            return [], "Could not resolve address to a county.", 0.0, 0, 0
        # Geocoding fails for non-address input (an account/parcel ID); with a
        # county override, hand the raw input to the scraper as the street.
        state, _, name = county_override.partition("_")
        county = County(state=state.upper(), name=name.replace("_", " ").title() or "Unknown")
        geocoded = GeocodedAddress(
            street=address, city="", state=county.state, zip_code="", county=county
        )

    if not quiet:
        county_resolved(geocoded.county.name, geocoded.county.state)

    county_key = county_override or geocoded.county.key()
    scrape_fn = COUNTY_SCRAPERS.get(county_key)
    if not scrape_fn:
        supported = ", ".join(COUNTY_SCRAPERS)
        return [], f"County '{county_key}' is not yet supported. Supported: {supported}", 0, 0, 0

    # Weld accepts SOP Phase 1 keyword args; the other scrapers don't.
    kwargs = {}
    if county_key == "CO_weld":
        kwargs = {"str_input": str_input, "owner_input": owner_input, "sop_strict": sop_strict}
    saved, err, cost, in_tok, out_tok = await scrape_fn(geocoded, tmp_dir, **kwargs)
    if not quiet:
        run_cost(cost, in_tok, out_tok)
        if saved and not err:
            files_table(saved, tmp_dir)
    return ([] if err else saved), err, cost, in_tok, out_tok
