"""Weld County, CO — property records scraper.

Workflow
--------
1. Property Portal (apps.weld.gov/propertyportal/)
   - Browser agent searches by situs address.
   - Extracts the account number (e.g. R1234567).

2. Property Report (propertyreport.weld.gov/?account=RXXXXXXX)
   - Direct HTTP — no browser, no CAPTCHA.
   - Returns a full document history table: reception number, date, type,
     grantor, grantee, and a direct link to each document image.

3. Recorder Document Download (recording.weld.gov)
   - Documents live at recording.weld.gov/web/web/integration/document/{id}
   - Gated behind a click-through disclaimer with reCAPTCHA.
   - Stealth browser (--disable-blink-features=AutomationControlled) hides
     the Playwright automation fingerprint so reCAPTCHA typically shows only
     a simple checkbox instead of an image challenge.
   - Agent accepts the disclaimer once, then iterates through document URLs.

Key Notes
---------
- Reception numbers are the canonical lookup key in the Weld recorder system.
- propertyreport.weld.gov is CAPTCHA-free and returns complete doc history.
- Document type codes: SWD/SWDN = Special Warranty Deed, WD/WDN = Warranty
  Deed, QCD = Quit Claim Deed, SUB = Subdivision Plat, ESMT = Easement,
  DOT = Deed of Trust, LSP = Land Survey Plat, ISP = Improvement Survey Plat.
"""

from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from land_survey_scraper.county_sites import SUPPORTED_COUNTIES
from land_survey_scraper.document_filter import DEFAULT_FILTER, DocumentFilter
from land_survey_scraper.download import download_dir as make_download_dir
from land_survey_scraper.geocode import GeocodedAddress
from land_survey_scraper.settings import get_settings

logger = logging.getLogger(__name__)

_ENTRY = next(e for e in SUPPORTED_COUNTIES if e["county"] == "Weld")
_PORTAL_URL = _ENTRY["urls"]["property_portal"]
_PORTAL_SEARCH_URL = "https://apps.weld.gov/propertyportal/index.cfm"
_RECORDER_DISCLAIMER = _ENTRY["urls"]["recorder"]
_PROPERTY_REPORT_URL = "https://propertyreport.weld.gov/"

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Survey-relevant document type codes used by Weld County recorder.
# Codes not in this set are skipped (mortgage releases, tax liens, etc.)
_SURVEY_TYPE_CODES = {
    # Plats / surveys
    "SURV",  # Survey / Site Plan
    "SUB",   # Subdivision Plat
    "LSP",   # Land Survey Plat
    "ISP",   # Improvement Survey Plat
    "ILC",   # Improvement Location Certificate
    "ALTA",  # ALTA/NSPS Survey
    "PLAT",  # Generic plat
    "AMDPL", # Amended Plat
    "CORPL", # Correction Plat
    "VACPL", # Vacating Plat
    "CONDPL",# Condominium Plat
    # Deeds (ownership chain)
    "WD",    # Warranty Deed
    "WDN",   # Warranty Deed (Non-Money)
    "SWD",   # Special Warranty Deed
    "SWDN",  # Special Warranty Deed (Non-Money)
    "QCD",   # Quit Claim Deed
    "QCN",   # Quit Claim Deed (Non-Money)
    "QCDN",  # Quit Claim Deed (Non-Money, alternate code)
    "PRD",   # Personal Representative Deed
    "TRD",   # Trustee Deed
    "GD",    # General Deed
    # Easements & ROW
    "ESMT",  # Easement
    "ROW",   # Right of Way
    "ROWE",  # Right of Way Easement
    "AE",    # Access Easement
    "UE",    # Utility Easement
    "DE",    # Drainage Easement
    "CE",    # Conservation Easement
    # Government / public records
    "RES",   # Resolution
    "ORD",   # Ordinance
    "COD",   # Certificate of Dedication
    "NOC",   # Notice of Condemnation
}


@dataclass
class _DocRecord:
    reception: str
    rec_date: str
    doc_type: str
    grantor: str
    grantee: str
    url: str
    doc_fee: str = ""
    sale_date: str = ""
    sale_price: str = ""

    def to_dict(self) -> dict:
        return {
            "reception": self.reception,
            "rec_date": self.rec_date,
            "doc_type": self.doc_type,
            "grantor": self.grantor,
            "grantee": self.grantee,
            "doc_fee": self.doc_fee,
            "sale_date": self.sale_date,
            "sale_price": self.sale_price,
            "url": self.url,
        }


# All of Weld County is north of the 6th P.M. base line and west of the meridian,
# so township direction is always 'N' and range direction is always 'W'.
_WELD_TOWNSHIP_DIR = "N"
_WELD_RANGE_DIR = "W"


def _format_township(value: str) -> str:
    """Convert raw numeric township (e.g. '05') to SOP form ('5N')."""
    v = value.lstrip("0") or value
    return f"{v}{_WELD_TOWNSHIP_DIR}" if v else ""


def _format_range(value: str) -> str:
    """Convert raw numeric range (e.g. '67') to SOP form ('67W')."""
    v = value.lstrip("0") or value
    return f"{v}{_WELD_RANGE_DIR}" if v else ""


