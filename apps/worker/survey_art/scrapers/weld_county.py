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
import os
import re
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import httpx
from pydantic import ValidationError

from survey_art.doc_classify import CATEGORIES, classify, reception_sort_key
from survey_art.download import download_dir
from survey_art.geocode import GeocodedAddress
from survey_art.id_extraction import IdExtraction, cache_fingerprint, extract_document_ids
from survey_art.overview import Overview, overview_path
from survey_art.scrapers.glo_records import fetch_glo_records
from survey_art.scrapers.weld_road_row import fetch_road_row, road_row_references
from survey_art.settings import get_settings
from survey_shared import jobs

logger = logging.getLogger(__name__)
# Plain-language, step-by-step narration for the non-technical end user (streamed
# to the UI's Logs tab). Separate from `logger` above, which carries the detailed
# SOP/phase diagnostics developers need — narration only ever adds new messages,
# it never replaces those.
narration = logging.getLogger("survey_art.narration")

_PORTAL_SEARCH_URL = "https://apps.weld.gov/propertyportal/index.cfm"
_PROPERTY_REPORT_URL = "https://propertyreport.weld.gov/"
_RECORDER_DOCUMENT_URL = "https://recording.weld.gov/web/web/integration/document/{reception}"

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class _DocRecord:
    reception: str
    rec_date: str = ""
    doc_type: str = ""
    grantor: str = ""
    grantee: str = ""
    doc_fee: str = ""
    sale_date: str = ""
    sale_price: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        # Anything found by search or citation is fetched through the
        # recorder's integration URL; only Document History rows carry their own.
        self.url = self.url or _RECORDER_DOCUMENT_URL.format(reception=self.reception)

    def to_dict(self) -> dict:
        return asdict(self)


def _row_to_record(row: dict) -> _DocRecord:
    """An Advanced Search result row (see `_run_advanced_search`) as a download target."""
    return _DocRecord(
        row["reception"],
        rec_date=row["rec_date"],
        doc_type=row["doc_type"],
        grantor="; ".join(row.get("grantors", [])),
        grantee="; ".join(row.get("grantees", [])),
    )


def _target_rows(targets) -> list[dict]:
    """overview.json rows for `(role, doc)` download targets."""
    return [
        {"role": role, "reception": doc.reception, "doc_type": doc.doc_type, "url": doc.url}
        for role, doc, *_ in targets
    ]


def _result_rows(results: list[tuple[str, _DocRecord, list[Path]]]) -> list[dict]:
    """overview.json rows for `(role, doc, paths)` download results."""
    return [
        {
            "role": role,
            "reception": doc.reception,
            "doc_type": doc.doc_type,
            "status": "downloaded" if paths else "failed",
            "files": [str(p) for p in paths],
        }
        for role, doc, paths in results
    ]


async def _launch_browser(pw, *, slow_mo: int = 400):
    """Chromium for the county sites. Set WELD_HEADED=1 to watch it drive itself."""
    headed = os.environ.get("WELD_HEADED", "").lower() in ("1", "true", "yes")
    return await pw.chromium.launch(
        headless=not headed,
        args=["--disable-blink-features=AutomationControlled"],
        slow_mo=slow_mo if headed else 0,
    )


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

    def str_criteria(self, purpose: str) -> dict[str, str] | None:
        """Advanced Search criteria for this parcel's S/T/R, or None (logged as
        skipping `purpose`) when any of the three is unknown."""
        if not (self.section and self.township and self.range_):
            logger.warning(
                "%s: parcel S/T/R is incomplete (%s/%s/%s) — skipping.",
                purpose,
                self.section,
                self.township,
                self.range_,
            )
            return None
        # _run_advanced_search strips the N/S and E/W off township and range.
        section = self.section.lstrip("0") or self.section
        return {"section": section, "township": self.township, "range_": self.range_}

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


_REPORT_TOKEN_RE = re.compile(r'name="token"[^>]*value="([^"]+)"')


def _fetch_property_report_html(account: str) -> str:
    """Cached fetch of the property report — backing store for both the
    field-dict and the document-history parsers, plus Phase 2's
    'No documents found.' detection. Returns "" on failure, uncached.
    """
    try:
        return _fetch_property_report_html_cached(account)
    except Exception as exc:
        logger.warning("Property report fetch failed for %s: %s", account, exc)
        return ""


@functools.lru_cache(maxsize=8)
def _fetch_property_report_html_cached(account: str) -> str:
    # Since 2026-09 a plain GET only renders the Account Search form; the
    # report sections come back from submitting that form with its CSRF token
    # (same cookie session).
    with httpx.Client(timeout=15.0, headers=_HTTP_HEADERS, follow_redirects=True) as client:
        resp = client.get(_PROPERTY_REPORT_URL, params={"account": account})
        resp.raise_for_status()
        token = _REPORT_TOKEN_RE.search(resp.text)
        if token:
            resp = client.post(
                f"{_PROPERTY_REPORT_URL}index.cfm",
                params={"defaultSection": "acctInfo"},
                data={"account": account, "token": token.group(1)},
            )
            resp.raise_for_status()
    if "data-label" not in resp.text:
        raise ValueError("response has no report fields (page layout changed?)")
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
    from playwright.async_api import async_playwright

    info: ParcelInfo | None = None

    async with async_playwright() as pw:
        browser = await _launch_browser(pw)
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
            const panel = '[class*="results"], [class*="Results"], aside, section';
            return m ? m.closest(panel)?.innerText : document.body.innerText;
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
# Three, not four: across 20 production jobs (1,852 documents saved), 196 receptions
# failed every attempt while only 2 needed a 4th to succeed — and each dead attempt
# burns ~25s of a download slot waiting for a button that never appears.
_DOC_FETCH_ATTEMPTS = 3
_DOC_FETCH_PAUSE_S = 2.0  # retry backoff, growing per attempt

# Spacing between viewer opens, shared by every concurrent fetch — this, not
# `weld_download_concurrency`, is what sets the rate recording.weld.gov sees.
# Measured live (2026-10-02), 40-50 documents from a cold session: at ~1/s the
# recorder withheld the print button after ~28 and kept doing so for minutes
# (32/40 saved). At 12/min and at this setting it blipped once around document
# 36 and every miss cleared on the first retry (50/50 both times). The old
# ~10/min — imposed by accident by the 20s visible-wait — moved 250/250 in prod.
# A short burst is allowed first, since most batches are a cross-reference
# level of 2-10 documents and the throttle only trips well past that.
_DOC_FETCH_INTERVAL_S = 6.0
_DOC_FETCH_BURST = 10
_next_fetch_at = 0.0


