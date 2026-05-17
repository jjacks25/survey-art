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

from browser_use import Agent

from land_survey_scraper.county_sites import SUPPORTED_COUNTIES
from land_survey_scraper.document_filter import DEFAULT_FILTER, DocumentFilter
from land_survey_scraper.download import download_all_async
from land_survey_scraper.geocode import GeocodedAddress
from land_survey_scraper.llm import agent_cost, get_llm
from land_survey_scraper.types import DocumentLink

logger = logging.getLogger(__name__)

_ENTRY = next(e for e in SUPPORTED_COUNTIES if e["county"] == "Arapahoe")
_ASSESSOR_URL = _ENTRY["urls"]["assessor"]
_RECORDER_URL = _ENTRY["urls"]["recorder"]




async def _get_parcel_number(address: str) -> tuple[str | None, float, int, int]:
    """Navigate Arapahoe Assessor to find the parcel number for the address."""
    task = (
        f"Go to {_ASSESSOR_URL}. "
        f"Search for the property at '{address}'. "
        "Find and return the parcel number or account number for the property. "
        "Return only the number, nothing else."
    )
    agent = Agent(task=task, llm=get_llm(), use_thinking=False, calculate_cost=True)
    result = await agent.run()
    parcel = str(result).strip()
    if parcel:
        logger.info("Arapahoe Assessor returned parcel number: %s", parcel)
    cost, in_tok, out_tok = agent_cost(agent)
    return (parcel or None), cost, in_tok, out_tok


async def _get_document_urls(
    address: str,
    parcel_number: str | None,
    doc_filter: DocumentFilter,
) -> tuple[list[str], float, int, int]:
    """Search Arapahoe Clerk & Recorder for recorded documents."""
    search_hint = (
        f"using parcel number '{parcel_number}'"
        if parcel_number
        else f"using the address '{address}'"
    )
    task = (
        f"Go to {_RECORDER_URL}. "
        f"Search for recorded documents for the property at '{address}' {search_hint}. "
        f"{doc_filter.to_prompt_fragment()}\n"
        "Return all download URLs as a plain newline-separated list, nothing else."
    )
    agent = Agent(task=task, llm=get_llm(), use_thinking=False, calculate_cost=True)
    result = await agent.run()
    raw = str(result).strip()
    urls = [u.strip() for u in raw.splitlines() if u.strip().startswith("http")]
    logger.info("Arapahoe Recorder returned %d document URLs", len(urls))
    cost, in_tok, out_tok = agent_cost(agent)
    return urls, cost, in_tok, out_tok


async def scrape(
    geocoded: GeocodedAddress,
    tmp_dir: Path,
    doc_filter: DocumentFilter = DEFAULT_FILTER,
) -> tuple[list[Path], str | None, float, int, int]:
    """Scrape Arapahoe County property records for the given address."""
    address = geocoded.one_line()
    logger.info("Arapahoe County scraper starting for: %s", address)

    parcel_number, cost1, in_tok1, out_tok1 = await _get_parcel_number(address)
    doc_urls, cost2, in_tok2, out_tok2 = await _get_document_urls(address, parcel_number, doc_filter)
    total_cost = cost1 + cost2
    total_in = in_tok1 + in_tok2
    total_out = out_tok1 + out_tok2

    if not doc_urls:
        return [], f"No documents found for {address} in Arapahoe County.", total_cost, total_in, total_out

    links = [DocumentLink(url=u, text="", content_type="") for u in doc_urls]
    saved = await download_all_async(links, geocoded.county, address, base=tmp_dir)
    return saved, None, total_cost, total_in, total_out