@dataclass
class ParcelInfo:
    """Identify Results panel fields, per SOP Phase 1 Step 1.5.

    Persisted to the run log because Owner, Account, Parcel, and S-T-R drive
    branching searches in Phases 3B and 3C.

    `township` and `range_` are stored in SOP form ("5N", "67W") — not the
    raw numeric form returned by the property report ("05", "67").
    """
    account: str
    parcel_id: str = ""
    owner: str = ""
    address: str = ""
    subdivision: str = ""
    section: str = ""
    township: str = ""
    range_: str = ""
    source: str = "http"  # "http" | "browser"

    def section_township_range(self) -> str:
        """SOP form: 'S15-T5N-R67W'. Empty string if no PLSS fields populated."""
        if not (self.section or self.township or self.range_):
            return ""
        return f"S{self.section}-T{self.township}-R{self.range_}"

    def to_dict(self) -> dict:
        return {
            "owner": self.owner,
            "account": self.account,
            "parcel_id": self.parcel_id,
            "address": self.address,
            "subdivision": self.subdivision,
            "section": self.section,
            "township": self.township,
            "range": self.range_,
            "section_township_range": self.section_township_range(),
            "source": self.source,
        }


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def _snake_case(label: str) -> str:
    """Convert a UI label to snake_case for JSON output.

    Examples:
        'Owner Name'                  -> 'owner_name'
        'Local Govt Assessed Value'   -> 'local_govt_assessed_value'
        'Land SqFt'                   -> 'land_sqft'
        'Notice of Valuation (NOV/NOD)' -> 'notice_of_valuation_nov_nod'
    """
    return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()


def _extract_data_labels(html: str) -> dict[str, str]:
    """Extract every `<... data-label="X">value</...>` pair as a dict.

    The Weld portals (both apps.weld.gov and propertyreport.weld.gov) annotate
    every result cell with a `data-label` attribute identifying the column.
    First occurrence wins — duplicate labels are ignored.
    """
    result: dict[str, str] = {}
    # Labels can be quoted ("Account") or unquoted (Account). The label charset
    # avoids '>' and quote characters so the match terminates naturally.
    pat = re.compile(
        r"""data-label=(?:"([^"]+)"|'([^']+)'|([A-Za-z][A-Za-z #/_-]*))[^>]*>(.*?)</""",
        re.DOTALL | re.IGNORECASE,
    )
    for m in pat.finditer(html):
        raw_label = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if not raw_label or raw_label.lower().startswith("sort by"):
            continue  # header rows use data-label="Sort by: "
        key = _snake_case(raw_label)
        if not key or key in result:
            continue
        result[key] = _strip_tags(m.group(4))
    return result


def _portal_post(query: str) -> str | None:
    """POST a free-text query to apps.weld.gov/propertyportal/index.cfm.

    Accepts addresses, owner names, account numbers, and parcel IDs.
    Returns the raw HTML response, or None on failure.
    """
    with httpx.Client(timeout=15.0, headers=_HTTP_HEADERS, follow_redirects=True) as client:
        try:
            resp = client.post(_PORTAL_SEARCH_URL, data={"searchInput": query})
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Property portal POST failed for '%s': %s", query, exc)
            return None
    return resp.text


def _parse_portal_rows(html: str) -> list[ParcelInfo]:
    """Parse each `<tr class="resultRow ...">` block into a ParcelInfo."""
    rows: list[ParcelInfo] = []
    row_re = re.compile(
        r"<tr[^>]*class=['\"]resultRow[^'\"]*['\"][^>]*>(.*?)</tr>",
        re.DOTALL | re.IGNORECASE,
    )
    for m in row_re.finditer(html):
        labels = _extract_data_labels(m.group(1))
        account = labels.get("account", "").upper()
        if not re.match(r"^R\d{5,9}$", account):
            continue
        rows.append(ParcelInfo(
            account=account,
            parcel_id=labels.get("parcel", ""),
            owner=labels.get("owner", ""),
            address=labels.get("location", "").strip(", "),
            subdivision=labels.get("subdivision", ""),
            source="http",
        ))
    return rows


@functools.lru_cache(maxsize=8)
def _fetch_property_report_html(account: str) -> str:
    """Single cached HTTP GET of the property report — backing store for both
    the field-dict and the document-history parsers, plus Phase 2's
    'No documents found.' detection.
    """
    with httpx.Client(timeout=15.0, headers=_HTTP_HEADERS, follow_redirects=True) as client:
        try:
            resp = client.get(_PROPERTY_REPORT_URL, params={"account": account})
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Property report fetch failed for %s: %s", account, exc)
            return ""
    return resp.text


def _fetch_report_fields(account: str) -> dict[str, str]:
    """Pull Section / Township / Range and other data-label fields from the report."""
    return _extract_data_labels(_fetch_property_report_html(account))


def _pick_best_row(
    rows: list[ParcelInfo], query: str, query_type: str
) -> ParcelInfo | None:
    """Choose the most likely match from a list of portal result rows."""
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    q_upper = query.upper().strip()
    if query_type == "address":
        for r in rows:
            if q_upper in r.address.upper():
                return r
    elif query_type == "owner":
        for r in rows:
            if q_upper in r.owner.upper():
                return r
    logger.warning(
        "Portal returned %d rows for %r (%s); using first match. "
        "Disambiguate by passing the account number directly.",
        len(rows), query, query_type,
    )
    return rows[0]


