"""Denver County, CO — property records scraper.

Workflow
--------
1. Spatialest (Denver Assessor proxy)
   - Browser agent navigates to property.spatialest.com/co/denver.
   - Searches for the address and extracts Schedule Number, owner name, and legal
     description. No credentials required — the site is public.

2. Kofile Tech (Denver Clerk & Recorder)
   - Authenticated browser session using CO_DENVER_USERNAME / CO_DENVER_PASSWORD.
   - Uses the owner name from Step 1 to search the Names index.
   - Denver Kofile has NO address search — only party name, reception number, and
     book/page. Owner-name search is the primary path.
   - For business owners the full name goes in the Last Name field. If that returns
     > 50 results the agent falls back to the most distinctive word.
   - Downloads all matching survey/deed/easement documents directly.

Key Notes
---------
- Denver legal descriptions often use Government Survey format (T4S R68W SEC2),
  NOT subdivision/block/lot. No legal-description search is available in Kofile.
- For Improvement Survey Plats (ISPs), the document type in Kofile is "PLAT MAP"
  or "SURVEY" — both are highest priority.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from browser_use import Agent

from land_survey_scraper.county_sites import SUPPORTED_COUNTIES
from land_survey_scraper.document_filter import DEFAULT_FILTER, DocumentFilter
from land_survey_scraper.download import download_dir as make_download_dir
from land_survey_scraper.geocode import GeocodedAddress
from land_survey_scraper.llm import agent_cost, get_llm
from land_survey_scraper.settings import get_settings

logger = logging.getLogger(__name__)

_ENTRY = next(e for e in SUPPORTED_COUNTIES if e["county"] == "Denver")
_KOFILE_LOGIN_URL = _ENTRY["urls"]["recorder"]
_SPATIALEST_URL = _ENTRY["urls"]["assessor"]

# Words that are too generic to use alone as a Kofile Names search term.
# If the owner name consists only of these words the full name is used instead.
_GENERIC_NAME_WORDS = {
    "THE", "A", "AN", "OF", "AND", "OR", "&", "AT", "IN",
    "LLC", "INC", "CORP", "CO", "LTD", "LP", "LLP", "PC", "PLLC",
    "COMPANY", "CORPORATION", "INCORPORATED", "LIMITED",
    "TRUST", "TRUSTEE", "TRUSTEES",
    "PROPERTIES", "PROPERTY", "REALTY", "REAL", "ESTATE",
    "MANAGEMENT", "INVESTMENT", "INVESTMENTS",
    "GROUP", "HOLDINGS", "VENTURES", "PARTNERS", "PARTNERSHIP",
    "CAFE", "COFFEE", "BAR", "RESTAURANT", "GRILL", "KITCHEN",
    "ASSOCIATES", "ASSOCIATION", "SERVICES", "SERVICE",
    "DEVELOPMENT", "DEVELOPER", "BUILDERS", "CONSTRUCTION",
    "NO", "NUMBER", "NUM",
}


def _distinctive_word(owner_name: str) -> str:
    """Return the first word from the owner name that is not a generic term.

    Falls back to the raw first word if every word is generic.
    """
    words = re.sub(r"[^A-Z0-9 ]", "", owner_name.upper()).split()
    for word in words:
        if word not in _GENERIC_NAME_WORDS and len(word) > 2:
            return word
    return words[0] if words else owner_name


async def _get_owner_info(address: str) -> tuple[dict | None, float, int, int]:
    """Phase 1: browser agent navigates Spatialest to retrieve owner and parcel data.

    Returns a dict with keys: schedule, owner, legal.
    All values are strings (may be empty if not found).
    """
    task = (
        f"Go to {_SPATIALEST_URL} and search for the property at '{address}'.\n"
        f"Use the search box (usually in the top navigation bar) to type the address.\n"
        f"Click on the correct matching property in the results list.\n"
        f"On the property detail page, read and return EXACTLY the following fields:\n\n"
        f"  SCHEDULE: [the account number or schedule number shown on the page]\n"
        f"  OWNER: [the current owner name shown on the page]\n"
        f"  LEGAL: [the full legal description shown on the page]\n\n"
        f"Return only those three lines. No explanation, no extra text."
    )
    agent = Agent(task=task, llm=get_llm(), use_thinking=False, calculate_cost=True)
    result = await agent.run()
    cost, in_tok, out_tok = agent_cost(agent)

    text = str(result).strip()
    logger.debug("Spatialest agent raw result: %s", text)

    info: dict = {"schedule": "", "owner": "", "legal": ""}
    for line in text.splitlines():
        if line.upper().startswith("SCHEDULE:"):
            info["schedule"] = line.split(":", 1)[1].strip()
        elif line.upper().startswith("OWNER:"):
            info["owner"] = line.split(":", 1)[1].strip()
        elif line.upper().startswith("LEGAL:"):
            info["legal"] = line.split(":", 1)[1].strip()

    if info["owner"] or info["schedule"]:
        logger.info(
            "Denver Assessor: schedule=%s owner=%s legal=%s",
            info["schedule"], info["owner"], info["legal"],
        )
        return info, cost, in_tok, out_tok

    logger.warning("Denver Assessor: could not parse owner info from result: %s", text)
    return None, cost, in_tok, out_tok


async def _download_documents(
    address: str,
    owner_info: dict | None,
    doc_filter: DocumentFilter,
    dest_dir: Path,
    username: str,
    password: str,
) -> tuple[list[Path], float, int, int]:
    """Phase 2: log into Kofile Tech, search by owner name, download documents.

    Kofile County Fusion for Denver:
    - Names search: Last Name field accepts a full business name OR a surname.
      For individuals enter Last Name + First Name in separate fields.
    - Reception Number search: exact numeric lookup.
    - There is NO address search field — owner name is the primary path.
    - Document download: click a row in results → detail page → Save/Download button.
    """
    # ------------------------------------------------------------------ #
    # Build search context from assessor data                              #
    # ------------------------------------------------------------------ #
    if owner_info:
        owner = owner_info.get("owner", "")
        schedule = owner_info.get("schedule", "")
        legal = owner_info.get("legal", "")
        keyword = _distinctive_word(owner) if owner else ""

        owner_block = (
            f"Property owner (from Denver Assessor): {owner}\n"
            f"Schedule Number: {schedule}\n"
            f"Legal Description: {legal}\n\n"
        )
        search_steps = (
            f"STEP 3 — Search by full owner name:\n"
            f"  Select the 'Names' search type.\n"
            f"  In the Last Name field enter the FULL owner name: '{owner}'\n"
            f"  Leave the First Name field blank.\n"
            f"  Click Search.\n\n"
            f"  IMPORTANT — If the search returns more than 50 results, the owner name is\n"
            f"  too broad. Clear the form and search again using only the distinctive\n"
            f"  keyword: '{keyword}' (avoid generic words like LLC, INC, CAFE, GROUP, etc.).\n\n"
            f"  The ISP (Improvement Survey Plat), any ALTA surveys, deeds, and easements\n"
            f"  for this property will appear in these results because the current owner\n"
            f"  '{owner}' is listed as a party on those documents.\n"
            f"  Look through the results for doc types: SURVEY, PLAT MAP, WARRANTY DEED,\n"
            f"  QUIT CLAIM DEED, DEED OF TRUST, EASEMENT. Download all of them.\n\n"
            f"STEP 4 — Search by distinctive keyword for older documents:\n"
            f"  Clear the form. Select the 'Names' search type.\n"
            f"  In the Last Name field enter '{keyword}'.\n"
            f"  Click Search.\n"
            f"  This finds any documents filed before the current owner acquired the property\n"
            f"  where '{keyword}' still appears as a party. Download any surveys or plats\n"
            f"  you have not already downloaded.\n\n"
        )
        dl_step = "STEP 5"
    else:
        owner = ""
        owner_block = ""
        search_steps = (
            f"STEP 3 — Search by address street name:\n"
            f"  Select the 'Names' search type.\n"
            f"  In the Last Name field enter 'YORK' (the street name from the address).\n"
            f"  Set Document Type filter to 'SURVEY' or 'PLAT MAP' if available.\n"
            f"  Click Search. Look for surveys or plats referencing this property.\n"
            f"  Download any matching documents.\n\n"
        )
        dl_step = "STEP 4"

    task = (
        f"You are researching property records for a professional land surveying firm.\n"
        f"Property address: {address}\n"
        f"{owner_block}"
        f"STEP 1 — Log in to Denver Clerk & Recorder (Kofile Tech):\n"
        f"  Navigate to {_KOFILE_LOGIN_URL}\n"
        f"  Enter username='{username}' in the username field.\n"
        f"  Enter password='{password}' in the password field.\n"
        f"  Click the Login button (NOT Guest Login).\n"
        f"  If a disclaimer or terms-of-service page appears, click 'I Agree' or 'Accept'.\n"
        f"  Wait for the main search interface to load — it has multiple search type tabs\n"
        f"  or links (Names, Reception Number, Book/Page, etc.).\n\n"
        f"STEP 2 — Familiarise yourself with the search interface:\n"
        f"  Note which search types are available (Names, Reception Number, Book/Page, etc.).\n"
        f"  You will perform multiple searches — do NOT stop after the first one.\n\n"
        f"{search_steps}"
        f"{dl_step} — Download ALL matching documents found across all searches:\n"
        f"  {doc_filter.to_prompt_fragment()}\n"
        f"  Priority order (highest first):\n"
        f"    1. Improvement Survey Plat (ISP) — document type PLAT MAP or SURVEY\n"
        f"    2. ALTA/NSPS Land Title Survey — document type SURVEY\n"
        f"    3. Subdivision Plat — document type PLAT MAP\n"
        f"    4. Deeds (warranty deed, quit claim deed, special warranty deed)\n"
        f"    5. Easements, right-of-way dedications, liens\n\n"
        f"  For each matching document:\n"
        f"    - Click the row to open the document detail page.\n"
        f"    - Click the Download, Save Image, or printer icon to download the PDF.\n"
        f"    - If a modal/dialog appears, confirm and click through to save the file.\n"
        f"    - Close the document detail and return to the search results.\n\n"
        f"When all downloads are complete, say 'Done — downloaded N files.'"
    )

    agent = Agent(task=task, llm=get_llm(), use_thinking=False, calculate_cost=True)
    await agent.run()

    _VALID_SUFFIXES = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}
    local_paths = [
        Path(p)
        for p in (agent.available_file_paths or [])
        if Path(p).suffix.lower() in _VALID_SUFFIXES and Path(p).exists()
    ]
    logger.info("Denver County: browser downloaded %d file(s)", len(local_paths))

    saved: list[Path] = []
    if local_paths:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for src in local_paths:
            dst = dest_dir / src.name
            shutil.copy(src, dst)
            saved.append(dst)

    cost, in_tok, out_tok = agent_cost(agent)
    return saved, cost, in_tok, out_tok


async def scrape(
    geocoded: GeocodedAddress,
    tmp_dir: Path,
    doc_filter: DocumentFilter = DEFAULT_FILTER,
) -> tuple[list[Path], str | None, float, int, int]:
    """Scrape Denver County property records for the given address."""
    address = geocoded.one_line()
    logger.info("Denver County scraper starting for: %s", address)

    s = get_settings()
    username = s.co_denver_username
    password = s.co_denver_password
    if not username or not password:
        return [], (
            "CO_DENVER_USERNAME and CO_DENVER_PASSWORD must be set in .env "
            "to access the Denver Clerk & Recorder records system."
        ), 0.0, 0, 0

    # Phase 1: get owner name and parcel info from Denver Assessor
    owner_info, cost1, in1, out1 = await _get_owner_info(address)

    # Phase 2: log into Kofile and download documents
    dest = make_download_dir(geocoded.county, address, base=tmp_dir)
    saved, cost2, in2, out2 = await _download_documents(
        address, owner_info, doc_filter, dest, username, password
    )

    total_cost = cost1 + cost2
    total_in = in1 + in2
    total_out = out1 + out2

    if not saved:
        return [], f"No documents found for {address} in Denver County.", total_cost, total_in, total_out

    return saved, None, total_cost, total_in, total_out
