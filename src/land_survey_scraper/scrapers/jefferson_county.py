"""Jefferson County, CO — property records scraper.

Workflow
--------
1. Jefferson County Records Search (jeffco.us/1027/Records-Search)
   - Search by address to find the parcel and pull document links directly.

2. Jefferson County Assessor (optional enrichment)
   - Look up parcel number if needed for more targeted record search.

3. Download
   - Save all files via download.py to {tmp_dir}/CO_jefferson/{address_slug}/.
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

_ENTRY = next(e for e in SUPPORTED_COUNTIES if e["county"] == "Jefferson")
_RECORDS_URL = _ENTRY["urls"]["records_search"]
_ASSESSOR_URL = _ENTRY["urls"]["assessor"]




async def _get_document_urls(address: str, doc_filter: DocumentFilter) -> list[str]:
    """Navigate Jefferson County Records Search to collect document download URLs."""
    task = (
        f"Go to {_RECORDS_URL}. "
        f"Search for recorded documents for the property at '{address}'. "
        f"If a parcel lookup is needed, also check {_ASSESSOR_URL} for the parcel number. "
        f"{doc_filter.to_prompt_fragment()}\n"
        "Return all download URLs as a plain newline-separated list, nothing else."
    )
    agent = Agent(task=task, llm=get_llm())
    result = await agent.run()
    raw = str(result).strip()
    urls = [u.strip() for u in raw.splitlines() if u.strip().startswith("http")]
    logger.info("Jefferson County Records returned %d document URLs", len(urls))
    return urls


async def scrape(
    geocoded: GeocodedAddress,
    tmp_dir: Path,
    doc_filter: DocumentFilter = DEFAULT_FILTER,
) -> tuple[list[Path], str | None]:
    """Scrape Jefferson County property records for the given address."""
    address = geocoded.one_line()
    logger.info("Jefferson County scraper starting for: %s", address)

    doc_urls = await _get_document_urls(address, doc_filter)
    if not doc_urls:
        return [], f"No documents found for {address} in Jefferson County."

    links = [DocumentLink(url=u, text="", content_type="") for u in doc_urls]
    saved = await download_all_async(links, geocoded.county, address, base=tmp_dir)
    return saved, None