def _get_parcel_info_http(
    query: str, query_type: str = "address"
) -> ParcelInfo | None:
    """SOP Phase 1 Steps 1.1–1.5 via direct HTTP (no browser).

    `query_type` is one of {"account", "address", "owner", "parcel"}.
    STR queries are not supported via HTTP — the portal search box is free-text
    only. Use the browser-walk path (`--sop-strict`) for STR lookups.
    """
    if query_type == "account":
        info = ParcelInfo(account=query.upper(), source="http")
    elif query_type == "parcel":
        rows = _parse_portal_rows(_portal_post(query) or "")
        info = _pick_best_row(rows, query, "parcel")
        if not info:
            logger.warning("Portal: no parcel match for %s", query)
            return None
    else:
        # Address or owner — use just the first comma segment (city/state/zip
        # in the query degrades match quality).
        primary = query.split(",")[0].strip() if query_type == "address" else query
        rows = _parse_portal_rows(_portal_post(primary) or "")
        info = _pick_best_row(rows, primary, query_type)
        if not info:
            logger.warning("Portal: no %s match for %r", query_type, primary)
            return None

    # Enrich with Section / Township / Range from the property report.
    fields = _fetch_report_fields(info.account)
    info.section = (fields.get("section") or info.section).lstrip("0") or fields.get("section", "")
    info.township = _format_township(fields.get("township") or info.township)
    info.range_ = _format_range(fields.get("range") or info.range_)
    # Fall back to report-derived fields if portal POST left them blank.
    # The report uses "owner_name" / "property_address" rather than "owner" / "address".
    if not info.owner:
        info.owner = fields.get("owner_name", "") or fields.get("owner", "")
    if not info.parcel_id:
        info.parcel_id = fields.get("parcel", "")
    if not info.address:
        situs = fields.get("property_address", "").strip()
        city = fields.get("property_city", "").strip()
        if situs or city:
            info.address = ", ".join(p for p in (situs, city) if p)
        else:
            # No situs — fall back to mailing address (owner's contact, not parcel location).
            info.address = fields.get("address", "")
    if not info.subdivision:
        info.subdivision = fields.get("subdivision", "")
    return info


def _get_account_number(address: str) -> str | None:
    """Back-compat shim: return just the account number for an address query."""
    info = _get_parcel_info_http(address, "address")
    return info.account if info else None


# ---------------------------------------------------------------------------
# Browser-walk Phase 1 (SOP-strict path)
# ---------------------------------------------------------------------------

_GIS_LANDING = "https://www.weld.gov/Government/Departments/Geographic-Information-Systems"
_PROPERTY_PORTAL_MAP = "https://maps.weld.gov/propertyportal/"


async def _get_parcel_info_browser(
    query: str,
    query_type: str = "address",
    *,
    str_input: str = "",
    owner_input: str = "",
) -> ParcelInfo | None:
    """SOP Phase 1 Steps 1.1–1.5 via literal Playwright browser walk.

    Drives the actual UI the surveyor sees:
      1.1 GIS landing page (weld.gov/.../Geographic-Information-Systems)
      1.2 click 'Interactive Maps' tile
      1.3 click 'View Property Portal' → maps.weld.gov/propertyportal/
      1.4 use Address / S-T-R / Owner search per priority
      1.5 click highlighted parcel → read Identify Results panel

    Set WELD_HEADED=1 to watch the browser drive itself.
    """
    import os
    from playwright.async_api import async_playwright

    headed = os.environ.get("WELD_HEADED", "").lower() in ("1", "true", "yes")
    info: ParcelInfo | None = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
            slow_mo=400 if headed else 0,
        )
        ctx = await browser.new_context(user_agent=_HTTP_HEADERS["User-Agent"])
        page = await ctx.new_page()

        try:
            # SOP Step 1.3 — go straight to the map (skipping 1.1/1.2 in this
            # iteration; we can chain the tile clicks later if needed).
            logger.info("SOP 1.3: opening Property Portal map")
            await page.goto(_PROPERTY_PORTAL_MAP, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=20_000)

            # SOP Step 1.4 — issue the right search per priority.
            await _browser_run_search(page, query, query_type, str_input, owner_input)

            # SOP Step 1.5 — click the highlighted parcel and read the Identify panel.
            await page.wait_for_timeout(2_000)  # let the map zoom + highlight settle
            info = await _browser_read_identify_panel(page)
            if info:
                info.source = "browser"
        except Exception as exc:
            logger.warning("Browser walk failed: %s", exc)
        finally:
            await browser.close()

    if info and info.account and not info.section:
        # Enrich S/T/R from property report to match the HTTP path's output shape.
        fields = _fetch_report_fields(info.account)
        info.section = fields.get("section", "")
        info.township = _format_township(fields.get("township", ""))
        info.range_ = _format_range(fields.get("range", ""))
    return info


async def _browser_run_search(
    page, query: str, query_type: str, str_input: str, owner_input: str
) -> None:
    """SOP Step 1.4 — drive whichever Search ribbon button matches the input."""
    if query_type in ("account", "parcel"):
        # Portal has Account # / Parcel # buttons. Use the matching one.
        button_label = "Account #" if query_type == "account" else "Parcel #"
        logger.info("SOP 1.4: clicking %r search", button_label)
        await page.get_by_role("button", name=re.compile(button_label, re.I)).first.click()
        await page.locator("input:visible").first.fill(query)
        await page.keyboard.press("Enter")
        return
    if query_type == "address":
        logger.info("SOP 1.4 Priority 1: Address search for %r", query)
        await page.get_by_role("button", name=re.compile(r"^Address$", re.I)).first.click()
        await page.locator("input:visible").first.fill(query.split(",")[0].strip())
        await page.keyboard.press("Enter")
        return
    if str_input:
        section, township, range_ = (p.strip() for p in str_input.split(",", 2))
        logger.info("SOP 1.4 Priority 3: S-T-R search %s/%s/%s", section, township, range_)
        await page.get_by_role("button", name=re.compile(r"S-?T-?R", re.I)).first.click()
        inputs = page.locator("input:visible")
        await inputs.nth(0).fill(section)
        await inputs.nth(1).fill(township)
        await inputs.nth(2).fill(range_)
        await page.keyboard.press("Enter")
        return
    if owner_input or query_type == "owner":
        owner = owner_input or query
        logger.info("SOP 1.4 Priority 4: Owner search for %r", owner)
        await page.get_by_role("button", name=re.compile(r"^Owner$", re.I)).first.click()
        await page.locator("input:visible").first.fill(owner)
        await page.keyboard.press("Enter")
        return
    raise ValueError(f"No usable search input for query_type={query_type!r}")


