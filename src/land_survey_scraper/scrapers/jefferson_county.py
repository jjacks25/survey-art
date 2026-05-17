"""Jefferson County, CO — property records scraper.

Workflow
--------
1. Jefferson County Assessor API (propertysearch.jeffco.us/api)
   - Direct REST calls — no browser needed.
   - /api/address  → uniquePropertyId + PIN
   - /api/property → subdivision name
   - /api/legalDescription → block + lot
   - /api/transfer → deed instrument numbers already recorded

2. Jefferson County Land Records (landrecords.co.jefferson.co.us)
   - Browser agent searches by Subdivision + Block + Lot (exact legal description fields).
   - Falls back to instrument number search if subdivision search returns no results.
   - Site uses session-based ASP.NET form submissions — no stable download URLs exist.
   - Agent downloads files directly; browser-use saves them to a temp dir.

3. Download
   - Files are copied from browser-use temp dir to {tmp_dir}/CO_jefferson/{address_slug}/.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import httpx
from browser_use import Agent

from land_survey_scraper.county_sites import SUPPORTED_COUNTIES
from land_survey_scraper.document_filter import DEFAULT_FILTER, DocumentFilter
from land_survey_scraper.download import download_dir as make_download_dir
from land_survey_scraper.geocode import GeocodedAddress
from land_survey_scraper.llm import agent_cost, get_llm

logger = logging.getLogger(__name__)

_ENTRY = next(e for e in SUPPORTED_COUNTIES if e["county"] == "Jefferson")
_RECORDS_URL = "https://landrecords.co.jefferson.co.us/RealEstate/SearchEntry.aspx"
_ASSESSOR_API = "https://propertysearch.jeffco.us/api"

# Required by the assessor API for all list endpoints — without these it returns 500.
_LIST_PARAMS = {"Skip": 0, "Take": 100, "page": 1, "sortBy": "houseNumber", "sortDirection": "asc"}
_PAGE_SIZE = 100


async def _get_parcel_info(house_number: str, street_name: str) -> dict | None:
    """
    Call the Jefferson County Assessor REST API to retrieve parcel details.
    Returns subdivision, block, lot, and any previously recorded instrument numbers.
    No browser required — all endpoints are unauthenticated JSON.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Address search → uniquePropertyId
        # The streetName search returns ALL streets with that name county-wide (can be 1000+).
        # We page through in chunks until we find a propertyAddress starting with house_number.
        prop = None
        skip = 0
        total_count: int | None = None
        try:
            while True:
                r = await client.get(
                    f"{_ASSESSOR_API}/address",
                    params={
                        **_LIST_PARAMS,
                        "Skip": skip,
                        "houseNumber": house_number,
                        "streetName": street_name,
                    },
                )
                r.raise_for_status()
                results = r.json()
                items = results.get("items") if isinstance(results, dict) else results
                if not items:
                    break
                if total_count is None:
                    total_count = results.get("totalCount", 0) if isinstance(results, dict) else 0
                prop = next(
                    (p for p in items if str(p.get("propertyAddress", "")).startswith(house_number)),
                    None,
                )
                if prop:
                    break
                skip += _PAGE_SIZE
                if total_count is not None and skip >= total_count:
                    break
        except Exception as exc:
            logger.warning("Assessor address lookup failed: %s", exc)
            return None

        if prop is None:
            logger.warning(
                "Assessor: no property starting with house number '%s' found — skipping parcel lookup",
                house_number,
            )
            return None
        uid = prop.get("uniquePropertyId")
        if not uid:
            return None

        # 2. Legal description → block + lot
        legal: dict = {}
        try:
            r2 = await client.get(f"{_ASSESSOR_API}/legalDescription/{uid}")
            r2.raise_for_status()
            body = r2.json() or {}
            legal_list = body.get("legalDescriptionDetailsList") or []
            legal = legal_list[0] if legal_list else {}
        except Exception as exc:
            logger.warning("Legal description lookup failed: %s", exc)

        # 3. Transfer history → previously recorded instrument numbers
        instruments: list[str] = []
        try:
            r3 = await client.get(
                f"{_ASSESSOR_API}/transfer/{uid}",
                params={**_LIST_PARAMS, "sortBy": "docNumber"},
            )
            r3.raise_for_status()
            body3 = r3.json() or {}
            transfers = body3.get("transferDetailsList") or []
            instruments = [str(t["docNumber"]) for t in transfers if t.get("docNumber")]
        except Exception as exc:
            logger.warning("Transfer history lookup failed: %s", exc)

        # Subdivision name comes from the address search result
        subdivision = prop.get("subdivision", "")

    # Strip leading numeric code from subdivision (e.g. "693499 SOLTERRA SUB FLG NO 17" → "SOLTERRA SUB FLG NO 17")
    parts = subdivision.split(" ", 1)
    if parts and parts[0].isdigit():
        subdivision = parts[1] if len(parts) > 1 else subdivision

    return {
        "subdivision": subdivision,
        "block": str(legal.get("block", "") or "").strip().lstrip("0"),
        "lot": str(legal.get("lot", "") or "").strip().lstrip("0"),
        "instruments": instruments,
    }


