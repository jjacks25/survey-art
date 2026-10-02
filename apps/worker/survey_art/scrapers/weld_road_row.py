"""State & county road right-of-way packet (SOP Phase 5).

Four public sources, each reached over plain HTTP except the last:

* **Weld parcel + road centerline layers** (ArcGIS, the ones maps.weld.gov/mapanaccount
  draws) — the parcel's own shape, and the roads within 100 ft of it (Step 5.2).
* **BOCC Laserfiche WebLink** (minutes.weld.gov/WebLink) — county road petitions,
  viewers' reports and vacations, indexed by Section/Township/Range on the
  "Commissioner Records" template (Step 5.3). Township/section are stored
  zero-padded (" 05"), and a search for "5" finds nothing.
* **CDOT's route layer + OTIS ROW Plans API** — which state highways run within
  300 ft of the parcel, the milepost where they pass it, and the ROW plan sets
  covering that milepost (Steps 5.4-5.5).
* **CDOT OnBase** (oitco.hylandcloud.com/cdotrmpop) — the plan set itself (Step 5.6).
  OnBase builds a one-time token in its viewer page, so this is the one fetch that
  goes through a browser: open the docpop link and keep the PDF the viewer loads.

Step 5.1 (road ROW exceptions cited by the ALTA's Schedule B-2) is not in here: the
cross-reference walk in `weld_county.py` already downloads every reception and
Book/Page it can resolve. `road_row_references()` just picks out the road ones
for the Phase 5 log.

Measured on R1611986 (S15-T5N-R67W, west Greeley): roads HIGHWAY 34 BYPASS,
HIGHWAY 257 and WCR 56; one BOCC road file (6/24/1936, "HWY257"); US 34 near
MP 102.6 and SH 257 near MP 4.4, whose 1961 plan set S 0057(2) is the same 1961
highway conveyance the parcel's ALTA cites at Book 1583 Page 294.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_ARCGIS = "https://services.arcgis.com/ewjSqmSyHJnkfBLL/arcgis/rest/services"
_PARCELS_URL = f"{_ARCGIS}/Parcels_open_data/FeatureServer/0/query"
_CENTERLINES_URL = f"{_ARCGIS}/Address_Centerlines_open_data/FeatureServer/0/query"
_CDOT_ROUTES_URL = (
    "https://dtdapps.codot.gov/server/rest/services/LRS/Routes_webmerc/MapServer/0/query"
)
_CDOT_ROW_PLANS_URL = "https://dtdapps.codot.gov/otis/API/TRANSYS/RowPlans/{route}/{beg}/{end}"
_WEBLINK = "https://minutes.weld.gov/WebLink"
_WEBLINK_REPO = "CTBIMAGES"

# How close a state highway has to run to count as "abutting". A highway's ROW is
# 50-100 ft either side of centerline, so 300 ft reaches the parcel line.
_HIGHWAY_BUFFER_FT = 300
# Half-width of the milepost window a plan set has to overlap. The milepost is
# read off the route vertex nearest the parcel, not interpolated, so it's approximate.
_MILEPOST_WINDOW = 0.25
# Per-source download caps — a dense section or a long highway corridor can list
# dozens of each. Everything found is still logged in overview.json.
_MAX_BOCC_RECORDS = 10
_MAX_PLANS_PER_ROUTE = 6

# BOCC "Document Type" / "Notes" values that mean a road record: RDF is "Road File
# Only"; notes carry the road ("HWY257", "WCR76").
_BOCC_ROAD_TYPE = re.compile(r"\b(RDF|ROAD|R/W|ROW|RIGHT OF WAY|VACAT|HWY|HIGHWAY)", re.I)
_BOCC_ROAD_NOTE = re.compile(r"\b(HWY|HIGHWAY|WCR|CR ?\d|ROAD VAC|VACAT)", re.I)
# Schedule B-2 exceptions that are road ROW rather than utility easements.
_ROAD_REFERENCE = re.compile(
    r"\b(COUNTY ROADS?|ROAD|HIGHWAY|HWY|DEPARTMENT OF HIGHWAYS"
    # A parcel "conveyed to" the county or state in an ALTA exception is a road
    # taking: "conveyed to County of Weld and State of Colorado ... 1934".
    r"|CONVEYED TO (THE )?(COUNTY OF WELD|WELD COUNTY|STATE OF COLORADO))\b",
    re.I,
)


def _num(value: str) -> str:
    """'5N' -> '5', '67W' -> '67', '015' -> '15'."""
    digits = re.sub(r"\D", "", value or "")
    return str(int(digits)) if digits else ""


async def _parcel_geometry(client: httpx.AsyncClient, account: str) -> dict | None:
    resp = await client.get(
        _PARCELS_URL,
        params={
            "where": f"ACCOUNTNO='{account}'",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "json",
        },
    )
    features = resp.json().get("features", [])
    if not features:
        return None
    return {**features[0]["geometry"], "spatialReference": {"wkid": 4326}}


def _near(geometry: dict, feet: int) -> dict:
    return {
        "geometry": json.dumps(geometry),
        "geometryType": "esriGeometryPolygon",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "distance": feet,
        "units": "esriSRUnit_Foot",
        "f": "json",
    }


async def _abutting_roads(client: httpx.AsyncClient, geometry: dict) -> list[str]:
    """Step 5.2 — road names within 100 ft of the parcel ("WCR 56", "HIGHWAY 257")."""
    resp = await client.post(
        _CENTERLINES_URL,
        data={**_near(geometry, 100), "outFields": "CC_FULLNAME", "returnGeometry": "false"},
    )
    names = {
        (f["attributes"].get("CC_FULLNAME") or "").strip() for f in resp.json().get("features", [])
    }
    return sorted(n for n in names if n)


def _bocc_fields(row: dict) -> dict[str, str]:
    return {
        m["name"]: "\n".join(m["values"] or [])
        for m in row.get("metadata", [])
        if not m.get("isMvfg")
    }


async def _bocc_road_records(
    client: httpx.AsyncClient, section: str, township: str, range_: str
) -> list[dict]:
    """Step 5.3 — BOCC road records indexed against this section."""
    command = (
        f'{{[Commissioner Records]:[Section]="{int(section):02d}",'
        f'[Township]="{int(township):02d}",[Range]="{int(range_):02d}"}}'
    )
    await client.get(f"{_WEBLINK}/Welcome9.aspx")  # sets the session cookie WebLink checks
    resp = await client.post(
        f"{_WEBLINK}/SearchService.aspx/GetSearchListing",
        json={
            "repoName": _WEBLINK_REPO,
            "searchSyn": command,
            "sortColumn": "",
            "startIdx": 0,
            "endIdx": 500,
            "getNewListing": True,
            "sortOrder": 2,
            "displayInGridView": True,
        },
    )
    records = []
    for row in resp.json()["data"]["results"]:
        fields = _bocc_fields(row)
        doc_type, notes = fields.get("Document Type", ""), fields.get("Notes", "")
        if not (_BOCC_ROAD_TYPE.search(doc_type) or _BOCC_ROAD_NOTE.search(notes)):
            continue
        pages = re.search(r"(\d+) pages?", row.get("entryProperties", ""))
        records.append(
            {
                "entry_id": row["entryId"],
                "name": row["name"],
                "doc_type": doc_type,
                "hearing_date": fields.get("Hearing Date", ""),
                "notes": notes,
                "file_location": fields.get("File Location", ""),
                "pages": int(pages[1]) if pages else 1,
                "url": f"{_WEBLINK}/DocView.aspx?id={row['entryId']}&dbid=0&repo={_WEBLINK_REPO}",
            }
        )
    return records


async def _bocc_pdf(client: httpx.AsyncClient, record: dict) -> bytes | None:
    """WebLink renders a PDF on request: generate -> (transition) -> fetch by key."""
    entry = record["entry_id"]
    gen = await client.post(
        f"{_WEBLINK}/GeneratePDF10.aspx",
        params={
            "key": entry,
            "PageRange": f"1 - {record['pages']}",
            "Watermark": 0,
            "repo": _WEBLINK_REPO,
        },
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    key = gen.text.strip().splitlines()[0].strip() if gen.status_code == 200 else ""
    if not key:
        return None
    # Rendering is gradual ("completion": 2, 4, 8 ...); a 48-page road file takes
    # tens of seconds. Fetching before `finished` returns an HTML stub, not a PDF.
    for _ in range(90):
        done = await client.post(
            f"{_WEBLINK}/DocumentService.aspx/PDFTransition", json={"Key": key}
        )
        if done.json().get("data", {}).get("finished"):
            break
        await asyncio.sleep(2)
    pdf = await client.get(f"{_WEBLINK}/PDF10/{key}/{entry}", timeout=120)
    return pdf.content if pdf.content[:5] == b"%PDF-" else None


def _milepost(route_geometry: dict, parcel: dict) -> float | None:
    """Measure at the route vertex nearest any parcel vertex."""
    parcel_pts = [p for ring in parcel["rings"] for p in ring]
    if not parcel_pts:
        return None
    k = math.cos(math.radians(parcel_pts[0][1]))
    best = None
    for path in route_geometry.get("paths", []):
        for x, y, *rest in path:
            if not rest or rest[-1] is None:
                continue
            d = min(math.hypot((x - px) * k, y - py) for px, py, *_ in parcel_pts)
            if best is None or d < best[0]:
                best = (d, rest[-1])
    return best[1] if best else None


async def _state_highway_plans(client: httpx.AsyncClient, geometry: dict) -> list[dict]:
    """Steps 5.4-5.5 — state highways by the parcel, and their ROW plan sets."""
    resp = await client.post(
        _CDOT_ROUTES_URL,
        data={
            **_near(geometry, _HIGHWAY_BUFFER_FT),
            "outFields": "ROUTE",
            "returnGeometry": "true",
            "returnM": "true",
            "outSR": 4326,
        },
    )
    highways = []
    for feature in resp.json().get("features", []):
        route = feature["attributes"]["ROUTE"]
        mp = _milepost(feature["geometry"], geometry)
        if mp is None:
            continue
        beg, end = max(0.0, mp - _MILEPOST_WINDOW), mp + _MILEPOST_WINDOW
        plans_resp = await client.get(
            _CDOT_ROW_PLANS_URL.format(route=route, beg=round(beg, 3), end=round(end, 3))
        )
        plans = [
            {
                "project": p.get("NUMBER") or "",
                "year": p.get("YEAR") or "",
                "begin_mp": p.get("BEG_MP"),
                "end_mp": p.get("END_MP"),
                "url": p.get("LINK2") or "",
            }
            for p in plans_resp.json()
        ]
        highways.append(
            {
                "route": route,
                "highway": f"SH {int(route[:3])}",
                "milepost": round(mp, 2),
                "plans": plans,
            }
        )
    return highways


async def _onbase_pdfs(urls: list[str]) -> dict[str, bytes]:
    """Step 5.6 — OnBase plan PDFs, one browser for the batch."""
    from playwright.async_api import async_playwright

    found: dict[str, bytes] = {}
    if not urls:
        return found
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            for url in urls:
                try:
                    async with page.expect_response(
                        lambda r: "PdfHandler.ashx" in r.url and r.status == 200,
                        timeout=120_000,
                    ) as info:
                        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    # The viewer's own response body is gone by the time we can
                    # read it (it's handed to the plugin frame); its URL carries a
                    # one-time token that's still good, so fetch it again.
                    pdf_url = (await info.value).url
                    body = await (await page.context.request.get(pdf_url, timeout=120_000)).body()
                    if body[:5] == b"%PDF-":
                        found[url] = body
                except Exception as exc:
                    logger.warning("OnBase plan %s: %s", url, exc)
        finally:
            await browser.close()
    return found


def road_row_references(
    extracted_ids: list[dict], book_pages: dict[str, str], downloaded: set[str]
) -> list[dict]:
    """Steps 5.1/5.7 — the road ROW exceptions among the IDs the run's documents
    cite, each marked with whether this run found it. `book_pages` maps a cited
    Book/Page to the reception it resolved to; `downloaded` is every reception on
    disk. One not found is flagged, not fatal: pre-1893, another county, or a
    Book/Page the recorder never indexed (the SOP's own failure list)."""
    rows = []
    for row in extracted_ids:
        if not _ROAD_REFERENCE.search(row.get("context", "")):
            continue
        reception = book_pages.get(row.get("id", ""), row.get("id", ""))
        rows.append(
            {
                **{k: row.get(k, "") for k in ("id", "id_type", "context", "source_reception")},
                "status": "downloaded" if reception in downloaded else "not located",
            }
        )
    return rows


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


async def fetch_road_row(
    *, account: str, section: str, township: str, range_: str, dest_dir: Path
) -> tuple[list[tuple[Path, str]], dict]:
    """Run Phase 5 for one parcel. Returns `([(path, doc_type)], log)` where `log`
    is the Step 5.7 record for overview.json. Never raises — each source that
    fails is noted in the log and the rest still run."""
    log: dict = {"abutting_roads": [], "bocc_road_records": [], "state_highways": [], "errors": []}
    saved: list[tuple[Path, str]] = []
    dest_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(
        timeout=60, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
    ) as client:
        try:
            geometry = await _parcel_geometry(client, account)
        except Exception as exc:
            geometry = None
            log["errors"].append(f"parcel shape: {exc}")

        if geometry:
            try:
                log["abutting_roads"] = await _abutting_roads(client, geometry)
            except Exception as exc:
                log["errors"].append(f"road centerlines: {exc}")

        sec, twp, rng = _num(section), _num(township), _num(range_)
        if sec and twp and rng:
            try:
                records = await _bocc_road_records(client, sec, twp, rng)
                for record in records[:_MAX_BOCC_RECORDS]:
                    pdf = await _bocc_pdf(client, record)
                    if pdf:
                        path = dest_dir / f"county_road_row_{_safe(record['name'])}.pdf"
                        path.write_bytes(pdf)
                        record["file"] = path.name
                        saved.append((path, f"County Road Right of Way — {record['doc_type']}"))
                log["bocc_road_records"] = records
            except Exception as exc:
                log["errors"].append(f"BOCC records: {exc}")

        if geometry:
            try:
                log["state_highways"] = await _state_highway_plans(client, geometry)
            except Exception as exc:
                log["errors"].append(f"CDOT OTIS: {exc}")

    wanted = [
        plan
        for hw in log["state_highways"]
        for plan in sorted(hw["plans"], key=lambda p: str(p["year"]), reverse=True)[
            :_MAX_PLANS_PER_ROUTE
        ]
        if plan["url"]
    ]
    try:
        pdfs = await _onbase_pdfs([p["url"] for p in wanted])
    except Exception as exc:
        pdfs = {}
        log["errors"].append(f"CDOT OnBase: {exc}")
    for hw in log["state_highways"]:
        for plan in hw["plans"]:
            if plan["url"] in pdfs:
                path = dest_dir / f"state_highway_row_{hw['route']}_{_safe(plan['project'])}.pdf"
                path.write_bytes(pdfs[plan["url"]])
                plan["file"] = path.name
                saved.append(
                    (path, f"State Highway Right of Way Plan — {hw['highway']} {plan['project']}")
                )

    logger.info(
        "Phase 5: %d road(s), %d BOCC road record(s), %d state highway(s), %d file(s)",
        len(log["abutting_roads"]),
        len(log["bocc_road_records"]),
        len(log["state_highways"]),
        len(saved),
    )
    return saved, log