async def _browser_read_identify_panel(page) -> ParcelInfo | None:
    """SOP Step 1.5 — read the Identify Results panel that appears in the left rail.

    The panel renders Owner / Account / Parcel / Address / Subdivision / S-T-R
    as `label: value` text. We grab the panel's textContent and regex it out
    rather than relying on fragile DOM selectors.
    """
    panel = page.locator("text=/Identify\\s+(Results|results)/").first
    try:
        await panel.wait_for(state="visible", timeout=15_000)
    except Exception:
        logger.warning("Identify Results panel never appeared")
        return None

    # Take the surrounding container's text — the panel is a sibling group.
    text = await page.evaluate(
        """() => {
            const m = Array.from(document.querySelectorAll('*'))
              .find(n => n.textContent && n.textContent.startsWith('Identify'));
            return m ? m.closest('[class*="results"], [class*="Results"], aside, section')?.innerText
                     : document.body.innerText;
        }"""
    ) or ""

    def grab(label: str) -> str:
        m = re.search(rf"{label}\s*[:\s]\s*([^\n]+)", text, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    account = grab("Account")
    m = re.match(r"(R\d{5,9})", account, re.IGNORECASE)
    account = m.group(1).upper() if m else ""
    if not account:
        logger.warning("Identify panel did not contain an account number")
        return None

    str_text = grab("Section") or ""
    s_m = re.search(r"Section[: ]+(\d+)", text, re.IGNORECASE)
    t_m = re.search(r"Township[: ]+(\d+[NS]?)", text, re.IGNORECASE)
    r_m = re.search(r"Range[: ]+(\d+[EW]?)", text, re.IGNORECASE)

    return ParcelInfo(
        account=account,
        parcel_id=grab("Parcel"),
        owner=grab("Owner"),
        address=grab("Address"),
        subdivision=grab("Subdivision"),
        section=s_m.group(1) if s_m else "",
        township=t_m.group(1) if t_m else "",
        range_=r_m.group(1) if r_m else "",
        source="browser",
    )


def _fetch_document_history(account: str) -> list[_DocRecord]:
    """Parse the Document History table from the (cached) property report HTML."""
    html = _fetch_property_report_html(account)
    records: list[_DocRecord] = []

    # Each data row contains a Reception cell with a link to recording.weld.gov.
    # HTML uses single-quoted attributes and data-label markers, e.g.:
    #   <td data-label='Reception'><a href='https://recording.weld.gov/...'
    #       target='_blank'>NNN</a></td>
    #   <td class="nowrap" data-label='Rec Date'>07-06-1994</td>
    #   <td data-label='Type'>SWDN</td>
    #   <td data-label='Grantor'>NAME</td>
    #   <td data-label='Grantee'>NAME</td>

    def _cell(label: str, row_html: str) -> str:
        """Extract text content of the td with the given data-label."""
        m = re.search(
            rf"data-label=['\"]{{0,1}}{re.escape(label)}['\"]{{0,1}}[^>]*>(.*?)</td>",
            row_html,
            re.DOTALL | re.IGNORECASE,
        )
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""

    row_re = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    link_re = re.compile(
        r"href=['\"]?(https://recording\.weld\.gov/[^'\" >]+)['\"]?[^>]*>(\d+)<",
        re.IGNORECASE,
    )

    for row_m in row_re.finditer(html):
        row_html = row_m.group(1)
        link_m = link_re.search(row_html)
        if not link_m:
            continue
        records.append(_DocRecord(
            reception=link_m.group(2).strip(),
            rec_date=_cell("Rec Date", row_html),
            doc_type=_cell("Type", row_html).upper(),
            grantor=_cell("Grantor", row_html),
            grantee=_cell("Grantee", row_html),
            doc_fee=_cell("Doc Fee", row_html),
            sale_date=_cell("Sale Date", row_html),
            sale_price=_cell("Sale Price", row_html),
            url=link_m.group(1).strip(),
        ))

    logger.info(
        "Property report for %s: found %d document records", account, len(records)
    )
    return records


# Field -> accordion section grouping, based on the SOP Step 1.6 accordion layout.
# Keys are snake_case (matching _extract_data_labels output). Any field not in
# this map gets placed in `other`.
_REPORT_SECTION_MAP: dict[str, list[str]] = {
    "account_information": [
        "account", "parcel", "account_type", "tax_year", "buildings",
        "actual_value", "assessed_value", "local_govt_assessed_value",
        "school_assessed_value", "legal", "subdivision", "block", "lot",
        "land_economic_area", "property_address", "property_city",
        "section", "township", "range",
    ],
    "owners": ["owner_name", "address"],
    "land_information": ["code", "description", "acres", "land_sqft"],
    "valuation_information": [
        "actual_value", "assessed_value", "local_govt_assessed_value",
        "school_assessed_value",
    ],
    "tax_authorities": [
        "tax_area", "district_id", "district_name",
        "current_mill_levy", "school_mill_levy", "taxes",
    ],
}

# Document-history columns — excluded from the `other` bucket because they're
# captured separately as structured rows in document_history[].
_DOC_HISTORY_LABELS: set[str] = {
    "reception", "rec_date", "type", "grantor", "grantee",
    "doc_fee", "sale_date", "sale_price",
}


def _group_report_fields(fields: dict[str, str]) -> dict[str, dict]:
    """Group the flat property-report field map into named accordion sections.

    Sections mirror the SOP Step 1.6 accordion: Owner(s), Account Information,
    Document History (handled separately), Building Information, Valuation
    Information, Tax Authorities, Notice of Valuation, etc.
    """
    grouped: dict[str, dict] = {name: {} for name in _REPORT_SECTION_MAP}
    grouped["other"] = {}
    placed: set[str] = set()
    for section, labels in _REPORT_SECTION_MAP.items():
        for label in labels:
            if label in fields:
                grouped[section][label] = fields[label]
                placed.add(label)
    for label, value in fields.items():
        if label not in placed and label not in _DOC_HISTORY_LABELS:
            grouped["other"][label] = value
    return grouped


_MAP_IFRAME_URL = "https://maps.weld.gov/mapanaccount/?Account={account}"


async def _capture_map_image(account: str, dest_dir: Path) -> Path | None:
    """Save the property-report Map accordion as PNG.

    The Map section embeds an iframe at maps.weld.gov/mapanaccount/?Account=R...
    which renders an ESRI JS API map showing the parcel boundary highlighted in
    red against a satellite basemap. We render that iframe URL directly in
    Playwright and screenshot the viewport — same image a surveyor would see in
    the SOP Step 1.7 Map accordion.
    """
    from playwright.async_api import async_playwright

    out = dest_dir / "map.png"
    url = _MAP_IFRAME_URL.format(account=account)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(
                viewport={"width": 1600, "height": 900},
                user_agent=_HTTP_HEADERS["User-Agent"],
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=20_000)
            # ESRI applies the parcel selection layer (the red boundary highlight)
            # only after the basemap tiles are fully painted. Wait longer than the
            # tile load alone needs, and additionally poll for the SVG layer that
            # ESRI uses to draw the selection geometry.
            try:
                await page.wait_for_function(
                    """() => {
                        const svgs = document.querySelectorAll('svg');
                        return Array.from(svgs).some(s => s.querySelectorAll('path').length > 0);
                    }""",
                    timeout=15_000,
                )
            except Exception:
                pass  # selection layer may not render; fall through and screenshot anyway
            await page.wait_for_timeout(5_000)
            dest_dir.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out), full_page=False)
            logger.info("Map image saved: %s", out)
            return out
        except Exception as exc:
            logger.warning("Map capture failed for %s: %s", account, exc)
            return None
        finally:
            await browser.close()