async def _download_documents(
    address: str,
    parcel: dict | None,
    doc_filter: DocumentFilter,
    dest_dir: Path,
) -> tuple[list[Path], float, int, int]:
    """Browser agent: search Jefferson County Land Records and download documents.

    The site uses session-based ASP.NET form submissions — no stable download URLs exist.
    The agent downloads files directly; browser-use saves them to a temp dir which we
    then copy to dest_dir.
    """
    if parcel:
        subdivision = parcel["subdivision"]
        block = parcel["block"]
        lot = parcel["lot"]
        instruments = parcel["instruments"]
        instrument_str = ", ".join(instruments[:5]) if instruments else "none"
        search_instructions = (
            f"The property's legal description is: Subdivision='{subdivision}', "
            f"Block='{block}', Lot='{lot}'. "
            f"Known deed instrument numbers from the assessor: {instrument_str}.\n\n"
            f"You must perform ALL of the following searches — do not stop early.\n\n"
            f"STEP 1 — Search by legal description (finds ISPs, ILCs, easements, deeds for this lot):\n"
            f"  Navigate to {_RECORDS_URL}\n"
            f"  Fill in Subdivision='{subdivision}', Block='{block}', Lot='{lot}' and click Search.\n"
            f"  If the subdivision field is a dropdown or autocomplete, type the first few words "
            f"  and select the closest match.\n\n"
            f"STEP 2 — Search by subdivision name only (finds the recorded subdivision plat):\n"
            f"  Clear the form. Fill in only Subdivision='{subdivision}' (leave Block and Lot blank) "
            f"  and click Search. The subdivision plat document is filed for the whole subdivision "
            f"  and will NOT appear in a block/lot search.\n\n"
            f"STEP 3 — Search by each known instrument number (finds the recorded deeds):\n"
            f"  Clear the form. Enter each of these instrument numbers individually in the "
            f"  Instrument # field and search: {instrument_str}.\n\n"
        )
    else:
        search_instructions = (
            f"STEP 1 — Search by address:\n"
            f"  Navigate to {_RECORDS_URL}\n"
            f"  Enter the street address in the Address field and click Search.\n\n"
        )

    task = (
        f"You are researching property records for a professional land surveying firm. "
        f"Property address: {address}\n\n"
        f"{search_instructions}"
        f"STEP 4 — Download ALL matching documents found across all searches above:\n"
        f"  {doc_filter.to_prompt_fragment()}\n"
        "  IMPORTANT: Survey plats (Land Survey Plat, Subdivision Plat, Improvement Survey Plat) "
        "  are the highest priority — download these even if you also found deeds.\n"
        "  For each matching document, click the row to open the document viewer. "
        "  Click 'Get Image Now', then click the 'here' link or the Download button "
        "  in the PDF viewer to download the file. Handle modals and new tabs as needed.\n\n"
        "After all downloads are complete, say 'Done' and report how many files were downloaded."
    )
    agent = Agent(task=task, llm=get_llm(), use_thinking=False, calculate_cost=True)
    await agent.run()

    # browser-use tracks all downloaded files in agent.available_file_paths
    _VALID_SUFFIXES = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}
    local_paths = [
        Path(p)
        for p in (agent.available_file_paths or [])
        if Path(p).suffix.lower() in _VALID_SUFFIXES and Path(p).exists()
    ]

    logger.info("Jefferson County: browser downloaded %d file(s)", len(local_paths))

    # Copy from browser-use temp dir to our destination
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
    """Scrape Jefferson County property records for the given address."""
    address = geocoded.one_line()
    logger.info("Jefferson County scraper starting for: %s", address)

    # Parse house number and street name from the geocoded address
    street = geocoded.street  # e.g. "2180 S Poppy Street"
    parts = street.split()
    house_number = parts[0] if parts else ""
    # Strip directional prefix (N/S/E/W) and use just the street name
    street_parts = parts[1:] if len(parts) > 1 else parts
    if street_parts and street_parts[0].upper() in ("N", "S", "E", "W", "NE", "NW", "SE", "SW"):
        street_parts = street_parts[1:]
    street_name = street_parts[0] if street_parts else ""

    parcel = await _get_parcel_info(house_number, street_name)
    if parcel:
        logger.info(
            "Assessor: subdivision=%s block=%s lot=%s instruments=%s",
            parcel["subdivision"],
            parcel["block"],
            parcel["lot"],
            parcel["instruments"],
        )
    else:
        logger.warning("Could not retrieve parcel info from assessor — falling back to address search")

    dest = make_download_dir(geocoded.county, address, base=tmp_dir)
    saved, total_cost, total_in, total_out = await _download_documents(
        address, parcel, doc_filter, dest
    )
    if not saved:
        return [], f"No documents found for {address} in Jefferson County.", total_cost, total_in, total_out

    return saved, None, total_cost, total_in, total_out
