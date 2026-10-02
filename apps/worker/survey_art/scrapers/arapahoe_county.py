"""Arapahoe County, CO — property records scraper.

Workflow
--------
1. Arapahoe County Assessor
   - Look up property by situs address to obtain the parcel/account number.

2. Arapahoe County Clerk & Recorder
   - Search recorded documents by parcel number.
   - Collect download URLs for all relevant survey documents.

3. Download
   - Save all files via download.py to {tmp_dir}/CO_arapahoe/{address_slug}/.
"""

from __future__ import annotations

import logging
from pathlib import Path

from survey_art.document_filter import prompt_fragment
from survey_art.download import download_dir, download_urls
from survey_art.geocode import GeocodedAddress
from survey_art.llm import run_agent

logger = logging.getLogger(__name__)

_ASSESSOR_URL = "https://www.arapahoegov.com/assessor"
_RECORDER_URL = "https://recording.arapahoegov.com"


async def scrape(
    geocoded: GeocodedAddress,
    tmp_dir: Path,
) -> tuple[list[Path], str | None, float, int, int]:
    """Scrape Arapahoe County property records for the given address."""
    address = geocoded.one_line()
    logger.info("Arapahoe County scraper starting for: %s", address)

    _, parcel, (cost1, in1, out1) = await run_agent(
        f"Go to {_ASSESSOR_URL}. "
        f"Search for the property at '{address}'. "
        "Find and return the parcel number or account number for the property. "
        "Return only the number, nothing else."
    )
    if parcel:
        logger.info("Arapahoe Assessor returned parcel number: %s", parcel)
    search_hint = f"using parcel number '{parcel}'" if parcel else f"using the address '{address}'"
    _, raw, (cost2, in2, out2) = await run_agent(
        f"Go to {_RECORDER_URL}. "
        f"Search for recorded documents for the property at '{address}' {search_hint}. "
        f"{prompt_fragment()}\n"
        "Return all download URLs as a plain newline-separated list, nothing else."
    )
    urls = [u.strip() for u in raw.splitlines() if u.strip().startswith("http")]
    logger.info("Arapahoe Recorder returned %d document URLs", len(urls))
    cost, in_tok, out_tok = cost1 + cost2, in1 + in2, out1 + out2

    if not urls:
        return [], f"No documents found for {address} in Arapahoe County.", cost, in_tok, out_tok
    saved = await download_urls(urls, download_dir(geocoded.county, address, tmp_dir))
    return saved, None, cost, in_tok, out_tok