def _filter_survey_docs(records: list[_DocRecord]) -> list[_DocRecord]:
    """Keep only documents relevant to land survey research."""
    filtered = [r for r in records if r.doc_type in _SURVEY_TYPE_CODES]
    logger.info(
        "Document filter: %d/%d records match survey types", len(filtered), len(records)
    )
    return filtered


# ---------------------------------------------------------------------------
# Phase 2 — Decision Matrix (SOP Phase 2)
# ---------------------------------------------------------------------------

# Vesting deeds per the SOP doc-type cheat sheet. These are the "conveyance"
# rows that establish chain of title. SOP Path 3A.4 / 3B.2 use {WD, SWD, GEN}
# and {SWD, WD, QCD, GEN} respectively; the non-money variants (WDN, SWDN,
# QCN, QCDN) appear in real data and count for routing purposes.
_VESTING_DEED_TYPES = {"WD", "WDN", "SWD", "SWDN", "QCD", "QCN", "QCDN", "GEN"}

# Survey row indicator (Condition A visual trigger). The Document History
# "Type" column uses 'SURV' for all surveys including ALTA — the SOP cheat
# sheet maps SURV → "Survey (incl. ALTA Land Title Survey)".
_SURVEY_ROW_TYPES = {"SURV"}

# Literal text that, when present, distinguishes a truly empty document
# history (route to Path C) from a UI rendering error (retry — per
# tie-breaker rule #3).
_NO_DOCS_MESSAGE = "No documents found."


