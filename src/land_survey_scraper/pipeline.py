"""Orchestrate: address -> geocode -> county scraper -> download."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path

from land_survey_scraper.console import county_resolved, download_done, files_table, model_banner
from land_survey_scraper.document_filter import DEFAULT_FILTER, DocumentFilter
from land_survey_scraper.geocode import GeocodedAddress, address_to_county
from land_survey_scraper.settings import get_settings
from land_survey_scraper.scrapers import (
    arapahoe_county,
    denver_county,
    jefferson_county,
    weld_county,
)

logger = logging.getLogger(__name__)

# Maps County.key() -> scrape coroutine function
ScrapeFn = Callable[..., Coroutine]

COUNTY_SCRAPERS: dict[str, ScrapeFn] = {
    "CO_weld": weld_county.scrape,
    "CO_denver": denver_county.scrape,
    "CO_arapahoe": arapahoe_county.scrape,
    "CO_jefferson": jefferson_county.scrape,
}


async def run_async(
    address: str,
    *,
    tmp_dir: Path | None = None,
    doc_filter: DocumentFilter = DEFAULT_FILTER,
    skip_existing: bool = True,
    quiet: bool = False,
    county_override: str | None = None,
) -> tuple[list[Path], str | None]:
    """
    Full async pipeline: geocode address -> dispatch to county scraper -> download.
    Returns (saved_paths, error_message).
    """
    tmp = tmp_dir or Path("tmp")
    s = get_settings()
    if not quiet:
        model_banner(s.model)

    geocoded: GeocodedAddress | None = address_to_county(address)
    if not geocoded:
        return [], "Could not resolve address to a county."

    if not quiet:
        county_resolved(geocoded.county.name, geocoded.county.state)

    county_key = county_override or geocoded.county.key()
    scrape_fn = COUNTY_SCRAPERS.get(county_key)
    if not scrape_fn:
        supported = ", ".join(COUNTY_SCRAPERS)
        return [], (
            f"County '{county_key}' is not yet supported. "
            f"Supported counties: {supported}"
        )

    saved, err = await scrape_fn(geocoded, tmp, doc_filter)
    if err:
        return [], err

    if not quiet and saved:
        download_done(saved, str(tmp))
        files_table(saved, str(tmp))

    return saved, None


def run(
    address: str,
    *,
    tmp_dir: Path | None = None,
    doc_filter: DocumentFilter = DEFAULT_FILTER,
    skip_existing: bool = True,
    quiet: bool = False,
    county_override: str | None = None,
) -> tuple[list[Path], str | None]:
    """Synchronous wrapper around run_async."""
    return asyncio.run(
        run_async(
            address,
            tmp_dir=tmp_dir,
            doc_filter=doc_filter,
            skip_existing=skip_existing,
            quiet=quiet,
            county_override=county_override,
        )
    )
