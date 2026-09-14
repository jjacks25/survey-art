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

from survey_art.county_sites import SUPPORTED_COUNTIES
from survey_art.document_filter import DEFAULT_FILTER, DocumentFilter
from survey_art.download import download_dir as make_download_dir
from survey_art.geocode import GeocodedAddress
from survey_art.llm import agent_cost, get_llm

logger = logging.getLogger(__name__)

_ENTRY = next(e for e in SUPPORTED_COUNTIES if e["county"] == "Jefferson")
_RECORDS_URL = "https://landrecords.co.jefferson.co.us/RealEstate/SearchEntry.aspx"
_ASSESSOR_API = "https://propertysearch.jeffco.us/api"

# Required by the assessor API for all list endpoints — without these it returns 500.
# Address search uses Take=500: most streets have < 500 properties, so this resolves in one
# call and the API returns full result objects including the subdivision field.
# The pagination loop handles streets with > 500 properties.
_LIST_PARAMS = {"Skip": 0, "Take": 500, "page": 1, "sortBy": "houseNumber", "sortDirection": "asc"}
_PAGE_SIZE = 500


async def _get_parcel_info(house_number: str, street_fragment: str) -> dict | None:
    """
    Call the Jefferson County Assessor REST API to retrieve parcel details.
    Returns subdivision, block, lot, and any previously recorded instrument numbers.
    No browser required — all endpoints are unauthenticated JSON.

    street_fragment is everything after the house number in the geocoded address,
    e.g. "S POPPY ST". Used both to derive the API streetName param and to verify
    the matched property address (avoids matching "2180 S POPPY CT" when looking
    for "2180 S POPPY ST").
    """
    # Derive the streetName API param: skip directional prefix, take the street name word.
    # Preserve original case from the geocoded address — the assessor API is case-sensitive
    # and returns richer result objects (including subdivision) when the casing matches
    # what is stored (e.g. "Poppy" not "POPPY").
    # e.g. "S Poppy Street" → skip "S" → "Poppy"
    frag_tokens = street_fragment.split()
    if frag_tokens and frag_tokens[0].upper() in ("N", "S", "E", "W", "NE", "NW", "SE", "SW"):
        frag_tokens = frag_tokens[1:]
    street_name = frag_tokens[0] if frag_tokens else street_fragment

    # Use only the street name word for address matching (case-insensitive).
    # The Census geocoder may return "Street" but the assessor stores "ST", so matching the
    # full fragment including suffix would fail. The street name alone ("POPPY") is
    # sufficient to disambiguate in almost all cases.

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Address search → uniquePropertyId
        # The streetName search returns ALL streets with that name county-wide (can be 1000+).
        # We page through in chunks until we find an exact address match.
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
                    (
                        p
                        for p in items
                        if str(p.get("propertyAddress", "")).startswith(house_number)
                    ),
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

        # Subdivision name: prefer the address search result; fall back to property detail.
        # The address API sometimes returns an empty subdivision field — the property
        # detail endpoint is more reliable but slower, so only call it when needed.
        subdivision = prop.get("subdivision", "")
        if not subdivision:
            try:
                r4 = await client.get(f"{_ASSESSOR_API}/property/{uid}")
                r4.raise_for_status()
                detail = r4.json() or {}
                # Response structure: {"propertyDetails": {"subdivision": "...", ...}}
                subdivision = (detail.get("propertyDetails") or {}).get("subdivision") or ""
            except Exception as exc:
                logger.warning("Property detail lookup failed: %s", exc)

    # Strip leading numeric code from subdivision (e.g. "693499 SOLTERRA SUB FLG NO 17" → "SOLTERRA SUB FLG NO 17")
    parts = subdivision.split(" ", 1)
    if parts and parts[0].isdigit():
        subdivision = parts[1] if len(parts) > 1 else subdivision

    if not subdivision:
        logger.warning("Assessor: subdivision name could not be resolved; legal=%s", legal)

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
        subdiv_keyword = subdivision.split()[0] if subdivision else ""

        # Build subdivision-dependent steps only when we have a subdivision name
        if subdivision:
            subdiv_steps = (
                f"STEP 1 — Search by legal description (finds ISPs, ILCs, easements, deeds for this lot):\n"
                f"  Navigate to {_RECORDS_URL}\n"
                f"  The Subdivision field is likely an autocomplete. To fill it:\n"
                f"    - Click the Subdivision field and type the first word only: '{subdiv_keyword}'\n"
                f"    - Wait 1–2 seconds for a dropdown list to appear\n"
                f"    - Select the entry that most closely matches '{subdivision}'\n"
                f"    - If no dropdown appears, clear the field and leave it blank\n"
                f"  Fill in Block='{block}' and Lot='{lot}', then click Search.\n\n"
                f"STEP 2 — Search by subdivision name only (finds the recorded subdivision plat):\n"
                f"  Clear the form. The subdivision plat is filed for the whole subdivision and will NOT\n"
                f"  appear in a block/lot search — you must search by subdivision name with Block and Lot blank.\n"
                f"  Fill in the Subdivision field using the same autocomplete technique above (type '{subdiv_keyword}',\n"
                f"  wait for dropdown, select the entry matching '{subdivision}').\n"
                f"  Leave Block and Lot blank. Click Search.\n\n"
            )
            next_step = "STEP 3"
        else:
            subdiv_steps = (
                f"STEP 1 — Search by block and lot:\n"
                f"  Navigate to {_RECORDS_URL}\n"
                f"  Fill in Block='{block}' and Lot='{lot}' (no subdivision available), then click Search.\n\n"
            )
            next_step = "STEP 2"

        search_instructions = (
            f"The property's legal description is: Subdivision='{subdivision}', "
            f"Block='{block}', Lot='{lot}'. "
            f"Known deed instrument numbers from the assessor: {instrument_str}.\n\n"
            f"You must perform ALL of the following searches — do not stop early.\n\n"
            f"{subdiv_steps}"
            f"{next_step} — Search by each known instrument number (finds the recorded deeds):\n"
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
        f"FINAL STEP — Download ALL matching documents found across all searches above:\n"
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

    # Parse house number and street fragment from the geocoded address.
    # geocoded.street is the Census USPS-normalized form, e.g. "2180 S POPPY ST".
    # We pass the full fragment after the house number so the assessor lookup
    # can verify street suffix (ST vs CT vs DR) and avoid false matches.
    street = geocoded.street
    parts = street.split()
    house_number = parts[0] if parts else ""
    street_fragment = " ".join(parts[1:]) if len(parts) > 1 else ""

    parcel = await _get_parcel_info(house_number, street_fragment)
    if parcel:
        logger.info(
            "Assessor: subdivision=%s block=%s lot=%s instruments=%s",
            parcel["subdivision"],
            parcel["block"],
            parcel["lot"],
            parcel["instruments"],
        )
    else:
        logger.warning(
            "Could not retrieve parcel info from assessor — falling back to address search"
        )

    dest = make_download_dir(geocoded.county, address, base=tmp_dir)
    saved, total_cost, total_in, total_out = await _download_documents(
        address, parcel, doc_filter, dest
    )
    if not saved:
        return (
            [],
            f"No documents found for {address} in Jefferson County.",
            total_cost,
            total_in,
            total_out,
        )

    return saved, None, total_cost, total_in, total_out