def _decision_matrix(records: list[_DocRecord], html: str) -> dict:
    """SOP Phase 2 — evaluate Conditions A/B/C against the Decision Frame.

    Apply IF/THEN/ELSE in order; stop at first match. Tie-breakers:
      - If both A and B technically match, prefer A (Path 3B is reachable
        as a fallback when 3A fails — SOP Step 3A.4).
      - Never route to C unless the literal 'No documents found.' string
        is present. A blank section without that text is a UI error.

    Returns a dict shaped for direct serialization to overview.json:

        {
          "path": "A" | "B" | "C" | "UNROUTABLE",
          "reasoning": ["..."],
          "vesting_deed_present": bool,
          "survey_present": bool,
          "empty_state_message_present": bool,
          "row_count": int,
          "vesting_deeds":   [_DocRecord.to_dict(), ...],   # all vesting rows
          "survey_rows":     [_DocRecord.to_dict(), ...],   # all SURV rows
          "most_recent_survey": _DocRecord.to_dict() | None,
          "most_recent_vesting_deed": _DocRecord.to_dict() | None,
        }
    """
    vesting = [r for r in records if r.doc_type in _VESTING_DEED_TYPES]
    surveys = [r for r in records if r.doc_type in _SURVEY_ROW_TYPES]
    no_docs = _NO_DOCS_MESSAGE in html

    # Most-recent helpers — rec_date is MM-DD-YYYY; lexicographic max doesn't
    # work, so parse the year/month/day.
    def _date_key(r: _DocRecord) -> tuple:
        try:
            m, d, y = r.rec_date.split("-")
            return (int(y), int(m), int(d))
        except (ValueError, AttributeError):
            return (0, 0, 0)

    most_recent_survey = max(surveys, key=_date_key) if surveys else None
    most_recent_vesting = max(vesting, key=_date_key) if vesting else None

    base = {
        "vesting_deed_present": bool(vesting),
        "survey_present": bool(surveys),
        "empty_state_message_present": no_docs,
        "row_count": len(records),
        "vesting_deeds": [v.to_dict() for v in vesting],
        "survey_rows": [s.to_dict() for s in surveys],
        "most_recent_survey": most_recent_survey.to_dict() if most_recent_survey else None,
        "most_recent_vesting_deed": most_recent_vesting.to_dict() if most_recent_vesting else None,
    }

    # Condition A — both a vesting deed AND at least one SURV row.
    if vesting and surveys:
        reasoning = [
            f"Condition A matched: {len(records)} row(s) including "
            f"{len(vesting)} vesting deed(s) [{', '.join(sorted({v.doc_type for v in vesting}))}] "
            f"and {len(surveys)} SURV row(s).",
            f"Most recent SURV: reception {most_recent_survey.reception} "
            f"({most_recent_survey.rec_date}). Expected to correspond to a recorded ALTA.",
            f"Most recent vesting deed: reception {most_recent_vesting.reception} "
            f"({most_recent_vesting.doc_type}, {most_recent_vesting.rec_date}).",
            "→ Route to Phase 3A (Happy Path).",
        ]
        return {"path": "direct", "sop_letter": "A", "reasoning": reasoning, **base}

    # Condition B — rows present, but missing either survey or vesting deed.
    if records and (bool(vesting) ^ bool(surveys) or (not vesting and not surveys)):
        missing = "vesting deed" if surveys else "SURV row"
        if not vesting and not surveys:
            missing = "vesting deed AND SURV row"
        reasoning = [
            f"Condition B matched: {len(records)} row(s) present but no {missing}.",
            f"Doc types on record: {', '.join(sorted({r.doc_type for r in records}))}.",
            "→ Route to Phase 3B (Alternative Research Path 1 — "
            "S/T/R Advanced Search for easements/ROW).",
        ]
        return {"path": "alternate_partial", "sop_letter": "B", "reasoning": reasoning, **base}

    # Condition C — empty AND the literal "No documents found." text is shown.
    if not records and no_docs:
        reasoning = [
            "Condition C matched: Document History is empty AND "
            f"'{_NO_DOCS_MESSAGE}' is present below the section header.",
            "→ Route to Phase 3C (Alternative Research Path 2 — "
            "owner-name search + S/T/R-driven exemption packet).",
        ]
        return {"path": "alternate_empty", "sop_letter": "C", "reasoning": reasoning, **base}

    # Tie-breaker #3: 0 rows + no message → UI error, not Path C.
    if not records and not no_docs:
        reasoning = [
            "UNROUTABLE: Document History returned 0 rows but the literal "
            f"'{_NO_DOCS_MESSAGE}' string was NOT present in the response.",
            "Per SOP tie-breaker rule #3, this is treated as a UI / rendering "
            "error rather than Condition C. Retry the run before routing.",
        ]
        return {"path": "unroutable", "sop_letter": None, "reasoning": reasoning, **base}

    # Defensive fallthrough — shouldn't happen given the conditions above.
    return {
        "path": "unroutable",
        "sop_letter": None,
        "reasoning": ["UNROUTABLE: no Decision Matrix condition matched. "
                      "This indicates a logic bug — investigate."],
        **base,
    }


_RECORDER_LOGIN_URL = "https://recording.weld.gov/web/user/login"

_BINARY_CONTENT_TYPES = {
    "image/tiff", "image/x-tiff", "image/tif",
    "image/jpeg", "image/png", "image/gif",
    "application/pdf", "application/octet-stream",
}
_DOC_EXTENSIONS = (".tif", ".tiff", ".pdf", ".jpg", ".jpeg", ".png")


