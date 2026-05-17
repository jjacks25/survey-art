"""Weld County, CO — property records scraper.

Workflow
--------
1. Property Portal (maps.weld.gov/propertyportal/)
   - Search by situs address to find the parcel.
   - Pull document history to collect Reception Numbers for deeds/plats/surveys.

2. eRecording (erecording.weld.gov)
   - Login with shared surveyor credentials.
   - Search by Reception Number and by document type.
   - Collect all relevant document download URLs.

3. Download
   - Save all files via download.py to {tmp_dir}/CO_weld/{address_slug}/.
"""

from __future__ import annotations

import logging
from pathlib import Path

from browser_use import Agent

from land_survey_scraper.settings import get_settings
from land_survey_scraper.county_sites import SUPPORTED_COUNTIES
from land_survey_scraper.document_filter import DEFAULT_FILTER, DocumentFilter
from land_survey_scraper.download import download_all_async
from land_survey_scraper.geocode import GeocodedAddress
from land_survey_scraper.llm import agent_cost, get_llm
from land_survey_scraper.types import DocumentLink

logger = logging.getLogger(__name__)

_ENTRY = next(e for e in SUPPORTED_COUNTIES if e["county"] == "Weld")
_PORTAL_URL = _ENTRY["urls"]["property_portal"]
_ERECORDING_URL = _ENTRY["urls"]["erecording"]




async def _get_reception_numbers(address: str) -> tuple[list[str], float, int, int]:
    """Use browser-use to navigate the Weld Property Portal and extract Reception Numbers."""
    task = (
        f"Go to {_PORTAL_URL}. "
        f"Search for the property with address '{address}'. "
        "Click on the matching parcel to open its property report. "
        "Find the document history section and collect all Reception Numbers listed. "
        "Return them as a plain comma-separated list, nothing else."
    )
    agent = Agent(task=task, llm=get_llm(), use_thinking=False, calculate_cost=True)
    result = await agent.run()
    raw = str(result).strip()
    numbers = [r.strip() for r in raw.replace("\n", ",").split(",") if r.strip()]
    logger.info("Weld Property Portal returned %d reception numbers", len(numbers))
    cost, in_tok, out_tok = agent_cost(agent)
    return numbers, cost, in_tok, out_tok


async def _get_document_urls(
    address: str,
    reception_numbers: list[str],
    doc_filter: DocumentFilter,
) -> tuple[list[str], float, int, int]:
    """Login to eRecording and collect document URLs matching the filter."""
    reception_list = ", ".join(reception_numbers) if reception_numbers else "none"

    task = (
        f"Go to {_ERECORDING_URL}. "
        f"Login with username '{get_settings().weld_erecording_username}' "
        f"and password '{get_settings().weld_erecording_password}'. "
        f"For the property at '{address}', search for documents using these approaches:\n"
        f"1. If reception numbers are available ({reception_list}), search each one directly.\n"
        "2. Also search by grantor/grantee name or address if reception numbers are not found.\n"
        f"{doc_filter.to_prompt_fragment()}\n"
        "Return all download URLs as a plain newline-separated list, nothing else."
    )
    agent = Agent(task=task, llm=get_llm(), use_thinking=False, calculate_cost=True)
    result = await agent.run()
    raw = str(result).strip()
    urls = [u.strip() for u in raw.splitlines() if u.strip().startswith("http")]
    logger.info("eRecording returned %d document URLs", len(urls))
    cost, in_tok, out_tok = agent_cost(agent)
    return urls, cost, in_tok, out_tok


async def scrape(
    geocoded: GeocodedAddress,
    tmp_dir: Path,
    doc_filter: DocumentFilter = DEFAULT_FILTER,
) -> tuple[list[Path], str | None, float, int, int]:
    """Scrape Weld County property records for the given address."""
    s = get_settings()
    if not s.weld_erecording_username or not s.weld_erecording_password:
        return [], (
            "Weld County eRecording credentials are not configured. "
            "Set WELD_ERECORDING_USERNAME and WELD_ERECORDING_PASSWORD in .env."
        ), 0.0, 0, 0
    address = geocoded.one_line()
    logger.info("Weld County scraper starting for: %s", address)

    reception_numbers, cost1, in_tok1, out_tok1 = await _get_reception_numbers(address)
    doc_urls, cost2, in_tok2, out_tok2 = await _get_document_urls(address, reception_numbers, doc_filter)
    total_cost = cost1 + cost2
    total_in = in_tok1 + in_tok2
    total_out = out_tok1 + out_tok2

    if not doc_urls:
        return [], f"No documents found for {address} in Weld County.", total_cost, total_in, total_out

    links = [DocumentLink(url=u, text="", content_type="") for u in doc_urls]
    saved = await download_all_async(links, geocoded.county, address, base=tmp_dir)
    return saved, None, total_cost, total_in, total_out
