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

from survey_art.county_sites import SUPPORTED_COUNTIES
from survey_art.document_filter import DEFAULT_FILTER, DocumentFilter
from survey_art.download import download_dir as make_download_dir
from survey_art.geocode import GeocodedAddress
from survey_art.llm import agent_cost, get_llm
from survey_art.settings import get_settings

logger = logging.getLogger(__name__)

_ENTRY = next(e for e in SUPPORTED_COUNTIES if e["county"] == "Denver")
_KOFILE_LOGIN_URL = _ENTRY["urls"]["recorder"]
_SPATIALEST_URL = _ENTRY["urls"]["assessor"]

# Words that are too generic to use alone as a Kofile Names search term.
# If the owner name consists only of these words the full name is used instead.
_GENERIC_NAME_WORDS = {
    "THE",
    "A",
    "AN",
    "OF",
    "AND",
    "OR",
    "&",
    "AT",
    "IN",
    "LLC",
    "INC",
    "CORP",
    "CO",
    "LTD",
    "LP",
    "LLP",
    "PC",
    "PLLC",
    "COMPANY",
    "CORPORATION",
    "INCORPORATED",
    "LIMITED",
    "TRUST",
    "TRUSTEE",
    "TRUSTEES",
    "PROPERTIES",
    "PROPERTY",
    "REALTY",
    "REAL",
    "ESTATE",
    "MANAGEMENT",
    "INVESTMENT",
    "INVESTMENTS",
    "GROUP",
    "HOLDINGS",
    "VENTURES",
    "PARTNERS",
    "PARTNERSHIP",
    "CAFE",
    "COFFEE",
    "BAR",
    "RESTAURANT",
    "GRILL",
    "KITCHEN",
    "ASSOCIATES",
    "ASSOCIATION",
    "SERVICES",
    "SERVICE",
    "DEVELOPMENT",
    "DEVELOPER",
    "BUILDERS",
    "CONSTRUCTION",
    "NO",
    "NUMBER",
    "NUM",
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
        f"  LEGAL: [the full legal description shown on the page]\n"
        f"  PREV_OWNER: [the previous/prior owner name if shown in sale/transfer history, else blank]\n"
        f"  RECEPTIONS: [any reception numbers or document numbers visible on the page,\n"
        f"               comma-separated; else blank]\n\n"
        f"Return only those five lines. No explanation, no extra text."
    )
    agent = Agent(task=task, llm=get_llm(), use_thinking=False, calculate_cost=True)
    result = await agent.run()
    cost, in_tok, out_tok = agent_cost(agent)

    text = str(result).strip()
    logger.debug("Spatialest agent raw result: %s", text)

    info: dict = {"schedule": "", "owner": "", "legal": "", "prev_owner": "", "receptions": ""}
    for line in text.splitlines():
        # Strip leading bullet points, dashes, asterisks, and whitespace so the
        # parser handles "- SCHEDULE: ..." and "  OWNER: ..." in addition to the
        # bare "SCHEDULE: ..." format we asked for.
        stripped = re.sub(r"^[\s\-\*\•]+", "", line).strip()
        upper = stripped.upper()
        if upper.startswith("SCHEDULE:"):
            info["schedule"] = stripped.split(":", 1)[1].strip()
        elif upper.startswith("OWNER:"):
            info["owner"] = stripped.split(":", 1)[1].strip()
        elif upper.startswith("LEGAL:"):
            info["legal"] = stripped.split(":", 1)[1].strip()
        elif upper.startswith("PREV_OWNER:"):
            info["prev_owner"] = stripped.split(":", 1)[1].strip()
        elif upper.startswith("RECEPTIONS:"):
            info["receptions"] = stripped.split(":", 1)[1].strip()

    if info["owner"] or info["schedule"]:
        logger.info(
            "Denver Assessor: schedule=%s owner=%s prev_owner=%s receptions=%s legal=%s",
            info["schedule"],
            info["owner"],
            info["prev_owner"],
            info["receptions"],
            info["legal"],
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
        prev_owner = owner_info.get("prev_owner", "")
        receptions = owner_info.get("receptions", "")
        keyword = _distinctive_word(owner) if owner else ""

        # Build a list of name variants to try in Kofile when the full name fails.
        # Kofile normalises '&' to 'AND' internally, so a search for the literal '&'
        # may return zero results even when the document exists.
        name_variants: list[str] = [owner]
        if "&" in owner:
            name_variants.append(owner.replace("&", "AND"))
            before_amp = owner.split("&")[0].strip()
            after_amp = owner.split("&", 1)[1].strip()
            if before_amp:
                name_variants.append(before_amp)
            if after_amp:
                name_variants.append(after_amp)
        if keyword and keyword not in name_variants:
            name_variants.append(keyword)
        if prev_owner and prev_owner not in name_variants:
            name_variants.append(prev_owner)

        variant_lines = "\n".join(
            f"    {chr(96 + i)}. '{v}'" for i, v in enumerate(name_variants[1:], start=1)
        )

        reception_step = ""
        if receptions:
            reception_step = (
                f"STEP 4b — If names search finds nothing, search by Reception Number:\n"
                f"  Select the 'Reception Number' search type.\n"
                f"  Try each of these numbers: {receptions}\n"
                f"  Download every document returned.\n\n"
            )

        prev_owner_step = ""
        if prev_owner:
            prev_kw = _distinctive_word(prev_owner)
            prev_owner_step = (
                f"STEP 4c — Search by previous owner (documents recorded before current owner):\n"
                f"  Select the 'Names' search type.\n"
                f"  In the Last Name field enter the previous owner: '{prev_owner}'\n"
                f"  Click Search. Look for SURVEY, PLAT MAP, DEED, EASEMENT documents.\n"
                f"  If too many results, narrow to distinctive keyword: '{prev_kw}'\n"
                f"  Download any you have not already downloaded.\n\n"
            )

        owner_block = (
            f"Property owner (from Denver Assessor): {owner}\n"
            f"Schedule Number: {schedule}\n"
            f"Legal Description: {legal}\n"
            + (f"Previous owner: {prev_owner}\n" if prev_owner else "")
            + (f"Known reception numbers: {receptions}\n" if receptions else "")
            + "\n"
        )
        search_steps = (
            f"STEP 3 — Search Kofile by owner name (try variants if needed):\n"
            f"  Select the 'Names' search type.\n"
            f"  In the Last Name field enter the FULL owner name: '{owner}'\n"
            f"  Leave the First Name field blank. Click Search.\n\n"
            f"  If the search returns MORE THAN 50 results: it's too broad. Try '{keyword}'.\n\n"
            f"  If the search returns ZERO results: Kofile may store the name differently.\n"
            f"  Try each of these variants IN ORDER until you get results:\n"
            f"{variant_lines}\n\n"
            f"  Once you have results, look through them for doc types:\n"
            f"  SURVEY, PLAT MAP, WARRANTY DEED, QUIT CLAIM DEED, DEED OF TRUST, EASEMENT.\n"
            f"  Download all that relate to '{address}'.\n\n"
            f"{reception_step}"
            f"{prev_owner_step}"
        )
        dl_step = "STEP 5"
    else:
        owner = ""
        owner_block = ""
        search_steps = (
            f"STEP 3 — Look up the property owner in Denver Assessor (new tab):\n"
            f"  Open a new tab and go to {_SPATIALEST_URL}\n"
            f"  Search for '{address}' using the search box.\n"
            f"  Click the matching property in the results.\n"
            f"  On the detail page, note the current owner name and schedule number.\n"
            f"  Close the tab and return to the Kofile tab.\n\n"
            f"STEP 4 — Search Kofile by the owner name you just found:\n"
            f"  Select the 'Names' search type.\n"
            f"  Enter the full owner name in the Last Name field.\n"
            f"  Click Search. Download all matching SURVEY, PLAT MAP, DEED, and EASEMENT docs.\n"
            f"  IMPORTANT: Kofile searches party names — do NOT search by street address\n"
            f"  or street name; those searches will return unrelated results.\n\n"
        )
        dl_step = "STEP 5"

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
        f"    a. Click the row in the search results to open the document detail page.\n"
        f"    b. The document opens in an image viewer with a toolbar at the top.\n"
        f"       In that toolbar look for ONE of these download triggers (try in order):\n"
        f"         1. A button or link labelled 'Save Image' or 'Get Image'\n"
        f"         2. A floppy-disk icon or down-arrow download icon\n"
        f"         3. A button labelled 'Download' or 'Export'\n"
        f"       Click whichever you find. A file save dialog or automatic download\n"
        f"       should begin — confirm/save if prompted.\n"
        f"    c. CRITICAL — Do NOT use the browser's Print function or Ctrl+P.\n"
        f"       Do NOT use File > Save Page As. Do NOT right-click > Save as PDF.\n"
        f"       Only use the in-page Save Image / Download button in the viewer toolbar.\n"
        f"    d. After the file saves, use the browser Back button or breadcrumb to\n"
        f"       return to the search results and repeat for the next document.\n\n"
        f"When all downloads are complete, say 'Done — downloaded N files.'"
    )

    agent = Agent(task=task, llm=get_llm(), use_thinking=False, calculate_cost=True)
    await agent.run()

    _VALID_SUFFIXES = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}
    # Exclude URL-derived PDFs that browser-use generates when it intercepts
    # print events on Kofile pages (disclaimer, search results, etc.)
    _KOFILE_URL_FRAGMENTS = (
        "countyfusion",
        "kofiletech",
        "disclaimer",
        "searchentry",
        "logindisplay",
        "countyweb",
    )
    local_paths = [
        Path(p)
        for p in (agent.available_file_paths or [])
        if Path(p).suffix.lower() in _VALID_SUFFIXES
        and Path(p).exists()
        and not any(frag in Path(p).stem.lower() for frag in _KOFILE_URL_FRAGMENTS)
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
        return (
            [],
            (
                "CO_DENVER_USERNAME and CO_DENVER_PASSWORD must be set in .env "
                "to access the Denver Clerk & Recorder records system."
            ),
            0.0,
            0,
            0,
        )

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
        return (
            [],
            f"No documents found for {address} in Denver County.",
            total_cost,
            total_in,
            total_out,
        )

    return saved, None, total_cost, total_in, total_out