async def _download_documents(
    address: str,
    docs: list[_DocRecord],
    doc_filter: DocumentFilter,
    dest_dir: Path,
    username: str = "",
    password: str = "",
) -> tuple[list[Path], float, int, int]:
    """Phase 3: download documents using Playwright with a full browser-side session.

    Does disclaimer acceptance and login entirely in the browser — no httpx cookie
    injection — so the JSESSIONID remains consistent throughout and Tyler Tech's
    session validation passes.

    When authenticated, the Tyler Tech viewer server-renders document images into
    #ImageDiv as <img> tags. The browser fetches those image URLs as separate HTTP
    requests, which we capture via the response interceptor.
    """
    if not docs:
        return [], 0.0, 0, 0

    import asyncio as _asyncio

    from playwright.async_api import async_playwright

    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    # Set WELD_HEADED=1 to watch the browser drive itself (useful for debugging).
    import os
    headed = os.environ.get("WELD_HEADED", "").lower() in ("1", "true", "yes")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
            slow_mo=500 if headed else 0,
        )
        ctx = await browser.new_context(
            user_agent=_HTTP_HEADERS["User-Agent"],
            accept_downloads=True,
        )

        # Inject disclaimerAccepted cookie directly. The disclaimer page's "I Accept"
        # button is gated by a Google reCAPTCHA that headless Chromium can't pass, but
        # the document viewer only checks for this cookie's presence.
        await ctx.add_cookies([{
            "name": "disclaimerAccepted",
            "value": "true",
            "domain": "recording.weld.gov",
            "path": "/",
        }])
        setup_page = await ctx.new_page()

        # Step 2: Login if credentials are provided
        if username and password:
            try:
                await setup_page.goto(
                    _RECORDER_LOGIN_URL, wait_until="domcontentloaded", timeout=20_000
                )
                await setup_page.fill('[name="field_UserId"]', username)
                await setup_page.fill('[name="field_Password"]', password)
                await setup_page.click('[type="submit"]')
                await setup_page.wait_for_load_state("networkidle", timeout=20_000)

                # If still showing the password field, login failed
                still_has_password = await setup_page.query_selector('[name="field_Password"]')
                if still_has_password:
                    logger.error(
                        "Login failed — check WELD_RECORDER_USERNAME/PASSWORD. "
                        "Register for free at recording.weld.gov"
                    )
                    await browser.close()
                    return [], 0.0, 0, 0
                logger.info("Logged in as %s", username)
            except Exception as exc:
                logger.warning("Login failed: %s", exc)

        await setup_page.close()

        # Step 3: Download each document
        for doc in docs:
            captured: list[tuple[str, bytes]] = []

            async def handle_response(resp, _doc=doc):
                url = resp.url
                if "recording.weld.gov" not in url:
                    return
                ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                url_path = url.lower().split("?")[0]
                is_doc = ct in _BINARY_CONTENT_TYPES or any(
                    url_path.endswith(ext) for ext in _DOC_EXTENSIONS
                )
                if not is_doc:
                    return
                try:
                    body = await resp.body()
                    if body and len(body) > 5_000:
                        captured.append((url, body))
                        logger.info("Captured %d bytes from %s", len(body), url[:80])
                except Exception as exc:
                    logger.debug("Could not read response body: %s", exc)

            doc_page = await ctx.new_page()
            doc_page.on("response", handle_response)

            try:
                await doc_page.goto(doc.url, wait_until="domcontentloaded", timeout=30_000)
                logger.info("Navigated to %s", doc.url)
                await doc_page.wait_for_load_state("networkidle", timeout=20_000)
                await _asyncio.sleep(2)

                # Fallback: check ImageDiv for <img> srcs in case we missed the network event
                if not captured:
                    img_srcs: list[str] = await doc_page.evaluate(
                        """() => {
                            const div = document.getElementById('ImageDiv');
                            if (!div) return [];
                            return Array.from(div.querySelectorAll('img'))
                                       .map(i => i.src)
                                       .filter(s => s && s.includes('recording.weld.gov'));
                        }"""
                    )
                    for src in img_srcs:
                        try:
                            body = await (await ctx.request.get(src)).body()
                            if body and len(body) > 5_000:
                                captured.append((src, body))
                                logger.info(
                                    "DOM fallback: captured %d bytes from %s",
                                    len(body),
                                    src[:80],
                                )
                        except Exception as exc:
                            logger.debug("DOM fallback fetch failed: %s", exc)

                if not captured:
                    # Log ImageDiv text to diagnose auth/access issues
                    image_div = await doc_page.query_selector("#ImageDiv")
                    if image_div:
                        text = (await image_div.inner_text()).strip()[:200]
                        logger.warning("ImageDiv for %s: %s", doc.reception, text)
                    else:
                        logger.warning("No #ImageDiv found for reception %s", doc.reception)

            except Exception as exc:
                logger.warning("Navigation error for %s: %s", doc.url, exc)

            for idx, (url, body) in enumerate(captured):
                suffix = Path(url.split("?")[0]).suffix.lower()
                if suffix not in _DOC_EXTENSIONS:
                    suffix = ".bin"
                if len(captured) > 1:
                    fname = f"reception_{doc.reception}_{idx + 1}{suffix}"
                else:
                    fname = f"reception_{doc.reception}{suffix}"
                dst = dest_dir / fname
                dst.write_bytes(body)
                saved.append(dst)
                logger.info("Saved %s (%d bytes)", dst.name, len(body))

            if not captured:
                logger.warning(
                    "No document data captured for reception %s (%s)",
                    doc.reception,
                    doc.doc_type,
                )

            await doc_page.close()

        await browser.close()

    logger.info("Weld County: saved %d file(s)", len(saved))
    return saved, 0.0, 0, 0


async def _resolve_parcel(
    geocoded: GeocodedAddress,
    *,
    str_input: str = "",
    owner_input: str = "",
    sop_strict: bool = False,
) -> ParcelInfo | None:
    """SOP Phase 1 — resolve any supported input to a ParcelInfo.

    Priority (per SOP Step 1.4):
      0. Account / parcel ID short-circuit  (skips the portal entirely)
      1. Address
      2. Section / Township / Range
      3. Owner name (last resort)
    """
    raw = geocoded.street.strip()

    # Determine which input slot we're working with, in priority order.
    if re.match(r"^R\d{5,9}$", raw, re.IGNORECASE):
        query, query_type = raw.upper(), "account"
    elif re.match(r"^\d{10,14}$", raw):
        query, query_type = raw, "parcel"
    elif raw and not raw.startswith(","):
        query, query_type = raw, "address"
    elif str_input:
        query, query_type = str_input, "str"
    elif owner_input:
        query, query_type = owner_input, "owner"
    else:
        logger.warning("Phase 1: no usable input (address, account, S/T/R, or owner)")
        return None

    logger.info(
        "Phase 1: routing %s lookup via %s path",
        query_type, "browser (SOP-strict)" if sop_strict else "HTTP",
    )

    if sop_strict:
        return await _get_parcel_info_browser(
            query, query_type, str_input=str_input, owner_input=owner_input,
        )
    if query_type == "str":
        logger.warning(
            "Phase 1: S/T/R input %r — HTTP path does not support STR queries. "
            "Use --sop-strict for browser-walk STR lookup.", str_input,
        )
        return None
    return _get_parcel_info_http(query, query_type)


def _log_identify_results(info: ParcelInfo) -> None:
    """Log the Identify Results panel state per SOP Step 1.5."""
    str_ = info.section_township_range() or "(unknown)"
    logger.info(
        "Phase 1 complete — Identify Results [%s]: "
        "Owner=%r  Account=%s  Parcel=%s  Address=%r  Subdivision=%r  S-T-R=%s",
        info.source, info.owner, info.account, info.parcel_id,
        info.address, info.subdivision, str_,
    )


