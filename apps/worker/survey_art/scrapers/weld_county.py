"""Weld County, CO — property records scraper.

Implements the procedure documented in ../../../docs/weld_county_sop.md — read that
doc for the decision tree and the reasoning behind each phase; this docstring is just
an orientation map of where each phase lives in this file.

Workflow
--------
1. Parcel resolve (apps.weld.gov/propertyportal/index.cfm)
   - Direct HTTP POST — no browser. Address / account / parcel / S-T-R / owner search,
     in that priority order. Returns Owner, Account, Parcel, S-T-R (`ParcelInfo`).

2. Property Report (propertyreport.weld.gov/?account=RXXXXXXX)
   - Direct HTTP — no browser, no CAPTCHA.
   - Every accordion section server-renders in one response, including the Document
     History table (reception number, date, type, grantor, grantee) — the "Decision
     Frame" that `_decision_matrix()` routes on.

3. Decision Matrix (`_decision_matrix()`) — routes on Document History contents:
   - `direct`: both a vesting deed and a SURV row present →
     `_select_direct_extraction_targets()`.
   - `alternate_partial`: rows present, missing the deed, the survey, or both →
     `_select_partial_history_targets()`.
   - `alternate_empty`: empty + literal "No documents found." →
     `_select_owner_name_search_targets()`.

   The S/T/R easement/ROW Advanced Search (`_easement_row_search()`) is not
   exclusive to the partial-history route — it runs unconditionally alongside
   whichever route fires, since a recorded ALTA's Schedule B-2 only lists what
   its surveyor happened to cite, not necessarily everything else recorded
   against the section.

4. Recorder Document Download (recording.weld.gov)
   - Documents live at recording.weld.gov/web/web/integration/document/{id}.
   - Gated behind a click-through disclaimer with reCAPTCHA; we inject the
     `disclaimerAccepted=true` cookie directly instead of solving it.
   - Requires WELD_RECORDER_USERNAME/PASSWORD — anonymous viewing returns a
     "must be a registered user" stub.
   - Every downloaded document — not just the ALTA — is read for the other
     documents it cites (`id_extraction.py`, `_expand_cross_references()`),
     which fetches those too and repeats until nothing new turns up.

Key Notes
---------
- Reception numbers are the canonical lookup key in the Weld recorder system.
- propertyreport.weld.gov is CAPTCHA-free and returns complete doc history.
- Document type codes: SWD/SWDN = Special Warranty Deed, WD/WDN = Warranty
  Deed, QCD = Quit Claim Deed, SUB = Subdivision Plat, ESMT = Easement,
  DOT = Deed of Trust, LSP = Land Survey Plat, ISP = Improvement Survey Plat.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import httpx
from pydantic import ValidationError

from survey_art.county_sites import SUPPORTED_COUNTIES
from survey_art.doc_classify import CATEGORIES, classify, reception_sort_key
from survey_art.document_filter import DEFAULT_FILTER, DocumentFilter
from survey_art.download import download_dir as make_download_dir
from survey_art.geocode import GeocodedAddress
from survey_art.id_extraction import IdExtraction, cache_fingerprint, extract_document_ids
from survey_art.settings import get_settings
from survey_shared import jobs

logger = logging.getLogger(__name__)
# Plain-language, step-by-step narration for the non-technical end user (streamed
# to the UI's Logs tab). Separate from `logger` above, which carries the detailed
# SOP/phase diagnostics developers need — narration only ever adds new messages,
# it never replaces those.
narration = logging.getLogger("survey_art.narration")

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
    "SUB",  # Subdivision Plat
    "LSP",  # Land Survey Plat
    "ISP",  # Improvement Survey Plat
    "ILC",  # Improvement Location Certificate
    "ALTA",  # ALTA/NSPS Survey
    "PLAT",  # Generic plat
    "AMDPL",  # Amended Plat
    "CORPL",  # Correction Plat
    "VACPL",  # Vacating Plat
    "CONDPL",  # Condominium Plat
    # Deeds (ownership chain)
    "WD",  # Warranty Deed
    "WDN",  # Warranty Deed (Non-Money)
    "SWD",  # Special Warranty Deed
    "SWDN",  # Special Warranty Deed (Non-Money)
    "QCD",  # Quit Claim Deed
    "QCN",  # Quit Claim Deed (Non-Money)
    "QCDN",  # Quit Claim Deed (Non-Money, alternate code)
    "PRD",  # Personal Representative Deed
    "TRD",  # Trustee Deed
    "GD",  # General Deed
    # Easements & ROW
    "ESMT",  # Easement
    "ROW",  # Right of Way
    "ROWE",  # Right of Way Easement
    "AE",  # Access Easement
    "UE",  # Utility Easement
    "DE",  # Drainage Easement
    "CE",  # Conservation Easement
    # Government / public records
    "RES",  # Resolution
    "ORD",  # Ordinance
    "COD",  # Certificate of Dedication
    "NOC",  # Notice of Condemnation
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
    the partial-history and owner-name search routes.

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
        rows.append(
            ParcelInfo(
                account=account,
                parcel_id=labels.get("parcel", ""),
                owner=labels.get("owner", ""),
                address=labels.get("location", "").strip(", "),
                subdivision=labels.get("subdivision", ""),
                source="http",
            )
        )
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


def _pick_best_row(rows: list[ParcelInfo], query: str, query_type: str) -> ParcelInfo | None:
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
        len(rows),
        query,
        query_type,
    )
    return rows[0]


def _get_parcel_info_http(query: str, query_type: str = "address") -> ParcelInfo | None:
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
            logger.info("Opening Property Portal map")
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
        logger.info("Clicking %r search", button_label)
        await page.get_by_role("button", name=re.compile(button_label, re.I)).first.click()
        await page.locator("input:visible").first.fill(query)
        await page.keyboard.press("Enter")
        return
    if query_type == "address":
        logger.info("Address search for %r", query)
        await page.get_by_role("button", name=re.compile(r"^Address$", re.I)).first.click()
        await page.locator("input:visible").first.fill(query.split(",")[0].strip())
        await page.keyboard.press("Enter")
        return
    if str_input:
        section, township, range_ = (p.strip() for p in str_input.split(",", 2))
        logger.info("Section/Township/Range search %s/%s/%s", section, township, range_)
        await page.get_by_role("button", name=re.compile(r"S-?T-?R", re.I)).first.click()
        inputs = page.locator("input:visible")
        await inputs.nth(0).fill(section)
        await inputs.nth(1).fill(township)
        await inputs.nth(2).fill(range_)
        await page.keyboard.press("Enter")
        return
    if owner_input or query_type == "owner":
        owner = owner_input or query
        logger.info("Owner search for %r", owner)
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
    text = (
        await page.evaluate(
            """() => {
            const m = Array.from(document.querySelectorAll('*'))
              .find(n => n.textContent && n.textContent.startsWith('Identify'));
            return m ? m.closest('[class*="results"], [class*="Results"], aside, section')?.innerText
                     : document.body.innerText;
        }"""
        )
        or ""
    )

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
        records.append(
            _DocRecord(
                reception=link_m.group(2).strip(),
                rec_date=_cell("Rec Date", row_html),
                doc_type=_cell("Type", row_html).upper(),
                grantor=_cell("Grantor", row_html),
                grantee=_cell("Grantee", row_html),
                doc_fee=_cell("Doc Fee", row_html),
                sale_date=_cell("Sale Date", row_html),
                sale_price=_cell("Sale Price", row_html),
                url=link_m.group(1).strip(),
            )
        )

    logger.info("Property report for %s: found %d document records", account, len(records))
    return records


# Field -> accordion section grouping, based on the SOP Step 1.6 accordion layout.
# Keys are snake_case (matching _extract_data_labels output). Any field not in
# this map gets placed in `other`.
#
# Identity fields (account, parcel, subdivision, section/township/range) are
# deliberately NOT listed here even though the property report HTML repeats
# them next to Account Information — they're already captured once, in SOP
# form, in `identify_results` (see ParcelInfo.to_dict()). Same for the four
# valuation fields, which live only under `valuation_information` — Account
# Information doesn't need its own copy.
_REPORT_SECTION_MAP: dict[str, list[str]] = {
    "account_information": [
        "account_type",
        "tax_year",
        "buildings",
        "legal",
        "block",
        "lot",
        "land_economic_area",
        "property_address",
        "property_city",
    ],
    "owners": ["owner_name", "address"],
    "land_information": ["code", "description", "acres", "land_sqft"],
    "valuation_information": [
        "actual_value",
        "assessed_value",
        "local_govt_assessed_value",
        "school_assessed_value",
    ],
    "tax_authorities": [
        "tax_area",
        "district_id",
        "district_name",
        "current_mill_levy",
        "school_mill_levy",
        "taxes",
    ],
}

# Renamed when placed into a grouped section, so the UI shows a clear label
# instead of the terse raw data-label key.
_FIELD_RENAMES: dict[str, str] = {"legal": "legal_description"}

# Document-history columns — excluded from the `other` bucket because they're
# captured separately as structured rows in document_history[].
_DOC_HISTORY_LABELS: set[str] = {
    "reception",
    "rec_date",
    "type",
    "grantor",
    "grantee",
    "doc_fee",
    "sale_date",
    "sale_price",
}

# Identity columns the property report repeats next to Account Information —
# already captured once (in SOP form) in `identify_results`. Dropped here
# rather than falling through to `other`, so they don't show up twice.
_IDENTIFY_RESULTS_LABELS: set[str] = {
    "account",
    "parcel",
    "subdivision",
    "section",
    "township",
    "range",
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
                grouped[section][_FIELD_RENAMES.get(label, label)] = fields[label]
                placed.add(label)
    for label, value in fields.items():
        if label in placed or label in _DOC_HISTORY_LABELS or label in _IDENTIFY_RESULTS_LABELS:
            continue
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
            # Hide the ESRI zoom +/- widget overlay — it's a fixed UI control, not
            # part of the map content, and just clutters the corner of the image.
            await page.evaluate(
                "document.querySelectorAll('.esri-zoom').forEach(el => el.style.display = 'none')"
            )
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
    logger.info("Document filter: %d/%d records match survey types", len(filtered), len(records))
    return filtered


# ---------------------------------------------------------------------------
# Decision Matrix — routes a parcel to a research strategy based on what its
# Document History contains (see docs/weld_county_sop.md for the full spec).
# ---------------------------------------------------------------------------

# Vesting deeds per the SOP doc-type cheat sheet. These are the "conveyance"
# rows that establish chain of title. The non-money variants (WDN, SWDN,
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


def _date_sort_key(date_str: str) -> tuple[int, int, int]:
    """Parse a leading MM/DD/YYYY or MM-DD-YYYY date for chronological sort.

    Document History rows use 'MM-DD-YYYY'; Advanced Search result rows use
    'MM/DD/YYYY HH:MM AM/PM' — only the date prefix matters for "most
    recent" comparisons, and lexicographic sort doesn't work for either.
    """
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", date_str or "")
    if not m:
        return (0, 0, 0)
    mm, dd, yyyy = m.groups()
    return (int(yyyy), int(mm), int(dd))


def _decision_matrix(records: list[_DocRecord], html: str) -> dict:
    """Evaluate Document History against the Decision Matrix's three conditions.

    Apply IF/THEN/ELSE in order; stop at first match. Tie-breakers:
      - If the direct-extraction and partial-history conditions both
        technically match, prefer direct extraction (partial history is
        reachable as a fallback when direct extraction fails).
      - Never route to owner-name search unless the literal
        'No documents found.' string is present. A blank section without
        that text is a UI error.

    Returns a dict shaped for direct serialization to overview.json:

        {
          "path": "direct" | "alternate_partial" | "alternate_empty" | "unroutable",
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

    most_recent_survey = max(surveys, key=lambda r: _date_sort_key(r.rec_date)) if surveys else None
    most_recent_vesting = (
        max(vesting, key=lambda r: _date_sort_key(r.rec_date)) if vesting else None
    )

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

    # Direct extraction — both a vesting deed AND at least one SURV row.
    if vesting and surveys:
        reasoning = [
            f"Direct extraction matched: {len(records)} row(s) including "
            f"{len(vesting)} vesting deed(s) [{', '.join(sorted({v.doc_type for v in vesting}))}] "
            f"and {len(surveys)} SURV row(s).",
            f"Most recent SURV: reception {most_recent_survey.reception} "
            f"({most_recent_survey.rec_date}). Expected to correspond to a recorded ALTA.",
            f"Most recent vesting deed: reception {most_recent_vesting.reception} "
            f"({most_recent_vesting.doc_type}, {most_recent_vesting.rec_date}).",
            "→ Route to direct extraction (download the ALTA + vesting deed directly).",
        ]
        return {"path": "direct", "reasoning": reasoning, **base}

    # Partial history — rows present, but missing either survey or vesting deed.
    if records and (bool(vesting) ^ bool(surveys) or (not vesting and not surveys)):
        missing = "vesting deed" if surveys else "SURV row"
        if not vesting and not surveys:
            missing = "vesting deed AND SURV row"
        reasoning = [
            f"Partial history matched: {len(records)} row(s) present but no {missing}.",
            f"Doc types on record: {', '.join(sorted({r.doc_type for r in records}))}.",
            "→ Route to partial history search "
            "(S/T/R Advanced Search for easements/ROW, plus any vesting deed on file).",
        ]
        return {"path": "alternate_partial", "reasoning": reasoning, **base}

    # Empty history — AND the literal "No documents found." text is shown.
    if not records and no_docs:
        reasoning = [
            "Empty history matched: Document History is empty AND "
            f"'{_NO_DOCS_MESSAGE}' is present below the section header.",
            "→ Route to owner-name search (owner-name search + S/T/R-driven exemption packet).",
        ]
        return {"path": "alternate_empty", "reasoning": reasoning, **base}

    # Tie-breaker: 0 rows + no message → UI error, not empty history.
    if not records and not no_docs:
        reasoning = [
            "UNROUTABLE: Document History returned 0 rows but the literal "
            f"'{_NO_DOCS_MESSAGE}' string was NOT present in the response.",
            "This is treated as a UI / rendering error rather than empty "
            "history. Retry the run before routing.",
        ]
        return {"path": "unroutable", "reasoning": reasoning, **base}

    # Defensive fallthrough — shouldn't happen given the conditions above.
    return {
        "path": "unroutable",
        "reasoning": [
            "UNROUTABLE: no Decision Matrix condition matched. "
            "This indicates a logic bug — investigate."
        ],
        **base,
    }


_RECORDER_LOGIN_URL = "https://recording.weld.gov/web/user/login"

_BINARY_CONTENT_TYPES = {
    "image/tiff",
    "image/x-tiff",
    "image/tif",
    "image/jpeg",
    "image/png",
    "image/gif",
    "application/pdf",
    "application/octet-stream",
}
_DOC_EXTENSIONS = (".tif", ".tiff", ".pdf", ".jpg", ".jpeg", ".png")

# Pacing for the document viewer — see the retry comment in _download_documents().
# Measured: 90 back-to-back fetches got 38 documents; the same receptions all
# succeeded when requested on their own.
_DOC_FETCH_ATTEMPTS = 4
_DOC_FETCH_PAUSE_S = 2.0

# APPLICATION_MODE=demo: how many of the ALTA's Schedule B-2 referenced documents
# to actually fetch. Enough to show the feature working without the ~30 minutes a
# full ~90-document ALTA takes.
_DEMO_EXCEPTION_LIMIT = 10

# The disclaimer page's "I Accept" button is gated by a Google reCAPTCHA that headless
# Chromium can't pass, but the document viewer only checks for this cookie. The server
# hands out its own `disclaimerAccepted=false` on some responses, so this gets
# re-asserted (replacing, not appending) whenever a fetch has to be retried.
_DISCLAIMER_COOKIE = {
    "name": "disclaimerAccepted",
    "value": "true",
    "domain": "recording.weld.gov",
    "path": "/",
}


async def _recorder_login(ctx, username: str, password: str) -> bool:
    """Authenticate `ctx` against recording.weld.gov. Returns success.

    The login page is a jQuery Mobile fragment — loading `/web/user/login`
    directly leaves jQuery undefined, so clicking the in-page submit button (or
    calling the JS handler) doesn't work. The button's JS handler ultimately
    POSTs the serialized form to `/web/user/login` and expects a JSON
    `{success, message}` response, so we just do that POST directly through
    Playwright's request context. The response cookies are shared with
    subsequent page navigations.

    Called again mid-run when the viewer stops serving documents — see
    `_download_documents()`.
    """
    try:
        resp = await ctx.request.post(
            _RECORDER_LOGIN_URL,
            form={"field_UserId": username, "field_Password": password},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        body = await resp.text()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            # A non-JSON reply is the login *page*, which is a whole HTML document —
            # every log line here is streamed to the user's Logs tab, so collapse it
            # to something readable instead of pasting the page in.
            payload = {"success": False, "message": f"non-JSON reply ({len(body)} bytes)"}
        if not payload.get("success"):
            logger.error(
                "Login failed (HTTP %s): %s. Check WELD_RECORDER_USERNAME/PASSWORD "
                "in .env, or register at recording.weld.gov.",
                resp.status,
                payload.get("message", "(no message)"),
            )
            return False
        logger.info("Logged in as %s", username)
        return True
    except Exception as exc:
        logger.warning("Login POST failed: %s", exc)
        return False


async def _fetch_document(
    ctx,
    role: str,
    doc: _DocRecord,
    dest_dir: Path,
    cookie_lock: asyncio.Lock,
) -> tuple[str, _DocRecord, list[Path]]:
    """Fetch one recorder document. Returns `(role, doc, [path])`, or an empty
    path list if every attempt failed.

    Pace and retry. The Schedule B-2 exception walk turned this from "2
    documents" into "one per exception" — ~90 for a commercial ALTA — and
    occasionally the site serves a viewer page with no `#printCustom` button.
    That never means the document is missing; the same reception fetches
    cleanly moments later. So every non-success retries after a growing pause,
    re-asserting the disclaimer cookie (see below) rather than re-authenticating.

    Measured over the 80 documents ALTA 4571638 cites: 80/80, with only 3
    single-attempt retries. Two earlier versions scored 44/80 and 57/80 — the
    first because it treated any HTTP response as final and never retried, the
    second because it re-logged-in between attempts.
    """
    doc_saved: list[Path] = []
    for attempt in range(_DOC_FETCH_ATTEMPTS):
        # Pace every attempt, not just the retries. Those 80/80 measurements
        # were taken with a paused *serial* loop, and what the county's server
        # reacts to is the request rate — so each concurrent worker keeps
        # pacing itself exactly as the serial version did, and concurrency
        # (`weld_download_concurrency`) is what provides the speedup instead.
        await asyncio.sleep(_DOC_FETCH_PAUSE_S * (attempt + 1))
        if attempt:
            # Re-assert the disclaimer cookie rather than re-authenticating.
            # Hitting the login endpoint again is actively harmful: when the
            # session is still valid it replies with the login *page* (HTTP 200,
            # HTML, no JSON `success`), and that response sets
            # `disclaimerAccepted=false` alongside the injected `true`. The
            # server reads the false one, serves the disclaimer instead of the
            # document, and every following fetch loses its download button —
            # a re-login "fix" turned a partial failure into a total one.
            #
            # Cookies are context-wide, so this clear/re-add pair is shared with
            # every fetch running right now and has to be atomic: without the
            # lock a sibling can land in the window where the cookie is missing
            # and get served the disclaimer instead of its document.
            async with cookie_lock:
                await ctx.clear_cookies(name="disclaimerAccepted")
                await ctx.add_cookies([_DISCLAIMER_COOKIE])
        # Nothing below may escape: these run under one `asyncio.gather`, so a
        # raise here would cancel every other document in flight (mid-write, in
        # the worst case) and skip the browser teardown. A document that can't
        # be fetched returns no paths instead — the caller already treats that
        # as "failed" and carries on with the rest.
        doc_page = None
        try:
            doc_page = await ctx.new_page()
            await doc_page.goto(doc.url, wait_until="domcontentloaded", timeout=30_000)
            # The print button's `data-href` is the only thing this page is
            # opened for, so wait for that button rather than for the network to
            # go idle: the viewer is PDF.js fetching page images over HTTP Range
            # requests, which keeps the network busy long after the button
            # exists. A timeout here isn't fatal — fall through and let the
            # `href` check below produce the real diagnostic.
            try:
                await doc_page.wait_for_selector("#printCustom", timeout=20_000)
            except Exception:
                pass

            href = await doc_page.evaluate(
                "() => { const b = document.getElementById('printCustom');"
                " return b ? b.getAttribute('data-href') : null; }"
            )
            if not href:
                image_div = await doc_page.query_selector("#ImageDiv")
                msg = (await image_div.inner_text()).strip()[:200] if image_div else ""
                logger.warning(
                    "No printCustom button for %s reception %s (attempt %d/%d) — %s",
                    role,
                    doc.reception,
                    attempt + 1,
                    _DOC_FETCH_ATTEMPTS,
                    msg or "(no diagnostic message)",
                )
                continue

            pdf_url = f"https://recording.weld.gov{href}"
            resp = await ctx.request.get(pdf_url)
            body = await resp.body() if resp.status == 200 else b""
            if resp.status != 200:
                logger.warning(
                    "Print endpoint returned HTTP %s for %s reception %s (attempt %d/%d)",
                    resp.status,
                    role,
                    doc.reception,
                    attempt + 1,
                    _DOC_FETCH_ATTEMPTS,
                )
                continue
            if not body or body[:5] != b"%PDF-":
                logger.warning(
                    "Print endpoint returned non-PDF body for %s reception %s "
                    "(head=%r, %d bytes, attempt %d/%d)",
                    role,
                    doc.reception,
                    body[:8],
                    len(body),
                    attempt + 1,
                    _DOC_FETCH_ATTEMPTS,
                )
                continue

            dst = dest_dir / f"{role}_{doc.reception}.pdf"
            dst.write_bytes(body)
            doc_saved.append(dst)
            logger.info(
                "Saved %s (%d bytes) for %s reception %s",
                dst.name,
                len(body),
                role,
                doc.reception,
            )
            break
        except Exception as exc:
            logger.warning("Download error for %s reception %s: %s", role, doc.reception, exc)
        finally:
            if doc_page is not None:
                await doc_page.close()

    return role, doc, doc_saved


async def _download_documents(
    address: str,
    targets: list[tuple[str, _DocRecord]],
    doc_filter: DocumentFilter,
    dest_dir: Path,
    username: str = "",
    password: str = "",
) -> tuple[list[tuple[str, _DocRecord, list[Path]]], float, int, int]:
    """Phase 3: download recorder documents via Playwright with disclaimer bypass.

    Accepts a list of `(role, _DocRecord)` tuples. The `role` is used as the
    filename prefix so callers can distinguish ALTA / vesting_deed / exception
    output without re-parsing the doc record. Examples:
      - ("alta", doc)        -> "alta_4571638.tif"
      - ("vesting_deed", doc) -> "vesting_deed_4970002.pdf"
      - ("exception", doc)   -> "exception_1766550_p2.tif"

    The disclaimer page at recording.weld.gov is gated by a Google reCAPTCHA on
    the "I Accept" button. Headless Chromium can't pass reCAPTCHA, so we inject
    the `disclaimerAccepted=true` cookie directly — the document viewer only
    checks for the cookie's presence.

    Returns `(results, cost, in_tokens, out_tokens)` where `results` is a list
    of `(role, doc_record, [saved_paths])` so the caller can map each downloaded
    document back to its semantic role.
    """
    if not targets:
        return [], 0.0, 0, 0

    from playwright.async_api import async_playwright

    dest_dir.mkdir(parents=True, exist_ok=True)

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

        await ctx.add_cookies([_DISCLAIMER_COOKIE])
        setup_page = await ctx.new_page()

        # Step 2: Login if credentials are provided.
        #
        # The login page is a jQuery Mobile fragment — loading `/web/user/login`
        # directly leaves jQuery undefined, so clicking the in-page submit
        # button (or calling the JS handler) doesn't work. The button's JS
        # handler ultimately POSTs the serialized form to `/web/user/login`
        # and expects a JSON `{success, message}` response, so we just do
        # that POST directly through Playwright's request context. The
        # response cookies are shared with subsequent page navigations.
        if username and password:
            if not await _recorder_login(ctx, username, password):
                await browser.close()
                return [], 0.0, 0, 0

        await setup_page.close()

        # Step 3: Download each document as a single complete PDF.
        #
        # Tyler's viewer uses PDF.js with HTTP Range requests, so trying to
        # snoop the network for "the PDF" yields fragmented byte chunks rather
        # than a usable file. The viewer's print toolbar button (`#printCustom`)
        # has a `data-href` pointing at Tyler's native single-file endpoint
        # (`/web/document-image-pdf/.../<reception>-1.pdf?index=1`) which
        # serves the complete multi-page document. We open the viewer just
        # long enough to read that href, then fetch it directly.
        #
        # Fetches run concurrently, bounded by `weld_download_concurrency`, in
        # tabs of this one already-authenticated context — so the session is
        # established once and never re-established mid-run. `gather` preserves
        # input order, so `results` still lines up with `targets`.
        limit = max(1, get_settings().weld_download_concurrency)
        semaphore = asyncio.Semaphore(limit)
        cookie_lock = asyncio.Lock()
        logger.info("Fetching %d document(s), %d at a time", len(targets), limit)

        async def fetch(role: str, doc: _DocRecord):
            async with semaphore:
                return await _fetch_document(ctx, role, doc, dest_dir, cookie_lock)

        results = list(await asyncio.gather(*(fetch(role, doc) for role, doc in targets)))

        await browser.close()

    total_files = sum(len(paths) for _, _, paths in results)
    logger.info("Weld County: saved %d file(s) across %d document(s)", total_files, len(results))
    return results, 0.0, 0, 0


# ---------------------------------------------------------------------------
# Direct Extraction — ALTA + vesting deed already on file (decision path "direct")
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Partial History Search — S/T/R Advanced Search for easements/ROW, plus
# whatever vesting deed is on file (decision path "alternate_partial")
# ---------------------------------------------------------------------------

# Document Types multiselect filter list for the Advanced Search. Every
# variant of EASEMENT / RIGHT OF WAY the SOP enumerates. The Self Service Web
# search UI is an autocomplete input that's awkward to drive headlessly, so
# we let the Advanced Search return ALL rows matching the S/T/R and
# post-filter on the Type column rendered in each result row.
_EASEMENT_ROW_DOC_TYPES = {
    "EASEMENT",
    "EASEMENT & RIGHT OF WAY",
    "EASEMENT DEED",
    "EASEMENT PLAT",
    "EASEMENT RIGHT OF WAY & SURFACE USE AGR",
    "EASEMENT RIGHT OF WAY AND SURFACE USE AGR",
    "EASEMENT & SURFACE USE AGR",
    "GRANT & RELEASE OF EASEMENT",
    "RIGHT OF WAY",
    "RIGHT OF WAY EASEMENT",
    "RIGHT OF WAY AGREEMENT",
    "AMENDED RIGHT OF WAY",
    "R/W AGREEMENT",
    "ROW",
    "RIGHT OF WAY (RW)",
}

_ADVANCED_SEARCH_URL = "https://recording.weld.gov/web/search/DOCSEARCH524S12"


def _matches_easement_filter(doc_type_label: str) -> bool:
    """Loose match: a doc type passes if any easement keyword appears in it."""
    upper = doc_type_label.upper().strip()
    if upper in _EASEMENT_ROW_DOC_TYPES:
        return True
    # The label may carry extra punctuation/spacing; match the canonical
    # tokens conservatively.
    return any(t in upper for t in ("EASEMENT", "RIGHT OF WAY", "R/W", "ROW"))


async def _run_advanced_search(
    page,
    *,
    section: str = "",
    township: str = "",
    range_: str = "",
    subdivision: str = "",
    search_name: str = "",
    start_date: str = "",
    end_date: str = "",
) -> list[dict]:
    """Drive the recorder's Advanced Search UI and return the result rows.

    The Self Service Web's direct HTTP POST to `/web/searchPost/...` returns
    only metadata; the actual results render only when the search is driven
    through the page UI. Each result is parsed into:

        {"reception": str, "doc_type": str, "rec_date": str, "doc_id": str}

    where `doc_id` is Tyler's internal DOC ID (e.g. 'DOC808S1754'). Note this
    returns whatever rows the search criteria match, up to `_RESULT_ROW_CAP` —
    callers post-filter by `doc_type`, and anything that needs the *complete*
    set goes through `_search_all_rows()` instead.

    `search_name` fills 'Search Name as Grantor or Grantee' (the owner-name
    search) — it's a field on this same Advanced Search form
    (`#field_BothNamesID`), not a separate Basic Search page. `start_date` /
    `end_date` are MM/DD/YYYY strings for the Recording Date range.
    """
    await page.goto(_ADVANCED_SEARCH_URL, wait_until="networkidle", timeout=30_000)
    # The form opens with a "Continue session?" dialog if any user state exists.
    # Measured across ~60 consecutive searches on one login it never appeared
    # once, so don't wait long for it.
    try:
        await page.click("button:has-text('Yes - Continue')", timeout=1_500)
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    # **Every search starts by clearing the last one.** Tyler holds the search
    # criteria server-side, not in the form: reloading the page gives you empty
    # inputs while the server still has the previous query, and the next search
    # is silently ANDed with it. Measured — a name search, then a section search
    # on a page whose inputs read Section=32/Township=5/Range=65/Name=(blank):
    #
    #     no reset                   4 rows
    #     click 'Clear Selections' 100 rows
    #
    # That is how job 82f98c14 came to search S32-T5N-R65W three times for a
    # parcel's easements, exemptions and recorded ALTA and get 4 rows each time:
    # the owner's name was still in effect on the server. It poisons in both
    # directions — the section criteria then cut the *next* name search from 30
    # rows to 4 — so a whole run's research quietly narrows to nothing.
    try:
        await page.click(
            "a:has-text('Clear Selections'), button:has-text('Clear Selections')", timeout=5_000
        )
        await page.wait_for_timeout(1_000)
    except Exception as exc:
        logger.warning("Advanced Search: could not clear the previous search: %s", exc)

    # Fill every field, blanking the ones this call doesn't use — belt and
    # braces alongside the clear above, and the only way the form ever shows
    # what was actually searched. Township/range go in as the numeric portion
    # only ("5", not "5N").
    for selector, value in (
        ("#field_PLSSLegalID_DOT_Section", section),
        ("#field_PLSSLegalID_DOT_Township", township.rstrip("NnSs")),
        ("#field_PLSSLegalID_DOT_Range", range_.rstrip("EeWw")),
        ("#field_PlattedLegalID_DOT_Subdivision", subdivision),
        ("#field_BothNamesID", search_name),
        ("#field_RecordingDateID_DOT_StartDate", start_date),
        ("#field_RecordingDateID_DOT_EndDate", end_date),
    ):
        await page.fill(selector, value)

    await page.click("#searchButton")
    await page.wait_for_load_state("networkidle", timeout=30_000)
    await page.wait_for_timeout(2_000)  # results render via AJAX

    rows = await page.evaluate(
        """() => {
            const items = document.querySelectorAll('li.ss-search-row[data-documentid]');
            return Array.from(items).map(li => {
                const docId = li.getAttribute('data-documentid') || '';
                const text = (li.querySelector('h1')?.textContent || '').replace(/\\s+/g, ' ').trim();
                // Header format: "<reception> • <type> • <date>"
                const parts = text.split(/\\s*•\\s*/);
                return {
                    doc_id: docId,
                    reception: (parts[0] || '').trim(),
                    doc_type: (parts[1] || '').trim(),
                    rec_date: (parts[2] || '').trim(),
                };
            });
        }"""
    )
    logger.info(
        "Advanced Search (S=%s T=%s R=%s Sub=%s Name=%s Dates=%s-%s): %d row(s)",
        section,
        township,
        range_,
        subdivision,
        search_name,
        start_date or "*",
        end_date or "*",
        len(rows),
    )
    return rows


# The result list renders at most this many rows for one search. There is no
# paging control, no "load more", and scrolling the list adds nothing — measured
# against S32-T5N-R65W, which stops dead at 100 of its 872 documents. Rows come
# back newest-first, so what a single search silently drops is *all the old
# records* — the 1889 ditch deed, the 1952 highway ROW, the 2019 ROW takes. That
# is the trail a surveyor is actually following.
_RESULT_ROW_CAP = 100

# Weld's recorded documents are certified from Jan 1 1865 (the search form says
# so), so that's the floor of the date sweep below.
_RECORDS_BEGIN = date(1865, 1, 1)


async def _search_all_rows(page, **criteria: str) -> list[dict]:
    """Every row matching `criteria`, not just the first `_RESULT_ROW_CAP`.

    The form has no pagination but it does have a Recording Date range, so a
    search that comes back at the cap is split in half by date and each half
    re-run, recursively, until every window is under it. Rows are deduplicated
    by reception number because a document recorded on a boundary date can come
    back from both halves.

    Measured on S32-T5N-R65W: 29 searches, 872 documents, back to 1886 — against
    100 documents and nothing older than 2022 for the single unbounded search.
    Cost is one extra search per split, and splits only happen where the records
    are actually dense, so a quiet section stays a handful of queries.
    """

    async def sweep(start: date, end: date) -> list[dict]:
        rows = await _run_advanced_search(
            page,
            **criteria,
            start_date=start.strftime("%m/%d/%Y"),
            end_date=end.strftime("%m/%d/%Y"),
        )
        if len(rows) < _RESULT_ROW_CAP or start >= end:
            # A single day still at the cap is as far as the form can narrow;
            # take what it gives rather than looping forever.
            return rows
        mid = start + (end - start) / 2
        return await sweep(start, mid) + await sweep(mid + timedelta(days=1), end)

    by_reception: dict[str, dict] = {}
    for row in await sweep(_RECORDS_BEGIN, date.today()):
        by_reception.setdefault(row["reception"], row)
    logger.info(
        "Date-swept search (%s): %d distinct document(s)",
        ", ".join(f"{k}={v}" for k, v in criteria.items() if v) or "no criteria",
        len(by_reception),
    )
    return list(by_reception.values())


@asynccontextmanager
async def _recorder_search_session(username: str, password: str):
    """Open an authenticated Playwright page for driving Advanced Search.

    Shared by the partial-history search, the owner-name search, and the
    always-on easement/ROW scan — Advanced Search returns no rows for
    anonymous sessions. Yields `None` (instead of raising) when credentials
    are missing or login fails, so callers can treat "no session" as "skip
    this research route" without crashing the run.
    """
    import os

    from playwright.async_api import async_playwright

    if not (username and password):
        logger.warning(
            "Advanced Search: WELD_RECORDER_USERNAME/PASSWORD not set — "
            "skipping (Advanced Search needs an authenticated session)."
        )
        yield None
        return

    headed = os.environ.get("WELD_HEADED", "").lower() in ("1", "true", "yes")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
            slow_mo=400 if headed else 0,
        )
        ctx = await browser.new_context(user_agent=_HTTP_HEADERS["User-Agent"])
        await ctx.add_cookies([_DISCLAIMER_COOKIE])
        try:
            resp = await ctx.request.post(
                _RECORDER_LOGIN_URL,
                form={"field_UserId": username, "field_Password": password},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            payload = json.loads(await resp.text())
            if not payload.get("success"):
                logger.error("Advanced Search login failed: %s", payload.get("message", "(no msg)"))
                await browser.close()
                yield None
                return
        except Exception as exc:
            logger.warning("Advanced Search login error: %s", exc)
            await browser.close()
            yield None
            return

        page = await ctx.new_page()
        try:
            yield page
        finally:
            await browser.close()


async def _easement_row_search(
    page,
    parcel: ParcelInfo,
    seen_receptions: set[str],
) -> list[tuple[str, _DocRecord]]:
    """S/T/R (+ subdivision) Advanced Search for easements and rights-of-way.

    Shared by the partial-history search and the owner-name search, and run
    unconditionally alongside direct extraction too (see `scrape()`) — the
    SOP treats this research as required regardless of whether Document
    History already yielded an ALTA. Mutates `seen_receptions` in place as
    it selects targets.
    """
    targets: list[tuple[str, _DocRecord]] = []
    if not (parcel.section and parcel.township and parcel.range_):
        logger.warning(
            "Easement/ROW search: parcel S/T/R is incomplete (%s/%s/%s) — skipping.",
            parcel.section,
            parcel.township,
            parcel.range_,
        )
        return targets

    section = parcel.section.lstrip("0") or parcel.section
    township = parcel.township  # _run_advanced_search strips N/S
    range_ = parcel.range_  # and E/W respectively

    # Date-swept: the type filter below runs on rows the form already returned,
    # so a capped search silently filters the newest 100 documents rather than
    # every easement ever recorded against the section.
    rows = await _search_all_rows(page, section=section, township=township, range_=range_)
    # If a Subdivision is on file, repeat with Platted Legal.
    if parcel.subdivision:
        sub_rows = await _search_all_rows(page, subdivision=parcel.subdivision)
        # Deduplicate by reception across the two searches.
        by_reception_in_results = {r["reception"]: r for r in rows}
        for r in sub_rows:
            by_reception_in_results.setdefault(r["reception"], r)
        rows = list(by_reception_in_results.values())

    for r in rows:
        reception = r["reception"]
        if not reception or reception in seen_receptions:
            continue
        if not _matches_easement_filter(r["doc_type"]):
            continue
        seen_receptions.add(reception)
        # Synthesize a _DocRecord pointing at the integration URL so the
        # existing downloader can fetch it via #printCustom.
        targets.append(
            (
                "easement_or_row",
                _DocRecord(
                    reception=reception,
                    rec_date=r["rec_date"],
                    doc_type=r["doc_type"],
                    grantor="",
                    grantee="",
                    url=f"https://recording.weld.gov/web/web/integration/document/{reception}",
                ),
            )
        )
    return targets


def _section_scan_rank(doc_type_label: str) -> int:
    """Survey relevance of one section-scan row, lowest first.

    Only consulted when the section has more documents than
    `weld_section_download_limit` — it decides what a run gives up first, not
    what it collects. A section's paper is mostly financing: 289 of
    S32-T5N-R65W's 872 documents are deeds of trust, and none of them tell a
    surveyor anything about a boundary.
    """
    upper = doc_type_label.upper().strip()
    if any(t in upper for t in ("DEED OF TRUST", "TRUST DEED", "MORTGAGE", "RELEASE")):
        return 4
    if _matches_survey_filter(upper) or "PLAT" in upper or _matches_exemption_filter(upper):
        return 0
    if _matches_easement_filter(upper):
        return 1
    if _matches_vesting_deed_label(upper):
        return 2
    return 3


async def _section_township_range_search(
    page,
    parcel: ParcelInfo,
    seen_receptions: set[str],
) -> tuple[list[tuple[str, _DocRecord]], list[dict]]:
    """S/T/R Advanced Search for every other document recorded against this
    parcel's section — not just easements/ROW (see `_easement_row_search`).

    Run unconditionally alongside every research route (see `scrape()`), per
    the SOP: a surveyor wants to know about anything else recorded in this
    section, not only what the property's own Document History or ALTA
    happened to cite. Mutates `seen_receptions` in place.

    Returns `(targets, every_row_found)`. The search is date-swept, so the
    second element is the section's whole index — 872 documents for
    S32-T5N-R65W — and goes into overview.json whether or not each one is
    downloaded. Targets are ordered by survey relevance and cut to
    `weld_section_download_limit`, so what the cap drops is the least useful
    end of the list (deeds of trust, 289 of that section's 872) rather than
    everything recorded before 2022, which is what the old unbounded search
    dropped.
    """
    if not (parcel.section and parcel.township and parcel.range_):
        logger.warning(
            "Section/Township/Range search: parcel S/T/R is incomplete (%s/%s/%s) — skipping.",
            parcel.section,
            parcel.township,
            parcel.range_,
        )
        return [], []

    section = parcel.section.lstrip("0") or parcel.section
    rows = await _search_all_rows(
        page, section=section, township=parcel.township, range_=parcel.range_
    )

    fresh = [r for r in rows if r["reception"] and r["reception"] not in seen_receptions]
    fresh.sort(key=lambda r: _date_sort_key(r["rec_date"]), reverse=True)  # newest first
    fresh.sort(key=lambda r: _section_scan_rank(r["doc_type"]))  # stable: rank, then date
    limit = get_settings().weld_section_download_limit
    if limit and len(fresh) > limit:
        logger.info(
            "Section scan: %d document(s) found, downloading the %d most survey-relevant "
            "(raise WELD_SECTION_DOWNLOAD_LIMIT for more).",
            len(fresh),
            limit,
        )
        fresh = fresh[:limit]

    targets: list[tuple[str, _DocRecord]] = []
    for r in fresh:
        seen_receptions.add(r["reception"])
        targets.append(("section_township_range_search", _row_to_record(r)))
    return targets, rows


def _select_direct_extraction_targets(
    decision: dict, all_docs: list[_DocRecord]
) -> list[tuple[str, _DocRecord]]:
    """Pick the documents to download for the direct-extraction route.

    Returns role-tagged docs in download order:
      - ("alta", most_recent_survey)
      - ("vesting_deed", most_recent_deed)

    Schedule B-2 exception references are handled separately by
    `_select_schedule_b2_exception_targets`, once the ALTA is on disk and has been
    read for the IDs it cites — see `id_extraction.py`.
    """
    targets: list[tuple[str, _DocRecord]] = []

    survey_dict = decision.get("most_recent_survey")
    deed_dict = decision.get("most_recent_vesting_deed")

    # Re-hydrate from the in-memory _DocRecord list so we have the dataclass,
    # not just the dict copy stored in the decision matrix.
    by_reception = {d.reception: d for d in all_docs}

    if survey_dict and survey_dict.get("reception") in by_reception:
        targets.append(("alta", by_reception[survey_dict["reception"]]))
    if deed_dict and deed_dict.get("reception") in by_reception:
        targets.append(("vesting_deed", by_reception[deed_dict["reception"]]))

    return targets


def _select_schedule_b2_exception_targets(
    extraction: IdExtraction, known_receptions: set[str]
) -> list[tuple[str, _DocRecord]]:
    """Turn one document's Schedule B-2-style referenced IDs into download targets.

    Only reception numbers become targets: the recorder's integration URL takes
    a document number and nothing else, so the other formats a document cites
    (book/page, ordinance numbers) are recorded in overview.json's
    `extracted_ids` for the surveyor but can't be auto-fetched today.

    `known_receptions` excludes documents already downloaded or queued this run
    so they aren't re-fetched under the "exception" role if a document happens
    to cite its own reception number, or one another document already found.

    Under `APPLICATION_MODE=demo` only the first `_DEMO_EXCEPTION_LIMIT` targets
    are returned — a single ALTA can cite ~90 documents on its own, which is a
    ~30 minute run.
    """
    targets: list[tuple[str, _DocRecord]] = []
    seen = set(known_receptions)
    for item in extraction.ids:
        if item.id_type != "reception_number" or item.id in seen:
            continue
        seen.add(item.id)
        targets.append(
            (
                "exception",
                _DocRecord(
                    reception=item.id,
                    rec_date="",
                    doc_type=item.context,
                    grantor="",
                    grantee="",
                    url=f"https://recording.weld.gov/web/web/integration/document/{item.id}",
                ),
            )
        )
    if get_settings().application_mode.lower() == "demo" and len(targets) > _DEMO_EXCEPTION_LIMIT:
        narration.info(
            f"Demo mode: downloading {_DEMO_EXCEPTION_LIMIT} of the "
            f"{len(targets)} referenced documents."
        )
        return targets[:_DEMO_EXCEPTION_LIMIT]
    return targets


# Safety backstop for the cross-reference walk below. Real recorder data is a
# finite graph and `extracted`/`known_receptions` already stop a document from
# being read or fetched twice, so this only guards the pathological case (a
# large agricultural owner's paper trail) from turning into an unbounded
# number of Bedrock calls and downloads on one property.
_MAX_CROSS_REFERENCE_DOCS = 150

# How many hops from a directly-fetched document (the ALTA, the vesting deed)
# we'll still chase citations from. Depth 1 = what the ALTA/deed itself cites
# (the actual Schedule B-2 exceptions). Depth 2 = what *those* documents cite.
# Beyond that, a document's own citations are no longer reliably "about this
# property" — an easement citing a prior deed's own unrelated paper trail is
# noise, not part of this parcel's chain — so relatedness is approximated by
# distance rather than inspecting content (which recorder docs don't carry
# consistently enough to judge). Already-fetched documents are still read for
# `extracted_ids` either way; this only stops *further* downloads.
_MAX_CROSS_REFERENCE_DEPTH = 3


def _extract_cited_ids(reception: str, path: Path) -> IdExtraction:
    """Read one document for the documents it cites, reusing a previous run's
    answer when there is one.

    Reading a recorder PDF is the most expensive thing a run does — none of them
    carry a text layer, so each takes the Bedrock vision path — and the answer
    never changes, because a recorded document is immutable once filed. So the
    same reception number costs the model once, ever, rather than once per run:
    a re-run of the same property is close to free, and a different parcel in
    the same section reuses every easement and plat the section-wide search
    returns for both.

    Runs on a worker thread (see `_expand_cross_references`) — both the S3 round
    trip and `extract_document_ids` block.
    """
    fingerprint = cache_fingerprint()
    cached = jobs.get_cached_extraction(fingerprint, reception)
    if cached is not None:
        try:
            # Deliberately not carrying the original token counts over: this run
            # didn't spend them, and the cost breakdown reads these fields.
            return IdExtraction(ids=cached.get("ids", []), source="cache")
        except ValidationError as exc:
            logger.warning("Ignoring malformed cached extraction for %s: %s", reception, exc)

    result = extract_document_ids(path)
    if result.source != "none":
        jobs.put_cached_extraction(fingerprint, reception, {"ids": result.to_metadata()})
    return result


async def _expand_cross_references(
    address: str,
    doc_filter: DocumentFilter,
    dest: Path,
    ov,
    initial_results: list[tuple[str, _DocRecord, list[Path]]],
    known_receptions: set[str],
    username: str,
    password: str,
) -> tuple[list[tuple[str, _DocRecord, list[Path]]], int, int]:
    """Read every downloaded document for the other documents it cites, fetch
    those too, and repeat until nothing new turns up.

    Every document a surveyor pulls — not just the ALTA — can cite exhibits,
    prior deeds, or "excepting" clauses that point at documents outside this
    parcel's own Document History. `known_receptions` (mutated in place) is
    the whole run's set of receptions already downloaded or queued, so
    nothing is fetched twice; a separate `extracted` set tracks which
    documents have already been read for citations, so a document two other
    documents both cite only goes through `extract_document_ids()` (and its
    Bedrock fallback) once. `extract_document_ids()` already tries the free
    text-layer path before Bedrock, so this only pays for a vision call on
    the documents that are actually scanned images.

    `extracted_ids` is written to `ov` once per level processed, as a single
    flat list (the shape the frontend already renders as a table) — cheap
    because nothing is duplicated per property, and a crash mid-walk still
    leaves everything found so far on disk.

    The walk runs a whole depth level at a time rather than one document at a
    time, which is what makes it finish in minutes instead of an hour:

    * every document at a level is read concurrently, and each read goes to a
      worker thread, so a level costs about as long as its slowest document
      instead of the sum of all of them (`extract_document_ids` blocks on both
      PDF decoding and Bedrock, so calling it inline would also stall the event
      loop and every download sharing it);
    * a level's citations are fetched in one `_download_documents()` call, which
      opens a browser and logs in once per call — so one login per level rather
      than one per citing document.
    """
    extracted: set[str] = set()
    # Seeded from what's already on file so a second walk (the section scan's
    # surveys, further down `scrape()`) adds to the table instead of replacing it.
    extracted_ids: list[dict] = list(ov.get("extracted_ids", []))
    new_results: list[tuple[str, _DocRecord, list[Path]]] = []
    level = list(initial_results)
    depth = 0
    discovered = 0
    in_tok = out_tok = 0

    while level:
        # Only the first saved file per document: one that came down as separate
        # per-page images (rather than one merged PDF) only gets its first page
        # read — the same simplification the ALTA-only walk made before.
        batch = [
            (role, doc, paths)
            for role, doc, paths in level
            if paths and doc.reception not in extracted
        ]
        if not batch:
            break
        extracted.update(doc.reception for _, doc, _ in batch)

        extractions = await asyncio.gather(
            *(
                asyncio.to_thread(_extract_cited_ids, doc.reception, paths[0])
                for _, doc, paths in batch
            )
        )

        next_targets: list[tuple[str, _DocRecord]] = []
        for (_role, doc, _paths), extraction in zip(batch, extractions, strict=True):
            in_tok += extraction.input_tokens
            out_tok += extraction.output_tokens
            if extraction.ids:
                extracted_ids.extend(
                    {**row, "source_reception": doc.reception, "source_doc_type": doc.doc_type}
                    for row in extraction.to_metadata()
                )
                ov.set_section("extracted_ids", extracted_ids)

            if discovered >= _MAX_CROSS_REFERENCE_DOCS:
                continue  # keep reading the level for citations, just stop fetching more
            if depth >= _MAX_CROSS_REFERENCE_DEPTH:
                continue

            found = _select_schedule_b2_exception_targets(extraction, known_receptions)
            if not found:
                continue
            if discovered + len(found) > _MAX_CROSS_REFERENCE_DOCS:
                found = found[: _MAX_CROSS_REFERENCE_DOCS - discovered]
                narration.info(
                    "Reached the cross-reference safety limit — stopping further lookups."
                )
            discovered += len(found)
            known_receptions.update(target_doc.reception for _, target_doc in found)
            narration.info(
                f"{doc.doc_type or 'A downloaded document'} references "
                f"{len(found)} other recorded document(s) — downloading them now..."
            )
            next_targets += found

        if depth >= _MAX_CROSS_REFERENCE_DEPTH:
            narration.info(
                "Reached the cross-reference depth limit — no longer chasing "
                "citations from citations."
            )
            ov.set_section("limits", {"cross_reference_depth_limit": True})
            break
        if not next_targets:
            break

        downloaded, _dl_cost, dl_in, dl_out = await _download_documents(
            address, next_targets, doc_filter, dest, username=username, password=password
        )
        in_tok += dl_in
        out_tok += dl_out
        new_results.extend(downloaded)
        level = downloaded
        depth += 1

    return new_results, in_tok, out_tok


async def _select_partial_history_targets(
    decision: dict,
    all_docs: list[_DocRecord],
    parcel: ParcelInfo,
    username: str = "",
    password: str = "",
) -> list[tuple[str, _DocRecord]]:
    """Pick targets for the partial-history route (decision path "alternate_partial").

    Returns role-tagged docs in download order:
      - ("vesting_deed", most_recent_vesting)   if a vesting deed is present
      - ("easement_or_row", row)                for each easement / ROW
                                                matching the parcel's S/T/R
                                                (and subdivision if known)

    Requires authenticated session to drive the Advanced Search UI — caller
    must supply credentials. Cross-reference harvesting from a vesting deed's
    legal description (the "Excluding portions conveyed in Deed recorded ..."
    clauses that cite prior receptions) is not implemented: Tyler PDFs are
    scanned images, so extracting cited receptions would require OCR or a
    vision LLM. Without it we miss references that aren't already
    discoverable via S/T/R Advanced Search.
    """
    targets: list[tuple[str, _DocRecord]] = []
    by_reception = {d.reception: d for d in all_docs}

    # Most-recent vesting deed (if present in Document History).
    deed_dict = decision.get("most_recent_vesting_deed")
    if deed_dict and deed_dict.get("reception") in by_reception:
        targets.append(("vesting_deed", by_reception[deed_dict["reception"]]))

    seen_receptions = {d.reception for d in all_docs}
    async with _recorder_search_session(username, password) as page:
        if page is None:
            return targets
        targets += await _easement_row_search(page, parcel, seen_receptions)

    logger.info(
        "Partial history search: selected %d target(s) (%d vesting, %d easements/ROW)",
        len(targets),
        sum(1 for role, _ in targets if role == "vesting_deed"),
        sum(1 for role, _ in targets if role == "easement_or_row"),
    )
    return targets


# ---------------------------------------------------------------------------
# Owner-Name Search — owner + S/T/R driven exemption/easement/ALTA packet
# (decision path "alternate_empty": Document History is empty)
# ---------------------------------------------------------------------------

_VESTING_DEED_LABELS = {
    "WARRANTY DEED",
    "SPECIAL WARRANTY DEED",
    "QUIT CLAIM DEED",
    "GENERAL WARRANTY DEED",
}

# Any other label with DEED in it also vests title — "JOINT TENANCY WARRANTY
# DEED", "PERSONAL REPRESENTATIVES DEED", "BARGAIN AND SALE DEED",
# "TREASURERS DEED" — so the set above is a floor, not the whole list. What has
# to stay out is the paperwork that says DEED without conveying the land: a
# deed of trust is a mortgage, and mineral/royalty deeds and easement deeds
# convey something other than the fee.
_NOT_A_VESTING_DEED = (
    "DEED OF TRUST",
    "TRUST DEED",
    "EASEMENT",
    "MINERAL",
    "ROYALTY",
    "RELEASE",
    "ASSIGNMENT",
    "MODIFICATION",
    "AMENDMENT",
    "SUBORDINATION",
)

# Document Types filter list for the subdivision-exemption search.
_EXEMPTION_DOC_TYPES = {
    "SUBDIVISION EXEMPTION",
    "EXEMPTION",
    "MINOR SUBDIVISION",
    "AMENDED EXEMPTION",
}


def _matches_vesting_deed_label(doc_type_label: str) -> bool:
    upper = doc_type_label.upper().strip()
    if upper in _VESTING_DEED_LABELS:
        return True
    return "DEED" in upper and not any(t in upper for t in _NOT_A_VESTING_DEED)


def _matches_exemption_filter(doc_type_label: str) -> bool:
    upper = doc_type_label.upper().strip()
    return upper in _EXEMPTION_DOC_TYPES or "EXEMPTION" in upper


def _matches_survey_filter(doc_type_label: str) -> bool:
    upper = doc_type_label.upper().strip()
    return "SURVEY" in upper or "ALTA" in upper


def _row_to_record(row: dict) -> _DocRecord:
    reception = row["reception"]
    return _DocRecord(
        reception=reception,
        rec_date=row["rec_date"],
        doc_type=row["doc_type"],
        grantor="",
        grantee="",
        url=f"https://recording.weld.gov/web/web/integration/document/{reception}",
    )


# An owner-name search narrowed to this section returns a handful of rows; the
# cap only bites on the county-wide fallback query, where an owner who holds
# land all over Weld comes back with dozens. Deeds are never what gets cut —
# the list is ordered with them first.
_MAX_OWNER_NAME_DOCS = 25


async def _owner_name_search(
    page,
    parcel: ParcelInfo,
    seen_receptions: set[str],
) -> list[tuple[str, _DocRecord]]:
    """Advanced Search under the current owner's name — everything it returns.

    This is the surveyor's own manual move, and it has to run on **every**
    route, not just the empty-history one: a vesting deed is rarely named on a
    survey, and the parcel's Document History only lists what the assessor
    linked to the account, which routinely isn't the deed. Account R8961716
    (job cab228bd) is the case that prompted this — one recorded-exemption row
    on file, no deed anywhere in the run, while the owner's warranty deed sits
    in the recorder under his name.

    Every row comes back, not only the deeds: a document recorded under this
    owner's name is worth having whatever the recorder calls it. Deed rows are
    tagged `vesting_deed` so the one the surveyor is after is obvious in the
    output (and so a run that found none can say so) — the tag is a highlight,
    not a filter.

    Queries are tried in order and the first one that turns up a deed wins:

    1. owner name + the parcel's S/T/R — every hit is both this owner's and
       this section's, so there is nothing to guess at;
    2. surname only + the same S/T/R — Weld prints owners "LAST FIRST M" and
       Tyler indexes some names with a comma, so the full string can miss.
       Only ever run bounded by S/T/R, or a common surname returns the county;
    3. owner name alone — for a platted lot indexed by subdivision rather than
       section, where the S/T/R queries find nothing.

    Rows from every query tried are kept, so a query that found no deed still
    contributes what it did find. Mutates `seen_receptions` in place, like the
    other search helpers.
    """
    if not parcel.owner:
        logger.warning("Owner-name search: no owner name on record — skipping.")
        return []

    queries: list[dict[str, str]] = []
    if parcel.section and parcel.township and parcel.range_:
        str_query = {
            "section": parcel.section.lstrip("0") or parcel.section,
            "township": parcel.township,
            "range_": parcel.range_,
        }
        queries.append({"search_name": parcel.owner, **str_query})
        surname = parcel.owner.split()[0]
        if surname != parcel.owner:
            queries.append({"search_name": surname, **str_query})
    queries.append({"search_name": parcel.owner})

    found: dict[str, dict] = {}
    for query in queries:
        for row in await _search_all_rows(page, **query):
            if row["reception"]:
                found.setdefault(row["reception"], row)
        if any(_matches_vesting_deed_label(r["doc_type"]) for r in found.values()):
            break  # the deed is here; no need to widen the net further

    rows = sorted(found.values(), key=lambda r: _date_sort_key(r["rec_date"]), reverse=True)
    rows.sort(key=lambda r: not _matches_vesting_deed_label(r["doc_type"]))  # deeds first

    targets: list[tuple[str, _DocRecord]] = []
    for row in rows[:_MAX_OWNER_NAME_DOCS]:
        if row["reception"] in seen_receptions:
            continue
        seen_receptions.add(row["reception"])
        role = "vesting_deed" if _matches_vesting_deed_label(row["doc_type"]) else "owner_name"
        targets.append((role, _row_to_record(row)))

    deeds = sum(1 for role, _ in targets if role == "vesting_deed")
    logger.info(
        "Owner-name search (%s): %d row(s), %d new target(s), %d of them deeds",
        parcel.owner,
        len(found),
        len(targets),
        deeds,
    )
    if not deeds:
        narration.info(
            f"No deed recorded under {parcel.owner}'s name turned up in the "
            "Clerk & Recorder — the vesting deed will need a manual look."
        )
    return targets


async def _select_owner_name_search_targets(
    page,
    parcel: ParcelInfo,
) -> list[tuple[str, _DocRecord]]:
    """Pick targets for the owner-name route (decision path "alternate_empty").

    Document History is empty for this parcel, so everything is driven off
    Advanced Search using the owner's name and the parcel's
    Section/Township/Range instead. `page` must come from an
    already-authenticated `_recorder_search_session`.

    Returns role-tagged docs in download order:
      - ("vesting_deed", row)          `_owner_name_search()`'s hits
      - ("affidavit", row)             AFFIDAVIT rows from the owner search —
                                        the SOP flags these as typical Exhibit
                                        A carriers for a large owner's
                                        contiguous parcels
      - ("subdivision_exemption", row) each SUBDIVISION EXEMPTION-family hit
      - ("easement_or_row", row)       same S/T/R easement/ROW scan the
                                        partial-history route uses
      - ("alta", row)                  a SURVEY/ALTA hit, if the S/T/R search
                                        turns one up that Document History missed

    Harvesting extra Section/Township/Range values from a quit-claim deed's
    Exhibit A is not implemented, for the same reason the partial-history
    route skips Exhibit A cross-references: Tyler PDFs are scanned images
    with no text layer. The search universe falls back to the parcel's own
    Identify Results S/T/R instead.
    """
    targets: list[tuple[str, _DocRecord]] = []
    seen: set[str] = set()

    # Owner-name search ("Search Name as Grantor or Grantee"). The deeds come
    # from the shared helper — same search every other route now runs — and the
    # county-wide sweep below is kept for the affidavits it also turns up.
    targets += await _owner_name_search(page, parcel, seen)
    if parcel.owner:
        owner_rows = await _run_advanced_search(page, search_name=parcel.owner)
        for row in owner_rows:
            reception = row["reception"]
            if reception and reception not in seen and "AFFIDAVIT" in row["doc_type"].upper():
                seen.add(reception)
                targets.append(("affidavit", _row_to_record(row)))
    else:
        logger.warning("Owner-name search: no owner name on record — skipping.")

    if not (parcel.section and parcel.township and parcel.range_):
        logger.warning(
            "Owner-name search: parcel S/T/R is incomplete (%s/%s/%s) — skipping "
            "exemption/easement/ALTA searches.",
            parcel.section,
            parcel.township,
            parcel.range_,
        )
        return targets

    section = parcel.section.lstrip("0") or parcel.section
    township = parcel.township
    range_ = parcel.range_

    # Subdivision Exemption search.
    exemption_rows = await _run_advanced_search(
        page, section=section, township=township, range_=range_
    )
    for row in exemption_rows:
        reception = row["reception"]
        if reception and reception not in seen and _matches_exemption_filter(row["doc_type"]):
            seen.add(reception)
            targets.append(("subdivision_exemption", _row_to_record(row)))

    # Easement / ROW scan, same as the partial-history route.
    targets += await _easement_row_search(page, parcel, seen)

    # Last-ditch ALTA/survey search over the same S/T/R.
    survey_rows = await _run_advanced_search(
        page, section=section, township=township, range_=range_
    )
    alta_hit = next(
        (
            r
            for r in survey_rows
            if r["reception"] not in seen and _matches_survey_filter(r["doc_type"])
        ),
        None,
    )
    if alta_hit:
        seen.add(alta_hit["reception"])
        targets.append(("alta", _row_to_record(alta_hit)))
    else:
        narration.info("No recorded ALTA was found — the exemption packet is the survey of record.")

    logger.info(
        "Owner-name search: selected %d target(s) (%s)",
        len(targets),
        ", ".join(f"{role}={doc.reception}" for role, doc in targets) or "none",
    )
    return targets


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
        logger.warning("Parcel resolve: no usable input (address, account, S/T/R, or owner)")
        return None

    logger.info(
        "Parcel resolve: routing %s lookup via %s path",
        query_type,
        "browser (SOP-strict)" if sop_strict else "HTTP",
    )

    if sop_strict:
        return await _get_parcel_info_browser(
            query,
            query_type,
            str_input=str_input,
            owner_input=owner_input,
        )
    if query_type == "str":
        logger.warning(
            "Parcel resolve: S/T/R input %r — HTTP path does not support STR queries. "
            "Use --sop-strict for browser-walk STR lookup.",
            str_input,
        )
        return None
    return _get_parcel_info_http(query, query_type)


def _log_identify_results(info: ParcelInfo) -> None:
    """Log the Identify Results panel state per SOP Step 1.5."""
    str_ = info.section_township_range() or "(unknown)"
    logger.info(
        "Parcel resolve complete — Identify Results [%s]: "
        "Owner=%r  Account=%s  Parcel=%s  Address=%r  Subdivision=%r  S-T-R=%s",
        info.source,
        info.owner,
        info.account,
        info.parcel_id,
        info.address,
        info.subdivision,
        str_,
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
    from survey_art.overview import Overview, overview_path

    address = geocoded.one_line()
    logger.info("Weld County scraper starting for: %s", address)
    narration.info(f"Searching the Weld County property records for {address}...")

    # --- Phase 1: parcel discovery (SOP Steps 1.1–1.5) ---
    parcel = await _resolve_parcel(
        geocoded,
        str_input=str_input,
        owner_input=owner_input,
        sop_strict=sop_strict,
    )
    if not parcel:
        narration.info("We couldn't find this property in the Weld County system.")
        return (
            [],
            (f"Parcel resolve failed: could not resolve {address} to a Weld parcel."),
            0.0,
            0,
            0,
        )
    _log_identify_results(parcel)
    if parcel.owner:
        narration.info(f"Found the property — account {parcel.account}, owned by {parcel.owner}.")
    else:
        narration.info(f"Found the property — account {parcel.account}.")
    account = parcel.account
    address = (
        parcel.account
        if re.match(r"^R\d{5,9}$", geocoded.street.strip(), re.IGNORECASE)
        else address
    )

    # Output dir + overview store. Initialized here so every later phase can
    # read/write it. Overview survives partial runs (crashes after Phase 1
    # leave a valid overview.json with just identify_results).
    dest = make_download_dir(geocoded.county, address, base=tmp_dir)
    ov = Overview(overview_path(tmp_dir, geocoded.county.key(), dest.name))
    ov.merge_section(
        "meta",
        {
            "county_key": geocoded.county.key(),
            "input_address": geocoded.one_line(),
            "account": account,
            "sop_path": None,  # filled in by the Decision Matrix
            "source_urls": [
                _PORTAL_SEARCH_URL,
                f"{_PROPERTY_REPORT_URL}?account={account}",
            ],
        },
    )
    ov.set_section("identify_results", parcel.to_dict())

    # --- SOP Step 1.6 + 1.7: Property Report (Account Information page) ---
    # The HTTP GET to propertyreport.weld.gov returns every accordion section
    # server-rendered in a single response, so we capture all of them at once
    # rather than driving "Open/Close All Sections" in a browser.
    narration.info("Reading the county's property report page for ownership and deed history...")
    report_fields = _fetch_report_fields(account)
    for section, values in _group_report_fields(report_fields).items():
        if values:
            ov.set_section(section, values)
    # Every field the report page returned, unsectioned/undeduplicated — kept
    # so nothing the county published is ever lost, even if today's grouping
    # doesn't have a home for it. The frontend deliberately does not render
    # this section (see RAW_SECTION_KEYS in App.tsx) — it's for completeness/
    # future use, not the curated view a surveyor reads.
    ov.set_section("raw_report_fields", report_fields)

    # SOP Step 1.7 Map accordion — render and save the parcel map as PNG.
    map_path = await _capture_map_image(account, dest)
    if map_path:
        ov.merge_section(
            "map",
            {
                "image_path": str(map_path),
                "iframe_url": _MAP_IFRAME_URL.format(account=account),
            },
        )

    # --- SOP Step 1.7: Document History capture (the "Decision Frame") ---
    # Parse the document history table directly from the property report HTML.
    # An empty result is valid — per the SOP it routes to the owner-name search.
    all_docs = _fetch_document_history(account)
    ov.set_section("document_history", [d.to_dict() for d in all_docs])
    narration.info(f"Found {len(all_docs)} recorded document(s) on file for this property.")

    # --- Phase 2: Decision Matrix (SOP Phase 2) ---
    report_html = _fetch_property_report_html(account)  # cached
    decision = _decision_matrix(all_docs, report_html)
    ov.set_section("decision_matrix", decision)
    ov.merge_section("meta", {"sop_path": decision["path"]})
    logger.info(
        "Decision Matrix: route %s — %s",
        decision["path"],
        decision["reasoning"][0],
    )

    # --- Phase 3: route by Decision Matrix outcome ---
    s = get_settings()
    route_section: str
    if decision["path"] == "direct":
        narration.info("The survey and deed are on file — downloading them now...")
        targets = _select_direct_extraction_targets(decision, all_docs)
        route_section = "direct_extraction"
        if not targets:
            narration.info("Couldn't find a survey or deed to download for this property.")
            return (
                [],
                (f"Direct extraction found no SURV or vesting deed for {account}."),
                0.0,
                0,
                0,
            )
        logger.info(
            "Direct extraction targets: %s",
            ", ".join(f"{role}={doc.reception}({doc.doc_type})" for role, doc in targets),
        )
    elif decision["path"] == "alternate_partial":
        narration.info(
            "The main documents aren't directly on file — checking the Clerk & "
            "Recorder's office for related records..."
        )
        targets = await _select_partial_history_targets(
            decision,
            all_docs,
            parcel,
            username=s.weld_recorder_username,
            password=s.weld_recorder_password,
        )
        route_section = "partial_history_search"
        if not targets:
            logger.info(
                "Partial history search produced no download targets — stopping. Overview at %s",
                ov.path,
            )
            narration.info("No downloadable documents were found for this property.")
            ov.set_section(route_section, {"targets": [], "results": []})
            return [], None, 0.0, 0, 0
        logger.info(
            "Partial history search targets: %s",
            ", ".join(f"{role}={doc.reception}({doc.doc_type})" for role, doc in targets),
        )
    elif decision["path"] == "alternate_empty":
        narration.info(
            "No documents are on file directly for this parcel — searching by "
            "owner name and section/township/range instead..."
        )
        route_section = "owner_name_search"
        async with _recorder_search_session(
            s.weld_recorder_username, s.weld_recorder_password
        ) as search_page:
            targets = (
                await _select_owner_name_search_targets(search_page, parcel) if search_page else []
            )
        if not targets:
            logger.info(
                "Owner-name search produced no download targets — stopping. Overview at %s",
                ov.path,
            )
            narration.info("No downloadable documents were found for this property.")
            ov.set_section(route_section, {"targets": [], "results": []})
            return [], None, 0.0, 0, 0
        logger.info(
            "Owner-name search targets: %s",
            ", ".join(f"{role}={doc.reception}({doc.doc_type})" for role, doc in targets),
        )
    else:
        logger.info(
            "Decision Matrix route %r is not handled — stopping. Overview at %s",
            decision["path"],
            ov.path,
        )
        narration.info("We couldn't automatically determine the next step for this property.")
        return [], None, 0.0, 0, 0

    ov.set_section(
        route_section,
        {
            "targets": [
                {"role": role, "reception": doc.reception, "doc_type": doc.doc_type, "url": doc.url}
                for role, doc in targets
            ],
            "results": [],  # filled in after download
        },
    )

    results, cost, in_tok, out_tok = await _download_documents(
        address,
        targets,
        doc_filter,
        dest,
        username=s.weld_recorder_username,
        password=s.weld_recorder_password,
    )

    # --- Searches that run on every route, whatever Document History held. ---
    # The owner-name deed search runs for every property: a vesting deed is
    # rarely named on a survey and often isn't linked to the account either, so
    # searching the recorder under the owner's name is the only reliable way to
    # get it (see `_owner_name_search`). The empty-history route has
    # already run it as part of picking its targets.
    # The S/T/R easement/ROW scan is the other half — per the SOP that research
    # is required even when direct extraction already found the ALTA, since its
    # Schedule B-2 only lists what its surveyor happened to cite. The other two
    # routes run it while selecting their own targets.
    narration.info(
        "Also checking the Clerk & Recorder for the current owner's deed, and "
        "for easements and rights-of-way recorded against this parcel's section..."
    )
    known_receptions = {doc.reception for _, doc in targets}
    supplemental: list[tuple[str, _DocRecord]] = []
    async with _recorder_search_session(
        s.weld_recorder_username, s.weld_recorder_password
    ) as search_page:
        if search_page:
            if route_section != "owner_name_search":
                supplemental += await _owner_name_search(search_page, parcel, known_receptions)
            if route_section == "direct_extraction":
                supplemental += await _easement_row_search(search_page, parcel, known_receptions)
    if supplemental:
        deeds = sum(1 for role, _ in supplemental if role == "vesting_deed")
        narration.info(
            f"Found {deeds} deed(s) recorded under the owner's name, plus "
            f"{len(supplemental) - deeds} other document(s) — downloading them now..."
        )
        sup_results, _sup_cost, sup_in_tok, sup_out_tok = await _download_documents(
            address,
            supplemental,
            doc_filter,
            dest,
            username=s.weld_recorder_username,
            password=s.weld_recorder_password,
        )
        in_tok += sup_in_tok
        out_tok += sup_out_tok
        targets = targets + supplemental
        results = results + sup_results
        ov.set_section(
            "owner_deed_and_easement_search",
            {
                "targets": [
                    {
                        "role": role,
                        "reception": doc.reception,
                        "doc_type": doc.doc_type,
                        "url": doc.url,
                    }
                    for role, doc in supplemental
                ],
                "results": [
                    {
                        "role": role,
                        "reception": doc.reception,
                        "doc_type": doc.doc_type,
                        "status": "downloaded" if paths else "failed",
                        "files": [str(p) for p in paths],
                    }
                    for role, doc, paths in sup_results
                ],
            },
        )

    # --- Read every document downloaded so far for the other documents it
    # cites — not just the ALTA — and fetch those too, recursively. ---
    narration.info(
        "Checking each downloaded document for references to other recorded documents..."
    )
    known_receptions = {doc.reception for _, doc in targets}
    cross_ref_results, cr_in_tok, cr_out_tok = await _expand_cross_references(
        address,
        doc_filter,
        dest,
        ov,
        results,
        known_receptions,
        username=s.weld_recorder_username,
        password=s.weld_recorder_password,
    )
    in_tok += cr_in_tok
    out_tok += cr_out_tok
    if cross_ref_results:
        results = results + cross_ref_results
        targets = targets + [(role, doc) for role, doc, _ in cross_ref_results]
        ov.set_section(
            "cross_references",
            {
                "targets": [
                    {
                        "role": role,
                        "reception": doc.reception,
                        "doc_type": doc.doc_type,
                        "url": doc.url,
                    }
                    for role, doc, _ in cross_ref_results
                ],
                "results": [
                    {
                        "role": role,
                        "reception": doc.reception,
                        "doc_type": doc.doc_type,
                        "status": "downloaded" if paths else "failed",
                        "files": [str(p) for p in paths],
                    }
                    for role, doc, paths in cross_ref_results
                ],
            },
        )

    # --- Search recording.weld.gov's Advanced Search for every other
    # document recorded against this parcel's section, and download those
    # too — a surveyor wants to know about anything else recorded here, not
    # just what this property's own history happened to cite. ---
    narration.info(
        "Searching the Clerk & Recorder for other documents recorded in this "
        "property's section..."
    )
    str_targets: list[tuple[str, _DocRecord]] = []
    section_index: list[dict] = []
    async with _recorder_search_session(
        s.weld_recorder_username, s.weld_recorder_password
    ) as search_page:
        if search_page:
            str_targets, section_index = await _section_township_range_search(
                search_page, parcel, known_receptions
            )
    if str_targets:
        if len(section_index) > len(str_targets):
            narration.info(
                f"This section has {len(section_index)} recorded document(s) — "
                f"downloading the {len(str_targets)} most relevant to a survey. "
                "The full list is in the property metadata."
            )
        else:
            narration.info(f"Found {len(str_targets)} additional document(s) in this section.")
        str_results, _str_cost, str_in_tok, str_out_tok = await _download_documents(
            address,
            str_targets,
            doc_filter,
            dest,
            username=s.weld_recorder_username,
            password=s.weld_recorder_password,
        )
        in_tok += str_in_tok
        out_tok += str_out_tok
        targets = targets + str_targets
        results = results + str_results
        ov.set_section(
            "section_township_range_search",
            {
                "targets": [
                    {
                        "role": role,
                        "reception": doc.reception,
                        "doc_type": doc.doc_type,
                        "url": doc.url,
                    }
                    for role, doc in str_targets
                ],
                "results": [
                    {
                        "role": role,
                        "reception": doc.reception,
                        "doc_type": doc.doc_type,
                        "status": "downloaded" if paths else "failed",
                        "files": [str(p) for p in paths],
                    }
                    for role, doc, paths in str_results
                ],
                # Every document the recorder indexes against this section,
                # downloaded or not — a document that wasn't fetched is still
                # one the surveyor may want to pull by hand.
                "section_index": [
                    {
                        "reception": r["reception"],
                        "doc_type": r["doc_type"],
                        "rec_date": r["rec_date"],
                    }
                    for r in section_index
                ],
            },
        )

        # A recorded survey or plat from this section carries its own title
        # exception table — the same list a title commitment gives, as the last
        # surveyor to work here compiled it. That makes these the section-scan
        # documents worth paying to read; the rest are delivered as files.
        survey_results = [
            (role, doc, paths)
            for role, doc, paths in str_results
            if paths and _section_scan_rank(doc.doc_type) == 0
        ]
        if survey_results:
            narration.info(
                f"Reading {len(survey_results)} survey/plat document(s) from this section "
                "for the records they cite..."
            )
            survey_refs, sr_in_tok, sr_out_tok = await _expand_cross_references(
                address,
                doc_filter,
                dest,
                ov,
                survey_results,
                known_receptions,
                username=s.weld_recorder_username,
                password=s.weld_recorder_password,
            )
            in_tok += sr_in_tok
            out_tok += sr_out_tok
            if survey_refs:
                results = results + survey_refs
                targets = targets + [(role, doc) for role, doc, _ in survey_refs]

    # Record per-target results in overview.json. A target with zero files
    # captured is "failed"; non-zero is "downloaded".
    results_section: list[dict] = []
    saved_paths: list[Path] = []
    for role, doc, paths in results:
        results_section.append(
            {
                "role": role,
                "reception": doc.reception,
                "doc_type": doc.doc_type,
                "status": "downloaded" if paths else "failed",
                "files": [str(p) for p in paths],
            }
        )
        saved_paths.extend(paths)
    ov.merge_section(route_section, {"results": results_section})

    # One row per downloaded file, keyed by the filename the Results tab shows,
    # so the frontend can group the grid by category and sort by reception
    # without re-deriving either from the filename. Every route's documents land
    # here — each route writes its own section above, and a surveyor scanning
    # the grid doesn't care which search turned a document up.
    ov.set_section(
        "documents",
        sorted(
            (
                {
                    "file": path.name,
                    "reception": doc.reception,
                    "doc_type": doc.doc_type,
                    "category": classify(doc.doc_type),
                    "role": role,
                }
                for role, doc, paths in results
                for path in paths
            ),
            key=lambda row: (
                CATEGORIES.index(row["category"]),
                reception_sort_key(row["reception"]),
            ),
        ),
    )

    if not saved_paths:
        return (
            [],
            (
                f"Targeted {len(targets)} document(s) for {account} but captured none. "
                f"Receptions: {', '.join(doc.reception for _, doc in targets)}. "
                f"If you see 'must be a registered user' in logs, set WELD_RECORDER_USERNAME / "
                f"WELD_RECORDER_PASSWORD in .env."
            ),
            cost,
            in_tok,
            out_tok,
        )

    return saved_paths, None, cost, in_tok, out_tok