async def _pace_fetch() -> None:
    """Wait for this fetch's slot: up to `_DOC_FETCH_BURST` unused slots bank up
    while idle, then one per `_DOC_FETCH_INTERVAL_S`."""
    global _next_fetch_at
    now = asyncio.get_running_loop().time()
    _next_fetch_at = max(_next_fetch_at, now - (_DOC_FETCH_BURST - 1) * _DOC_FETCH_INTERVAL_S)
    wait = max(0.0, _next_fetch_at - now)
    _next_fetch_at += _DOC_FETCH_INTERVAL_S
    await asyncio.sleep(wait)


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


async def _reassert_disclaimer(ctx) -> None:
    """Replace whatever disclaimer cookie the server last set with ours."""
    await ctx.clear_cookies(name="disclaimerAccepted")
    await ctx.add_cookies([_DISCLAIMER_COOKIE])


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
        # Pace every attempt, not just the retries: what the county's server
        # reacts to is the request rate, so every fetch in flight shares one
        # schedule (`_pace_fetch`), and retries back off on top of it.
        await asyncio.sleep(_DOC_FETCH_PAUSE_S * attempt)
        await _pace_fetch()
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
                await _reassert_disclaimer(ctx)
        # Nothing below may escape: these run under one `asyncio.gather`, so a
        # raise here would cancel every other document in flight (mid-write, in
        # the worst case) and skip the browser teardown. A document that can't
        # be fetched returns no paths instead — the caller already treats that
        # as "failed" and carries on with the rest.
        where = f"{role} reception {doc.reception} (attempt {attempt + 1}/{_DOC_FETCH_ATTEMPTS})"
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
            #
            # `state="attached"`: the button is in the DOM but hidden, so the
            # default visible-wait never succeeds and every document used to sit
            # out the whole timeout (measured: 20.5s each, vs ~2s attached). The
            # timeout now only bounds receptions that have no image at all.
            try:
                await doc_page.wait_for_selector("#printCustom", state="attached", timeout=10_000)
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
                    "No printCustom button for %s — %s", where, msg or "(no diagnostic message)"
                )
                continue

            pdf_url = f"https://recording.weld.gov{href}"
            resp = await ctx.request.get(pdf_url)
            body = await resp.body() if resp.status == 200 else b""
            if resp.status != 200:
                logger.warning("Print endpoint returned HTTP %s for %s", resp.status, where)
                continue
            if not body or body[:5] != b"%PDF-":
                logger.warning(
                    "Print endpoint returned non-PDF body for %s (head=%r, %d bytes)",
                    where,
                    body[:8],
                    len(body),
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
            logger.warning("Download error for %s: %s", where, exc)
        finally:
            if doc_page is not None:
                await doc_page.close()

    return role, doc, doc_saved


async def _download_documents(
    targets: list[tuple[str, _DocRecord]],
    dest_dir: Path,
) -> list[tuple[str, _DocRecord, list[Path]]]:
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

    Returns a list of `(role, doc_record, [saved_paths])`, in `targets` order,
    so the caller can map each downloaded document back to its semantic role.
    """
    if not targets:
        return []

    from playwright.async_api import async_playwright

    dest_dir.mkdir(parents=True, exist_ok=True)
    s = get_settings()

    async with async_playwright() as pw:
        browser = await _launch_browser(pw, slow_mo=500)
        ctx = await browser.new_context(user_agent=_HTTP_HEADERS["User-Agent"])
        await ctx.add_cookies([_DISCLAIMER_COOKIE])

        # Anonymous viewing gets a "must be a registered user" stub instead of
        # the document, but the attempt is still made (and logged) without creds.
        if s.weld_recorder_username and s.weld_recorder_password:
            if not await _recorder_login(ctx, s.weld_recorder_username, s.weld_recorder_password):
                await browser.close()
                return []

        # Download each document as a single complete PDF.
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
        limit = max(1, s.weld_download_concurrency)
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
    return results


# ---------------------------------------------------------------------------
# Partial History Search — S/T/R Advanced Search for easements/ROW, plus
# whatever vesting deed is on file (decision path "alternate_partial")
# ---------------------------------------------------------------------------

_ADVANCED_SEARCH_URL = "https://recording.weld.gov/web/search/DOCSEARCH524S12"


def _matches_easement_filter(doc_type_label: str) -> bool:
    """Loose match on the Advanced Search Type column — the recorder spells the
    same easement/ROW type many ways ("EASEMENT RIGHT OF WAY & SURFACE USE AGR",
    "R/W AGREEMENT", "RIGHT OF WAY (RW)"), so match keywords, not a list."""
    upper = doc_type_label.upper()
    return any(t in upper for t in ("EASEMENT", "RIGHT OF WAY", "R/W", "ROW"))


# Bounces sometimes come in runs — three back-to-back retries all failed on one
# S32-T5N-R65W sweep — so later retries wait first.
_DISCLAIMER_RETRIES = 5
_DISCLAIMER_BACKOFF_S = 10.0


async def _run_advanced_search(page, **criteria: str) -> list[dict]:
    """`_advanced_search_once`, retried when the recorder bounces to its disclaimer.

    Partway through a session the recorder can bounce any page load to
    /web/user/disclaimer ("the terms of usage have changed") — the server has
    dropped our disclaimer cookie. It lands on whichever reload comes next: the
    form load, the Clear Selections reload, or the search submit itself
    (measured: all three, in one afternoon on S32-T5N-R65W), and it failed every
    R8995911 job from 2026-09-28 to 2026-10-02 with "waiting for
    #field_PLSSLegalID_DOT_Section". Re-asserting the cookie and running the
    whole search again recovers, same as `_fetch_document`'s retry; logging in
    again would not (see there).
    """
    for attempt in range(_DISCLAIMER_RETRIES):
        try:
            rows = await _advanced_search_once(page, **criteria)
            if "/user/disclaimer" not in page.url:
                return rows
        except Exception as exc:
            # A slow submit can still be navigating when the results are read
            # ("Execution context was destroyed" — jobs 813c4d81, c70469e7 on
            # R8995911), and page.url doesn't show where it went until it lands.
            await page.wait_for_load_state()
            if "/user/disclaimer" not in page.url and "context was destroyed" not in str(exc):
                raise
        logger.warning(
            "Advanced Search: bounced to %s (attempt %d/%d)",
            page.url,
            attempt + 1,
            _DISCLAIMER_RETRIES,
        )
        await asyncio.sleep(_DISCLAIMER_BACKOFF_S * attempt)
        await _reassert_disclaimer(page.context)
    raise RuntimeError("Advanced Search: the recorder kept bouncing to its disclaimer page")


async def _advanced_search_once(
    page,
    *,
    section: str = "",
    township: str = "",
    range_: str = "",
    subdivision: str = "",
    search_name: str = "",
    book: str = "",
    page_no: str = "",
    start_date: str = "",
    end_date: str = "",
) -> list[dict]:
    """Drive the recorder's Advanced Search UI and return the result rows.

    The Self Service Web's direct HTTP POST to `/web/searchPost/...` returns
    only metadata; the actual results render only when the search is driven
    through the page UI. Each result is parsed into:

        {"reception": str, "doc_type": str, "rec_date": str, "doc_id": str,
         "grantors": [str], "grantees": [str], "legals": [str]}

    where `doc_id` is Tyler's internal DOC ID (e.g. 'DOC808S1754'). Note this
    returns whatever rows the search criteria match, up to `_RESULT_ROW_CAP` —
    callers post-filter by `doc_type`, and anything that needs the *complete*
    set goes through `_search_all_rows()` instead.

    `search_name` fills 'Search Name as Grantor or Grantee' (the owner-name
    search) — it's a field on this same Advanced Search form
    (`#field_BothNamesID`), not a separate Basic Search page. `start_date` /
    `end_date` are MM/DD/YYYY strings for the Recording Date range.
    """
    # The form opens with a "Continue session?" dialog if any user state exists.
    # Measured across ~60 consecutive searches on one login it never appeared
    # once, so don't wait long for it.
    await page.goto(_ADVANCED_SEARCH_URL, wait_until="networkidle", timeout=30_000)
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
        ("#field_BookPageID_DOT_Book", book),
        ("#field_BookPageID_DOT_Page", page_no),
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
                const h1 = li.querySelector('h1')?.textContent || '';
                const text = h1.replace(/\\s+/g, ' ').trim();
                // Header format: "<reception> • <type> • <date>"
                const parts = text.split(/\\s*•\\s*/);
                // Body columns: <ul><li>Grantor (2)</li><li><b>NAME</b></li>...</ul>
                // — the label carries a count once there's more than one entry.
                const names = label => {
                    for (const ul of li.querySelectorAll('ul.selfServiceSearchResultColumn')) {
                        const items = Array.from(ul.querySelectorAll('li'));
                        const head = (items[0]?.textContent || '').trim();
                        if (head === label || head.startsWith(label + ' ('))
                            return items.slice(1)
                                .map(i => i.textContent.replace(/\\s+/g, ' ').trim())
                                .filter(t => t && t !== 'SEE RECORD');
                    }
                    return [];
                };
                return {
                    doc_id: docId,
                    reception: (parts[0] || '').trim(),
                    doc_type: (parts[1] || '').trim(),
                    rec_date: (parts[2] || '').trim(),
                    grantors: names('Grantor'),
                    grantees: names('Grantee'),
                    legals: names('Legal'),
                };
            });
        }"""
    )
    logger.info(
        "Advanced Search (S=%s T=%s R=%s Sub=%s Name=%s Bk/Pg=%s/%s Dates=%s-%s): %d row(s)",
        section,
        township,
        range_,
        subdivision,
        search_name,
        book,
        page_no,
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

    key = tuple(sorted(criteria.items()))
    if key in _sweep_cache:
        return list(_sweep_cache[key])

    by_reception: dict[str, dict] = {}
    for row in await sweep(_RECORDS_BEGIN, date.today()):
        by_reception.setdefault(row["reception"], row)
    logger.info(
        "Date-swept search (%s): %d distinct document(s)",
        ", ".join(f"{k}={v}" for k, v in criteria.items() if v) or "no criteria",
        len(by_reception),
    )
    _sweep_cache[key] = list(by_reception.values())
    return list(_sweep_cache[key])


# One run's date-swept results, by criteria. The easement scan and the section
# scan both sweep the same S/T/R — 31 searches, ~3 minutes on S32-T5N-R65W — so
# the second one reuses the first. Cleared at the start of every `scrape()` so a
# long-lived local worker never serves one job's index to the next.
_sweep_cache: dict[tuple, list[dict]] = {}


@asynccontextmanager
async def _recorder_search_session():
    """Open an authenticated Playwright page for driving Advanced Search.

    Shared by the partial-history search, the owner-name search, and the
    always-on easement/ROW scan — Advanced Search returns no rows for
    anonymous sessions. Yields `None` (instead of raising) when credentials
    are missing or login fails, so callers can treat "no session" as "skip
    this research route" without crashing the run.
    """
    from playwright.async_api import async_playwright

    s = get_settings()
    username, password = s.weld_recorder_username, s.weld_recorder_password
    if not (username and password):
        logger.warning(
            "Advanced Search: WELD_RECORDER_USERNAME/PASSWORD not set — "
            "skipping (Advanced Search needs an authenticated session)."
        )
        yield None
        return

    async with async_playwright() as pw:
        browser = await _launch_browser(pw)
        try:
            ctx = await browser.new_context(user_agent=_HTTP_HEADERS["User-Agent"])
            await ctx.add_cookies([_DISCLAIMER_COOKIE])
            yield await ctx.new_page() if await _recorder_login(ctx, username, password) else None
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
    criteria = parcel.str_criteria("Easement/ROW search")
    if not criteria:
        return targets

    # Date-swept: the type filter below runs on rows the form already returned,
    # so a capped search silently filters the newest 100 documents rather than
    # every easement ever recorded against the section.
    rows = await _search_all_rows(page, **criteria)
    # If a Subdivision is on file, repeat with Platted Legal (deduped by reception).
    if parcel.subdivision:
        by_reception = {r["reception"]: r for r in rows}
        for r in await _search_all_rows(page, subdivision=parcel.subdivision):
            by_reception.setdefault(r["reception"], r)
        rows = list(by_reception.values())

    for r in rows:
        reception = r["reception"]
        if not reception or reception in seen_receptions:
            continue
        if not _matches_easement_filter(r["doc_type"]):
            continue
        seen_receptions.add(reception)
        targets.append(("easement_or_row", _row_to_record(r)))
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


# The recorder Document Types a chain-of-title search downloads, exactly as
# the recorder spells them — the list our PLS picked from the Advanced Search
# form's Document Types box (2026-10-02), plus the five types a title
# commitment also lists that the box left out (MINERAL DEED, MINERAL QUIT
# CLAIM DEED, DRY UP COVENANT, MEMORANDUM OF LEASE, EXTENSION OIL & GAS LEASE).
# Everything else a chain owner's name turns up is only listed in
# overview.json. Variants the recorder files the same kind of document under
# (corrected / joint tenancy / trustees mineral deeds, corrected O&G leases,
# ANNEXATION PLAT) are listed too, from its full type list, so a property is
# treated the same whichever variant its county clerk picked. Matching ignores
# case and repeated spaces ("Historical", "WATER  AGREEMENT"). To change what
# gets pulled, edit this list.
_CHAIN_PULL_TYPES = frozenset(
    {
        "ACKNOWLEDGMENT",
        "ADJUSTMENT",
        "AGREEMENT",
        "AMENDED ANNEXATION",
        "AMENDED DEVELOPMENT PLAN",
        "AMENDED OIL & GAS LEASE",
        "AMENDED ORDINANCE (RELATED TO A MAP)",
        "AMENDED PLAT",
        "AMENDED PLAT & DEDICATION",
        "AMENDED RECORDED EXEMPTION",
        "AMENDED REPLAT & DEDICATION",
        "AMENDED RIGHT OF WAY",
        "AMENDED SITE PLAN REVIEW",
        "AMENDED SUBDIVISION EXEMPTION",
        "AMENDED SURVEY",
        "AMENDED ZONING PLAT",
        "CERTIFICATE OF PERMANENT LOCATION",
        "CONDOMINIUM MAP",
        "CORRECTED AMENDED RECORDED EXEMPTION",
        "CORRECTED PLAT",
        "CORRECTED RECORDED EXEMPTION",
        "CORRECTED SUBDIVISION EXEMPTION",
        "CORRECTED SURVEY",
        "CORRECTION",
        "DEVELOPMENT PLAN",
        "DITCH STATEMENT",
        "DRAWING",
        "DRY UP COVENANT",
        "EASEMENT",
        "EASEMENT & RIGHT OF WAY",
        "EASEMENT DEED",
        "EASEMENT PLAT",
        "EASEMENT RIGHT OF WAY & SURFACE USE AGM",
        "EXHIBIT",
        "EXTENSION OIL & GAS LEASE",
        "FINAL DEVELOPMENT PLAN",
        "GRANT & RELEASE OF EASEMENT",
        "HISTORICAL",
        "HISTORICAL CONVERSION",
        "IMPROVEMENT LOCATION CERTIFICATE",
        "LEGAL DESCRIPTION",
        "LOCATION ASSESSMENT PLAT",
        "LOT LINE ADJUSTMENT PLAT",
        "LOT LINE ADJUSTMENT REPLAT",
        "MAP",
        "MASTER PLAN",
        "MEMORANDUM OF LEASE",
        "MINERAL DEED",
        "MINERAL QUIT CLAIM DEED",
        "MINOR RESUBDIVISION PLAT",
        "MINOR SUBDIVISION",
        "NOTARY AFFIDAVIT",
        "OATH",
        "OIL & GAS LEASE",
        "ORDINANCE (RELATED TO A MAP)",
        "PARTY WALL AGREEMENT",
        "PARTY WALL DECLARATION CONDITIONS & REST",
        "PATENT",
        "PETITION FOR ANNEXATION",
        "PETITION FOR ADDITION OF LANDS",
        "PETITION FOR EXCLUSION OF LAND",
        "PETITION FOR INCLUSION OF LAND",
        "PLAT",
        "PLAT & DEDICATION",
        "PLOT PLAN",
        "RATIFICATION & CORRECTION OF PLAT",
        "RATIFICATION OF PLAT",
        "RECORDED EXEMPTION",
        "REPLAT",
        "REPLAT & DEDICATION",
        "RESUB",
        "REZONING PLAT",
        "RIGHT OF WAY",
        "RIGHT OF WAY AGREEMENT",
        "RIGHT OF WAY DEED",
        "RIGHT OF WAY EASEMENT",
        "RIGHT OF WAY PLAT",
        "ROAD PETITIONS",
        "RURAL LAND DIVISON",
        "SITE DEVELOPMENT PLAN",
        "SITE PLAN",
        "SITE PLAN REVIEW",
        "SPECIAL DISTRICT DESCRIPTION",
        "SUBDIVISION AGREEMENT",
        "SUBDIVISION EXEMPTION",
        "SUBDIVISION NAME CHANGE",
        "SUPPLEMENT TO PLAT",
        "SUPPLEMENT",
        "SURFACE USE AGREEMENT",
        "SURVEY",
        "SURVEYORS AFFIDAVIT",
        "USE BY SPECIAL REVIEW",
        "VACATION",
        "VACATION & DEDICATION PLAT",
        "VACATION & REPLAT",
        "VACATION AND RE-DEDICATION",
        "VACATION PLAT",
        "VALVE SITE CONTRACT",
        "WATER AGREEMENT",
        "WATER DEED",
        "ZONING MAP",
        "ZONE CHANGE",
        "ANNEXATION PLAT",
        "CORRECTED MINERAL DEED",
        "CORRECTED OIL & GAS LEASE",
        "CORRECTED PERSONAL REP MINERAL DEED",
        "JOINT TENANCY MINERAL DEED",
        "MINERAL & ROYALTY DEED",
        "MINERAL CONVEYANCE",
        "PERSONAL REPRESENTATIVES MINERAL DEED",
        "RATIFICATION & EXTENSION OIL & GAS",
        "TRUSTEES MINERAL DEED",
    }
)

# A deed *to* one of these is a right-of-way take, whatever the recorder calls
# it: R8995911's chain conveyed road ROW to Weld County and the highway
# department, and ditch ROW to the Lower Latham Ditch Company, all as plain
# WARRANTY DEEDs. Also never walked to as a chain owner (see below).
_PUBLIC_GRANTEES = (
    "WELD CO",
    "COUNTY",
    "CITY OF",
    "TOWN ",
    "STATE ",
    "COLORADO STATE",
    "DEPARTMENT",
    "HIGHWAY",
    "TRANSPORTATION",
    "DITCH",
    "RESERVOIR",
    "IRRIGATION",
    "CANAL",
)

# Never searched as a chain owner: a deed out of a bank or trustee is a
# foreclosure or refinance, and its name search returns the county.
_NOT_A_CHAIN_OWNER = _PUBLIC_GRANTEES + ("BANK", "TRUSTEE", "MORTGAGE", "FEDERAL", "SECRETARY")


def _legal_here(row: dict, parcel: ParcelInfo) -> bool | None:
    """Whether a result row's Legal column puts it in the parcel's township
    and range: True / False, or None when the recorder indexed no legal (most
    things before ~1994). Township, not section — a parcel's own vesting deed
    can be indexed to the neighbouring section only (R8995911, in S32: its
    2018 deeds 4372900/4372901 are indexed to S31)."""
    legals = [x for x in row.get("legals", []) if x.strip() and x.strip() != "NO LEGAL"]
    if not legals:
        return None
    here = f"Township: {parcel.township.rstrip('NnSs')} Range: {parcel.range_.rstrip('EeWw')}"
    return any(here in x for x in legals)


def _deed_to_public(row: dict) -> bool:
    return any(p in g.upper() for g in row.get("grantees", []) for p in _PUBLIC_GRANTEES)


def _is_chain_pull(row: dict, parcel: ParcelInfo) -> bool:
    """Whether a row from a chain owner's name search is worth downloading:
    one of `_CHAIN_PULL_TYPES`, or a deed to a government or ditch company (a
    ROW take recorded as a plain deed) — unless its legal puts it in another
    township."""
    if _legal_here(row, parcel) is False:
        return False
    doc_type = " ".join(row["doc_type"].upper().split())
    if doc_type in _CHAIN_PULL_TYPES:
        return True
    return _matches_vesting_deed_label(doc_type) and _deed_to_public(row)


_ENTITY_SUFFIX_RE = re.compile(r"[ ,]+(LLC|LLP|LTD|INC|LP|CORP|CORPORATION)\.?$")

# Owners searched per run — the current one plus this many links back up the
# chain of title. Each is a county-wide name search, so this bounds the run.
_MAX_CHAIN_OWNERS = 8


async def _section_township_range_search(
    page,
    parcel: ParcelInfo,
    seen_receptions: set[str],
    prior_owners: list[str] | tuple[str, ...] = (),
) -> tuple[list[tuple[str, _DocRecord]], list[dict]]:
    """Name search for every owner in the chain of title, plus a search of the
    parcel's section, keeping only the PLS's document types (`_is_chain_pull`).

    The chain is walked backwards: a deed *into* an owner names the owner
    before as its grantor, who is searched next — seeded with the current
    owner and `prior_owners` (Document History's deed parties). Only deeds
    indexed to this township, or not indexed at all, are followed.

    Not bounded by S/T/R. Measured against R8995911's 27-item commitment: the
    recorder indexes only 3 of those documents to S32-T5N-R65W — the rest
    predate legal indexing (~1994) or are indexed to S31 — so no section
    search can find them, while county-wide searches under the chain's names
    (Petroleum Exploration & Management ← Thurman Hays & Co / Chet Hays
    Family Co ← Hays Thurman) find 19. Names go in "Grantor or Grantee": an
    owner granting an easement or ROW is the grantor.

    The section search (S/T/R alone, date-swept) runs too, for what the chain
    can't reach: documents recorded under other people's names.

    Returns `(targets, every_row_found)` — the second element is everything
    either search found, pulled or not, for overview.json. Mutates
    `seen_receptions` in place.
    """
    if not parcel.owner:
        return [], []

    queue = [parcel.owner, *prior_owners]
    searched: list[str] = []
    found: dict[str, dict] = {}
    while queue and len(searched) < _MAX_CHAIN_OWNERS:
        # Tyler matches names by prefix, and the same entity is indexed with and
        # without its suffix: R8995911's "THURMAN HAYS & CO LLP" holds the land
        # through deeds into plain "THURMAN HAYS & CO".
        name = _ENTITY_SUFFIX_RE.sub("", " ".join(queue.pop(0).upper().split()))
        if not name or name in searched or any(p in name for p in _NOT_A_CHAIN_OWNER):
            continue
        searched.append(name)
        for row in await _search_all_rows(page, search_name=name):
            if not row["reception"]:
                continue
            found.setdefault(row["reception"], row)
            if (
                _matches_vesting_deed_label(row["doc_type"])
                and _legal_here(row, parcel) is not False
                and any(name in g.upper() for g in row.get("grantees", []))
            ):
                queue += row.get("grantors", [])
    chain_found = len(found)
    # And everything indexed to the parcel's section, whoever recorded it — the
    # chain search can't see documents recorded under other people's names.
    # Same type filter: S32-T5N-R65W's 874 documents come down to 90.
    if criteria := parcel.str_criteria("Section/Township/Range search"):
        for row in await _search_all_rows(page, **criteria):
            if row["reception"]:
                found.setdefault(row["reception"], row)
    logger.info(
        "Chain-of-title search: %d owner(s) searched (%s), %d document(s) found; "
        "%d more indexed to the section",
        len(searched),
        "; ".join(searched),
        chain_found,
        len(found) - chain_found,
    )

    fresh = [
        r
        for r in found.values()
        if r["reception"] not in seen_receptions and _is_chain_pull(r, parcel)
    ]
    fresh.sort(key=lambda r: _date_sort_key(r["rec_date"]), reverse=True)  # newest first
    fresh.sort(key=lambda r: _section_scan_rank(r["doc_type"]))  # stable: rank, then date
    limit = get_settings().weld_section_download_limit
    if limit and len(fresh) > limit:
        logger.info(
            "Chain-of-title search: %d pertinent document(s), downloading the %d most "
            "survey-relevant (raise WELD_SECTION_DOWNLOAD_LIMIT for more).",
            len(fresh),
            limit,
        )
        fresh = fresh[:limit]

    targets: list[tuple[str, _DocRecord]] = []
    for r in fresh:
        seen_receptions.add(r["reception"])
        targets.append(("section_township_range_search", _row_to_record(r)))
    return targets, list(found.values())


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


# Weld reception numbers run to 7 digits (5,1xx,xxx in 2026). A longer "reception
# number" read off a document is a date, an API well number or a misread — 210
# of the 568 job b721dda1 (R8995911) counted — and is neither fetched nor counted.
_MAX_RECEPTION_DIGITS = 7


def _plausible_reception(reception: str) -> bool:
    return reception.isdigit() and len(reception) <= _MAX_RECEPTION_DIGITS


def _citation_summary(extracted_ids: list[dict], results) -> dict:
    """What became of every reception number the documents cited, for the
    Results tab: downloaded (under any role — a cited document the owner or
    section search already fetched counts), looked up but not retrievable from
    the recorder (usually a misread number), or never fetched (a limit hit)."""
    cited = {
        i["id"]
        for i in extracted_ids
        if i.get("id_type") == "reception_number" and _plausible_reception(i.get("id", ""))
    }
    attempted = {doc.reception.lstrip("0") for _, doc, _ in results}
    downloaded = {doc.reception.lstrip("0") for _, doc, paths in results if paths}
    return {
        "cited": len(cited),
        "downloaded": len(cited & downloaded),
        "not_in_recorder": sorted(cited & attempted - downloaded),
        "not_fetched": sorted(cited - attempted),
    }


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
        if (
            item.id_type != "reception_number"
            or item.id in seen
            or not _plausible_reception(item.id)
        ):
            continue
        seen.add(item.id)
        targets.append(("exception", _DocRecord(item.id, doc_type=item.context)))
    if get_settings().application_mode.lower() == "demo" and len(targets) > _DEMO_EXCEPTION_LIMIT:
        narration.info(
            f"Demo mode: downloading {_DEMO_EXCEPTION_LIMIT} of the "
            f"{len(targets)} referenced documents."
        )
        return targets[:_DEMO_EXCEPTION_LIMIT]
    return targets


_CITED_BOOK_PAGE_RE = re.compile(r"Book (\d+) Page (\d+)")
_CITED_YEAR_RE = re.compile(r"\b(1[89]\d\d|20\d\d)\b")


async def _resolve_book_page_citations(
    page, items: list, known_receptions: set[str], resolved: dict[str, str] | None = None
) -> list[tuple[str, _DocRecord]]:
    """Turn cited Book/Page references into download targets via the recorder's
    Book/Page search.

    This is how anything older than ~1994 gets found at all: Weld only indexed
    legal descriptions from then on, so an S/T/R search is blind to the deeds,
    road rights-of-way and railroad reservations recorded before it. Measured on
    S15-T5N-R67W: 82 documents, two from 1908-1912 and the rest 1994+. Those
    older documents are what an ALTA cites by book and page.

    Book numbers repeat across eras, so a Book/Page hit is only trusted when its
    recording year matches a year printed in the citation. From R1611986's ALTA,
    Book 233 Page 185 ("Union Pacific reservations, Dec 17 1908") resolves to the
    1908 Union Pacific warranty deed, but Book 1583 Page 294 (a 1961 highway
    deed) resolves to a 1996 deed of trust. A citation with no year, or no
    matching hit, is not fetched and stays in `extracted_ids` for a manual look.
    Mutates `known_receptions` in place, and records each citation it matched in
    `resolved` (Book/Page id -> reception) when given.
    """
    targets: list[tuple[str, _DocRecord]] = []
    for item in items:
        m = _CITED_BOOK_PAGE_RE.fullmatch(item.id)
        years = set(_CITED_YEAR_RE.findall(f"{item.context} {item.raw}"))
        if not (m and years):
            logger.info("Book/Page %s: no recording year cited, not auto-fetched", item.id)
            continue
        rows = await _run_advanced_search(page, book=m[1], page_no=m[2])
        # rec_date is "MM/DD/YYYY hh:mm AM"
        hit = next((r for r in rows if r["reception"] and r["rec_date"][6:10] in years), None)
        if not hit:
            logger.info(
                "Book/Page %s: no recorded document from %s (%d other hit(s))",
                item.id,
                "/".join(sorted(years)),
                len(rows),
            )
            continue
        if resolved is not None:
            resolved[item.id] = hit["reception"]
        if hit["reception"] in known_receptions:
            continue
        known_receptions.add(hit["reception"])
        targets.append(("exception", _row_to_record(hit)))
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


# Documents read for citations at once. Each read holds its whole document
# decoded — a 36"x24" plat sheet is ~10,800x7,200 px at a byte per pixel, ~120 MB
# a page — so a level of a dozen section-scan plats peaked at 1.5 GB on its own
# and OOM-killed the 2 GB task mid-run (R8995911, 66 min, no traceback). Bedrock
# calls are pooled at 16 separately (id_extraction._BEDROCK_CONCURRENCY), so six
# documents in flight still keep that pool busy.
_EXTRACTION_CONCURRENCY = 6


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
    dest: Path,
    ov,
    initial_results: list[tuple[str, _DocRecord, list[Path]]],
    known_receptions: set[str],
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
    searched_book_pages: set[str] = set()
    extraction_slots = asyncio.Semaphore(_EXTRACTION_CONCURRENCY)
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

        async def extract(doc: _DocRecord, path: Path) -> IdExtraction:
            async with extraction_slots:
                return await asyncio.to_thread(_extract_cited_ids, doc.reception, path)

        extractions = await asyncio.gather(*(extract(doc, paths[0]) for _, doc, paths in batch))

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

        # Book/Page citations need a recorder search to become a reception
        # number, so they're resolved for the whole level in one session.
        book_pages = [
            item
            for extraction in extractions
            for item in extraction.ids
            if item.id_type == "book_page" and item.id not in searched_book_pages
        ]
        if (
            book_pages
            and depth < _MAX_CROSS_REFERENCE_DEPTH
            and discovered < _MAX_CROSS_REFERENCE_DOCS
        ):
            searched_book_pages.update(item.id for item in book_pages)
            async with _recorder_search_session() as search_page:
                if search_page:
                    resolved: dict[str, str] = dict(ov.get("book_page_resolutions", {}))
                    found = await _resolve_book_page_citations(
                        search_page, book_pages, known_receptions, resolved
                    )
                    ov.set_section("book_page_resolutions", resolved)
                    found = found[: _MAX_CROSS_REFERENCE_DOCS - discovered]
                    discovered += len(found)
                    if found:
                        narration.info(
                            f"Found {len(found)} older document(s) cited by book and "
                            "page — downloading them now..."
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

        downloaded = await _download_documents(next_targets, dest)
        new_results.extend(downloaded)
        level = downloaded
        depth += 1

    return new_results, in_tok, out_tok


async def _select_partial_history_targets(
    decision: dict,
    all_docs: list[_DocRecord],
    parcel: ParcelInfo,
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
    async with _recorder_search_session() as page:
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

# Any label with DEED in it vests title — "WARRANTY DEED", "JOINT TENANCY
# WARRANTY DEED", "PERSONAL REPRESENTATIVES DEED", "TREASURERS DEED". What has
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


def _matches_vesting_deed_label(doc_type_label: str) -> bool:
    upper = doc_type_label.upper()
    return "DEED" in upper and not any(t in upper for t in _NOT_A_VESTING_DEED)


def _matches_exemption_filter(doc_type_label: str) -> bool:
    upper = doc_type_label.upper()
    return "EXEMPTION" in upper or "MINOR SUBDIVISION" in upper


def _matches_survey_filter(doc_type_label: str) -> bool:
    upper = doc_type_label.upper()
    return "SURVEY" in upper or "ALTA" in upper


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
    if str_query := parcel.str_criteria("Owner-name search, S/T/R-bounded queries"):
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

    criteria = parcel.str_criteria("Owner-name route's exemption/easement/ALTA searches")
    if not criteria:
        return targets

    # One S/T/R search serves both the exemption and the last-ditch ALTA pick.
    str_rows = await _run_advanced_search(page, **criteria)
    for row in str_rows:
        reception = row["reception"]
        if reception and reception not in seen and _matches_exemption_filter(row["doc_type"]):
            seen.add(reception)
            targets.append(("subdivision_exemption", _row_to_record(row)))

    # Easement / ROW scan, same as the partial-history route.
    targets += await _easement_row_search(page, parcel, seen)

    # Last-ditch ALTA/survey pick over the same S/T/R.
    alta_hit = next(
        (
            r
            for r in str_rows
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
    address = geocoded.one_line()
    _sweep_cache.clear()
    logger.info("Weld County scraper starting for: %s", address)
    narration.info(f"Searching the Weld County property records for {address}...")

    # --- Phase 1: parcel discovery (SOP Steps 1.1–1.5) ---
    parcel = await _resolve_parcel(
        geocoded, str_input=str_input, owner_input=owner_input, sop_strict=sop_strict
    )
    if not parcel:
        narration.info("We couldn't find this property in the Weld County system.")
        return [], f"Parcel resolve failed: could not resolve {address} to a Weld parcel.", 0, 0, 0
    _log_identify_results(parcel)
    owned_by = f", owned by {parcel.owner}" if parcel.owner else ""
    narration.info(f"Found the property — account {parcel.account}{owned_by}.")
    account = parcel.account
    if re.match(r"^R\d{5,9}$", geocoded.street.strip(), re.IGNORECASE):
        address = account

    # Output dir + overview store. Initialized here so every later phase can
    # read/write it. Overview survives partial runs (crashes after Phase 1
    # leave a valid overview.json with just identify_results).
    dest = download_dir(geocoded.county, address, tmp_dir)
    ov = Overview(overview_path(tmp_dir, geocoded.county.key(), dest.name))
    ov.merge_section(
        "meta",
        {
            "county_key": geocoded.county.key(),
            "input_address": geocoded.one_line(),
            "account": account,
            "sop_path": None,  # filled in by the Decision Matrix
            "source_urls": [_PORTAL_SEARCH_URL, f"{_PROPERTY_REPORT_URL}?account={account}"],
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
    # this section (see RAW_SECTION_KEYS in App.tsx).
    ov.set_section("raw_report_fields", report_fields)

    # SOP Step 1.7 Map accordion — render and save the parcel map as PNG. The
    # ESRI map takes ~30s to paint and nothing downstream reads it, so it renders
    # in the background while the recorder research runs; `record_map()` must be
    # awaited before every return so map.png is on disk for the upload.
    map_task = asyncio.create_task(_capture_map_image(account, dest))

    async def record_map() -> None:
        if map_path := await map_task:
            ov.merge_section(
                "map",
                {
                    "image_path": str(map_path),
                    "iframe_url": _MAP_IFRAME_URL.format(account=account),
                },
            )

    # --- SOP Step 1.7: Document History capture (the "Decision Frame") ---
    # An empty result is valid — per the SOP it routes to the owner-name search.
    all_docs = _fetch_document_history(account)
    ov.set_section("document_history", [d.to_dict() for d in all_docs])
    narration.info(f"Found {len(all_docs)} recorded document(s) on file for this property.")

    # --- Phase 2: Decision Matrix (SOP Phase 2) ---
    decision = _decision_matrix(all_docs, _fetch_property_report_html(account))
    ov.set_section("decision_matrix", decision)
    ov.merge_section("meta", {"sop_path": decision["path"]})
    logger.info("Decision Matrix: route %s — %s", decision["path"], decision["reasoning"][0])

    # --- Phase 3: route by Decision Matrix outcome ---
    if decision["path"] == "direct":
        narration.info("The survey and deed are on file — downloading them now...")
        targets = _select_direct_extraction_targets(decision, all_docs)
        route_section = "direct_extraction"
        if not targets:
            narration.info("Couldn't find a survey or deed to download for this property.")
            await record_map()
            return [], f"Direct extraction found no SURV or vesting deed for {account}.", 0, 0, 0
    elif decision["path"] == "alternate_partial":
        narration.info(
            "The main documents aren't directly on file — checking the Clerk & "
            "Recorder's office for related records..."
        )
        targets = await _select_partial_history_targets(decision, all_docs, parcel)
        route_section = "partial_history_search"
    elif decision["path"] == "alternate_empty":
        narration.info(
            "No documents are on file directly for this parcel — searching by "
            "owner name and section/township/range instead..."
        )
        route_section = "owner_name_search"
        async with _recorder_search_session() as search_page:
            targets = (
                await _select_owner_name_search_targets(search_page, parcel) if search_page else []
            )
    else:
        logger.info("Decision Matrix route %r is not handled — stopping.", decision["path"])
        narration.info("We couldn't automatically determine the next step for this property.")
        await record_map()
        return [], None, 0.0, 0, 0

    if not targets:
        logger.info("%s produced no download targets — stopping.", route_section)
        narration.info("No downloadable documents were found for this property.")
        ov.set_section(route_section, {"targets": [], "results": []})
        await record_map()
        return [], None, 0.0, 0, 0
    logger.info(
        "%s targets: %s",
        route_section,
        ", ".join(f"{role}={doc.reception}({doc.doc_type})" for role, doc in targets),
    )

    # Phases 4 and 5 only need S/T/R and the account, and talk to the BLM and
    # county GIS/CDOT servers rather than recording.weld.gov — so they run in
    # the background for the whole recorder walk instead of after it.
    glo_task = None
    if parcel.township and parcel.range_:
        narration.info(
            "Looking up the original BLM General Land Office survey of record "
            f"for Township {parcel.township} Range {parcel.range_}..."
        )
        glo_task = asyncio.create_task(
            fetch_glo_records(
                state=geocoded.county.state,
                county=geocoded.county.name,
                section=parcel.section,
                township=parcel.township,
                range_=parcel.range_,
                dest_dir=dest,
            )
        )
    narration.info(
        "Assembling the road right-of-way packet: county road records and state "
        "highway right-of-way plans next to this property..."
    )
    row_task = asyncio.create_task(
        fetch_road_row(
            account=account,
            section=parcel.section,
            township=parcel.township,
            range_=parcel.range_,
            dest_dir=dest,
        )
    )

    ov.set_section(route_section, {"targets": _target_rows(targets), "results": []})
    results = await _download_documents(targets, dest)
    in_tok = out_tok = 0

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
    async with _recorder_search_session() as search_page:
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
        sup_results = await _download_documents(supplemental, dest)
        targets += supplemental
        results += sup_results
        ov.set_section(
            "owner_deed_and_easement_search",
            {"targets": _target_rows(supplemental), "results": _result_rows(sup_results)},
        )

    # --- Read every document downloaded so far for the other documents it
    # cites — not just the ALTA — and fetch those too, recursively. ---
    narration.info(
        "Checking each downloaded document for references to other recorded documents..."
    )
    known_receptions = {doc.reception for _, doc in targets}
    cross_ref_results, cr_in_tok, cr_out_tok = await _expand_cross_references(
        dest, ov, results, known_receptions
    )
    in_tok += cr_in_tok
    out_tok += cr_out_tok
    if cross_ref_results:
        results += cross_ref_results
        targets += [(role, doc) for role, doc, _ in cross_ref_results]
        ov.set_section(
            "cross_references",
            {
                "targets": _target_rows(cross_ref_results),
                "results": _result_rows(cross_ref_results),
            },
        )

    # --- Search recording.weld.gov's Advanced Search for every other
    # document recorded against this parcel's section, and download those
    # too — a surveyor wants to know about anything else recorded here, not
    # just what this property's own history happened to cite. ---
    narration.info(
        "Following the chain of title: searching the Clerk & Recorder under the current "
        "and previous owners' names..."
    )
    str_targets: list[tuple[str, _DocRecord]] = []
    section_index: list[dict] = []
    async with _recorder_search_session() as search_page:
        if search_page:
            str_targets, section_index = await _section_township_range_search(
                search_page,
                parcel,
                known_receptions,
                [
                    n
                    for d in all_docs
                    if d.doc_type in _VESTING_DEED_TYPES
                    for n in (d.grantee, d.grantor)
                ],
            )
    if str_targets:
        narration.info(
            f"Found {len(section_index)} document(s) under the owners' names — "
            f"downloading the {len(str_targets)} a title commitment would list. "
            "The full list is in the property metadata."
        )
        str_results = await _download_documents(str_targets, dest)
        targets += str_targets
        results += str_results
        ov.set_section(
            "section_township_range_search",
            {
                "targets": _target_rows(str_targets),
                "results": _result_rows(str_results),
                # Everything recorded under the chain owners' names,
                # downloaded or not — a document that wasn't fetched is still
                # one the surveyor may want to pull by hand.
                "section_index": [
                    {k: r[k] for k in ("reception", "doc_type", "rec_date")} for r in section_index
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
                dest, ov, survey_results, known_receptions
            )
            in_tok += sr_in_tok
            out_tok += sr_out_tok
            results += survey_refs
            targets += [(role, doc) for role, doc, _ in survey_refs]

    # --- Phase 4: GLO original survey of record (BLM General Land Office).
    # Runs regardless of which Phase 3 path fired — the original 6th P.M.
    # township survey and field notes are the earliest authoritative survey
    # for the tract, and every later ALTA ties its basis-of-bearings back to
    # it. Only needs S/T/R, so it can't fail on the Decision Matrix outcome.
    await record_map()
    cost = 0.0
    glo_files: list[tuple[Path, str]] = []
    if glo_task:
        glo_files, glo_log, cost, glo_in_tok, glo_out_tok = await glo_task
        in_tok += glo_in_tok
        out_tok += glo_out_tok
        if glo_files:
            narration.info(f"Found {len(glo_files)} GLO record(s) for this township.")
        else:
            narration.info("No GLO records were found for this township.")
        ov.set_section("glo_records", glo_log)

    # --- Phase 5: state & county road right-of-way packet. Also independent of
    # the Phase 3 path. The Schedule B-2 road exceptions it starts from were
    # already fetched by the cross-reference walk above; this adds the county's
    # road petitions/vacations and CDOT's highway ROW plans, and logs which of
    # those exceptions were and weren't found. ---
    row_files, row_log = await row_task
    downloaded = {doc.reception for _, doc, paths in results if paths}
    row_log["schedule_b_road_exceptions"] = road_row_references(
        ov.get("extracted_ids", []), ov.get("book_page_resolutions", {}), downloaded
    )
    ov.set_section("road_right_of_way", row_log)

    # Step 5.3 — a road petition or vacation cites the Clerk & Recorder's own
    # Book/Page for its deeds ("see Book 11 page 106"); read the BOCC files
    # for those and fetch them like any other citation.
    bocc_results = [
        (
            "county_road_row",
            _DocRecord(f"BOCC-{r['entry_id']}", doc_type=r["doc_type"]),
            [dest / r["file"]],
        )
        for r in row_log["bocc_road_records"]
        if r.get("file")
    ]
    if bocc_results:
        narration.info(
            f"Reading {len(bocc_results)} county road record(s) for the deeds they cite..."
        )
        bocc_refs, b_in_tok, b_out_tok = await _expand_cross_references(
            dest, ov, bocc_results, {doc.reception for _, doc in targets}
        )
        in_tok += b_in_tok
        out_tok += b_out_tok
        if bocc_refs:
            results += bocc_refs
            targets += [(role, doc) for role, doc, _ in bocc_refs]
            ov.set_section(
                "road_right_of_way_citations",
                {"targets": _target_rows(bocc_refs), "results": _result_rows(bocc_refs)},
            )
    missing = [r for r in row_log["schedule_b_road_exceptions"] if r["status"] != "downloaded"]
    narration.info(
        f"Found {len(row_files)} road right-of-way record(s) and plan set(s)"
        + (
            f"; {len(missing)} road exception(s) cited on the survey couldn't be located "
            "automatically and are listed in the property metadata."
            if missing
            else "."
        )
    )

    # Record per-target results in overview.json.
    ov.merge_section(route_section, {"results": _result_rows(results)})
    saved_paths = [p for _, _, paths in results for p in paths]
    saved_paths += [path for path, _ in glo_files]
    saved_paths += [path for path, _ in row_files]

    # One row per downloaded file, keyed by the filename the Results tab shows,
    # so the frontend can group the grid by category and sort by reception
    # without re-deriving either from the filename. Every route's documents land
    # here — each route writes its own section above, and a surveyor scanning
    # the grid doesn't care which search turned a document up.
    # Date, parties and Book/Page ride along so the Results tab can search on
    # what a title commitment cites ("BK. 571, PG. 55", "1917"), not just the
    # reception number. Book/Page is only known for citations resolved by it.
    book_pages = {rec: bp for bp, rec in ov.get("book_page_resolutions", {}).items()}
    no_doc = _DocRecord("")
    files: list[tuple[str, str, str, _DocRecord]] = [  # (file, doc_type, role, doc)
        *((p.name, doc.doc_type, role, doc) for role, doc, paths in results for p in paths),
        *((p.name, doc_type, "glo_record", no_doc) for p, doc_type in glo_files),
        *((p.name, doc_type, "road_row", no_doc) for p, doc_type in row_files),
    ]
    document_rows = [
        {
            "file": f,
            "reception": doc.reception,
            "doc_type": t,
            "category": classify(t),
            "role": role,
            "rec_date": doc.rec_date,
            "grantor": doc.grantor,
            "grantee": doc.grantee,
            "book_page": book_pages.get(doc.reception, ""),
        }
        for f, t, role, doc in files
    ]
    document_rows.sort(
        key=lambda row: (CATEGORIES.index(row["category"]), reception_sort_key(row["reception"]))
    )
    ov.set_section("documents", document_rows)
    ov.set_section("citations", _citation_summary(ov.get("extracted_ids", []), results))

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