async def scrape(
    geocoded: GeocodedAddress,
    tmp_dir: Path,
    doc_filter: DocumentFilter = DEFAULT_FILTER,
    *,
    str_input: str = "",
    owner_input: str = "",
    sop_strict: bool = False,
) -> tuple[list[Path], str | None, float, int, int]:
    """Scrape Weld County property records.

    Accepts inputs per SOP Phase 1 Step 1.4 priority order. `sop_strict=True`
    drives the literal browser walk; otherwise uses direct HTTP. Every phase
    writes to a per-property overview.json (see overview.py).
    """
    from land_survey_scraper.overview import Overview, overview_path

    address = geocoded.one_line()
    logger.info("Weld County scraper starting for: %s", address)

    # --- Phase 1: parcel discovery (SOP Steps 1.1–1.5) ---
    parcel = await _resolve_parcel(
        geocoded,
        str_input=str_input,
        owner_input=owner_input,
        sop_strict=sop_strict,
    )
    if not parcel:
        return [], (
            f"Phase 1 failed: could not resolve {address} to a Weld parcel."
        ), 0.0, 0, 0
    _log_identify_results(parcel)
    account = parcel.account
    address = parcel.account if re.match(r"^R\d{5,9}$", geocoded.street.strip(), re.IGNORECASE) else address

    # Output dir + overview store. Initialized here so every later phase can
    # read/write it. Overview survives partial runs (crashes after Phase 1
    # leave a valid overview.json with just identify_results).
    dest = make_download_dir(geocoded.county, address, base=tmp_dir)
    ov = Overview(overview_path(tmp_dir, geocoded.county.key(), dest.name))
    ov.merge_section("meta", {
        "county_key": geocoded.county.key(),
        "input_address": geocoded.one_line(),
        "account": account,
        "sop_path": None,  # filled in by the Decision Matrix
        "source_urls": [
            _PORTAL_SEARCH_URL,
            f"{_PROPERTY_REPORT_URL}?account={account}",
        ],
    })
    ov.set_section("identify_results", parcel.to_dict())

    # --- SOP Step 1.6 + 1.7: Property Report (Account Information page) ---
    # The HTTP GET to propertyreport.weld.gov returns every accordion section
    # server-rendered in a single response, so we capture all of them at once
    # rather than driving "Open/Close All Sections" in a browser.
    report_fields = _fetch_report_fields(account)
    for section, values in _group_report_fields(report_fields).items():
        if values:
            ov.set_section(section, values)
    ov.set_section("raw_property_report_fields", report_fields)

    # SOP Step 1.7 Map accordion — render and save the parcel map as PNG.
    map_path = await _capture_map_image(account, dest)
    if map_path:
        ov.merge_section("map", {
            "image_path": str(map_path),
            "iframe_url": _MAP_IFRAME_URL.format(account=account),
        })

    # --- SOP Step 1.7: Document History capture (the "Decision Frame") ---
    # Parse the document history table directly from the property report HTML.
    # An empty result is valid — per the SOP it would route to Path 3C in Phase 2.
    all_docs = _fetch_document_history(account)
    ov.set_section("document_history", [d.to_dict() for d in all_docs])

    # --- Phase 2: Decision Matrix (SOP Phase 2) ---
    report_html = _fetch_property_report_html(account)  # cached
    decision = _decision_matrix(all_docs, report_html)
    ov.set_section("decision_matrix", decision)
    ov.merge_section("meta", {"sop_path": decision["path"]})
    logger.info(
        "Phase 2 (Decision Matrix): Path %s — %s",
        decision["path"], decision["reasoning"][0],
    )

    # --- STOP: Phase 2 complete. Phase 3 (Document Download via Path 3A/B/C) ---
    # --- is intentionally not run yet. Restore by deleting this block.       ---
    logger.info(
        "Phase 2 complete for account %s — stopping before Phase 3. "
        "Overview written to %s", account, ov.path,
    )
    return [], None, 0.0, 0, 0

    # --- Phase 3 (dormant): survey-doc filter + per-path download ---
    survey_docs = _filter_survey_docs(all_docs)
    if not survey_docs:
        types_seen = ", ".join(sorted({d.doc_type for d in all_docs}))
        return [], (
            f"No survey-relevant documents found for {address} "
            f"(account {account}). Document types on record: {types_seen}"
        ), 0.0, 0, 0
    ov.set_section("survey_documents", [d.to_dict() for d in survey_docs])

    logger.info(
        "Survey documents to download: %s",
        ", ".join(f"{d.reception}({d.doc_type})" for d in survey_docs),
    )

    # --- Phase 3: download documents from recorder ---
    s = get_settings()
    saved, cost, in_tok, out_tok = await _download_documents(
        address, survey_docs, doc_filter, dest,
        username=s.weld_recorder_username,
        password=s.weld_recorder_password,
    )

    # Annotate each survey doc with its download status.
    saved_by_reception: dict[str, str] = {}
    for path in saved:
        # Filenames are reception_<id>{_page}.<ext>; pull the reception id.
        m = re.match(r"reception_(\d+)", path.name)
        if m:
            saved_by_reception.setdefault(m.group(1), str(path))
    for d in survey_docs:
        status = "downloaded" if d.reception in saved_by_reception else "failed"
        ov.update_list_item(
            "survey_documents", "reception", d.reception,
            {"download_status": status, "downloaded_to": saved_by_reception.get(d.reception, "")},
        )

    if not saved:
        return [], (
            f"Found {len(survey_docs)} document(s) for {address} but could not download them. "
            f"Reception numbers: {', '.join(d.reception for d in survey_docs)}"
        ), cost, in_tok, out_tok

    return saved, None, cost, in_tok, out_tok
