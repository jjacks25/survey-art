"""Denver County, CO — property records scraper.

Workflow
--------
1. Denver Assessor
   - Look up property by situs address to obtain the Schedule Number.

2. Denver Clerk & Recorder (find-records)
   - Search recorded documents by Schedule Number.
   - Collect download URLs for all relevant survey documents.

3. Download
   - Save all files via download.py to {tmp_dir}/CO_denver/{address_slug}/.
"""

from __future__ import annotations

import logging
from pathlib import Path

from browser_use import Agent

from land_survey_scraper.county_sites import SUPPORTED_COUNTIES
from land_survey_scraper.document_filter import DEFAULT_FILTER, DocumentFilter
from land_survey_scraper.download import download_all_async
from land_survey_scraper.geocode import GeocodedAddress
from land_survey_scraper.llm import get_llm
from land_survey_scraper.types import DocumentLink

logger = logging.getLogger(__name__)

_ENTRY = next(e for e in SUPPORTED_COUNTIES if e["county"] == "Denver")
_ASSESSOR_URL = _ENTRY["urls"]["assessor"]
_RECORDER_URL = _ENTRY["urls"]["recorder"]




async def _get_schedule_number(address: str) -> str | None:
    """Navigate Denver Assessor to find the Schedule Number for the address."""
    task = (
        f"Go to {_ASSESSOR_URL}. "
        f"Search for the property at '{address}'. "
        "Find and return the property's Schedule Number (also called Account Number). "
        "Return only the number, nothing else."
    )
    agent = Agent(task=task, llm=get_llm())
    result = await agent.run()
    schedule = str(result).strip()
    if schedule:
        logger.info("Denver Assessor returned schedule number: %s", schedule)
    return schedule or None


async def _get_document_urls(
    address: str,
    schedule_number: str | None,
    doc_filter: DocumentFilter,
) -> list[str]:
    """Search Denver Clerk & Recorder for recorded documents."""
    search_hint = (
        f"using Schedule Number '{schedule_number}'"
        if schedule_number
        else f"using the address '{address}'"
    )
    task = (
        f"Go to {_RECORDER_URL}. "
        f"Search for recorded documents for the property at '{address}' {search_hint}. "
        f"{doc_filter.to_prompt_fragment()}\n"
        "Return all download URLs as a plain newline-separated list, nothing else."
    )
    agent = Agent(task=task, llm=get_llm())
    result = await agent.run()
    raw = str(result).strip()
    urls = [u.strip() for u in raw.splitlines() if u.strip().startswith("http")]
    logger.info("Denver Recorder returned %d document URLs", len(urls))
    return urls


async def scrape(
    geocoded: GeocodedAddress,
    tmp_dir: Path,
    doc_filter: DocumentFilter = DEFAULT_FILTER,
) -> tuple[list[Path], str | None]:
    """Scrape Denver County property records for the given address."""
    address = geocoded.one_line()
    logger.info("Denver County scraper starting for: %s", address)

    schedule_number = await _get_schedule_number(address)
    doc_urls = await _get_document_urls(address, schedule_number, doc_filter)

    if not doc_urls:
        return [], f"No documents found for {address} in Denver County."

    links = [DocumentLink(url=u, text="", content_type="") for u in doc_urls]
    saved = await download_all_async(links, geocoded.county, address, base=tmp_dir)
    return saved, None
