"""End to end: a search submitted through the API is queued, run by the worker,
and read back through the API — against moto, with only the county scraper
(network + browser) and the map geocode stubbed out.

This is the path a surveyor's search actually takes, so it pins the contracts
between the three packages at once: the SQS message the API sends is the one
the worker reads, the documents the worker uploads are the ones the API lists,
and every exit path leaves a terminal status, a cost breakdown and an archived log.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from PIL import Image

from survey_art import worker
from survey_art.geocode import County, GeocodedAddress
from survey_shared import aws

narration = logging.getLogger("survey_art.narration")


@pytest.fixture
def client(aws_env):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _stub_map_geocode(monkeypatch):
    monkeypatch.setattr(
        worker,
        "address_to_county",
        lambda address: GeocodedAddress(
            street=address,
            city="Greeley",
            state="CO",
            zip_code="80631",
            county=County("CO", "Weld"),
            lat=40.42,
            lon=-104.71,
        ),
    )


def _use_scraper(monkeypatch, scrape) -> None:
    monkeypatch.setitem(worker.run_async.__globals__["COUNTY_SCRAPERS"], "CO_weld", scrape)


async def _process_next_message(aws_env) -> str:
    """Do what the local poll loop does with one queued job; returns its id."""
    sqs = aws.client("sqs")
    (msg,) = sqs.receive_message(QueueUrl=aws_env["queue_url"])["Messages"]
    body = json.loads(msg["Body"])
    assert await worker.run_job(body["jobId"], body["address"], body["county"] or None) in (0, 1)
    return body["jobId"]


async def test_a_search_runs_from_submission_to_downloadable_results(client, aws_env, monkeypatch):
    async def scrape(geocoded, tmp_dir, **_kwargs):
        dest = tmp_dir / geocoded.county.key() / "R1611986"
        dest.mkdir(parents=True)
        pdf = dest / "alta_4571638.pdf"
        Image.new("1", (400, 300), 1).save(pdf, "PDF")  # a bitonal "scan", like the county's
        (dest / "overview.json").write_text(
            json.dumps(
                {
                    "identify_results": {"account": "R1611986", "address": "1 Main St"},
                    "land_information": {"acres": 2.5},
                }
            )
        )
        narration.info("Found the property — account R1611986.")
        return [pdf], None, 0.0, 1000, 200

    _use_scraper(monkeypatch, scrape)

    created = client.post("/api/jobs", json={"address": "R1611986", "county": "CO_weld"})
    assert created.status_code == 202
    job_id = await _process_next_message(aws_env)
    assert job_id == created.json()["jobId"]

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "COMPLETED"
    assert job["fileCount"] == 1
    assert job["docPrefix"] == "co/weld/1_Main_St"
    assert job["metadata"]["identify_results"]["account"] == "R1611986"
    assert job["metadata"]["land_information"]["acres"] == 2.5
    assert job["location"] == {"lat": 40.42, "lon": -104.71}
    milestones = [entry["message"] for entry in job["logs"] if entry["kind"] == "milestone"]
    assert "Found the property — account R1611986." in milestones
    assert milestones[-1] == "All done — found 1 document(s) for this property."
    costs = {line["key"]: line["usd"] for line in job["costs"]}
    assert costs.keys() >= {"bedrock", "fargate", "s3"}
    # Unpriced tokens fall back to the rate card, so a Weld run still shows its model cost.
    assert costs["bedrock"] > 0

    (entry,) = client.get(f"/api/jobs/{job_id}/files").json()["files"]
    assert entry["name"] == "alta_4571638.pdf"
    assert "attachment" in entry["downloadUrl"].lower()
    assert entry["thumbnailUrl"]

    log = (
        aws.client("s3")
        .get_object(Bucket=aws.storage_bucket(), Key=f"property-search-logs/{job_id}.log")["Body"]
        .read()
        .decode()
    )
    assert "Status: COMPLETED" in log
    assert "[milestone] Found the property — account R1611986." in log


async def test_a_failed_scrape_is_recorded_with_its_error_and_cost(client, aws_env, monkeypatch):
    async def scrape(geocoded, tmp_dir, **_kwargs):
        return [], "Parcel resolve failed: could not resolve R0000000 to a Weld parcel.", 0, 0, 0

    _use_scraper(monkeypatch, scrape)

    client.post("/api/jobs", json={"address": "R0000000", "county": "CO_weld"})
    job_id = await _process_next_message(aws_env)

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "FAILED"
    assert job["error"].startswith("Parcel resolve failed")
    assert job["costs"]
    assert client.get(f"/api/jobs/{job_id}/files").json()["files"] == []
    assert "Error: Parcel resolve failed" in (
        aws.client("s3")
        .get_object(Bucket=aws.storage_bucket(), Key=f"property-search-logs/{job_id}.log")["Body"]
        .read()
        .decode()
    )


async def test_a_crashing_scraper_still_reaches_a_terminal_state(client, aws_env, monkeypatch):
    async def scrape(geocoded, tmp_dir, **_kwargs):
        raise RuntimeError("county site changed its layout")

    _use_scraper(monkeypatch, scrape)

    client.post("/api/jobs", json={"address": "R1611986", "county": "CO_weld"})
    job_id = await _process_next_message(aws_env)

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "FAILED"
    assert job["error"] == "county site changed its layout"


# --- The real Weld scraper, with only its network edges faked. ---
#
# R1611986 is the SOP's worked example (S15-T5N-R67W): Document History holds an
# ALTA and a vesting deed, so the run takes the direct-extraction route, reads the
# ALTA's Schedule B-2 for what it cites, then runs Phase 4 (GLO) and Phase 5 (road
# right-of-way). What's faked is where the run leaves the process: the county's
# HTTP endpoints, the recorder's browser download, Bedrock's document reads, and
# the GLO browser agent.

_ALTA_CITES = [
    # (id, id_type, context) — the two road exceptions from the SOP's Step 5.1.
    ("1511418", "reception_number", "right of way for Colorado State Highway No. 16"),
    ("Book 86 Page 273", "book_page", "Rights of way for County Roads, 30 feet on either side"),
]
# The BOCC road file cites the recorder's own deed for the road (SOP Step 5.3).
_BOCC_CITES = [("2661201", "reception_number", "Quit Claim Deed for R/W in Sec 28")]


def _pdf(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("1", (40, 30), 1).save(path, "PDF")
    return path


def _pdf_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("1", (40, 30), 1).save(buf, "PDF")
    return buf.getvalue()


def _weld_county_http(request):
    """The ArcGIS layers, BOCC WebLink and CDOT OTIS, as Phase 5 calls them."""
    import httpx

    url = str(request.url)
    parcel = {"rings": [[[-104.87, 40.40], [-104.87, 40.41], [-104.86, 40.41], [-104.87, 40.40]]]}
    if "Parcels_open_data" in url:
        return httpx.Response(200, json={"features": [{"geometry": parcel}]})
    if "Address_Centerlines" in url:
        names = ["WCR 56", "HIGHWAY 257", "WCR 56"]
        return httpx.Response(
            200, json={"features": [{"attributes": {"CC_FULLNAME": n}} for n in names]}
        )
    if "Welcome9.aspx" in url:
        return httpx.Response(200, text="")
    if "GetSearchListing" in url:
        # Zero-padded S/T/R is the only form WebLink matches.
        syntax = json.loads(request.content)["searchSyn"]
        assert '[Section]="15",[Township]="05",[Range]="67"' in syntax
        row = lambda entry, doc_type, notes: {  # noqa: E731
            "entryId": entry,
            "name": f"BOCC {entry}",
            "entryProperties": "3 pages",
            "metadata": [
                {"name": "Document Type", "values": [doc_type]},
                {"name": "Notes", "values": [notes]},
                {"name": "Hearing Date", "values": ["6/24/1936"]},
            ],
        }
        rows = [row(60007, "RDF", "HWY257"), row(60008, "MINUTES", "budget hearing")]
        return httpx.Response(200, json={"data": {"results": rows}})
    if "GeneratePDF10" in url:
        return httpx.Response(200, text="pdfkey\n")
    if "PDFTransition" in url:
        return httpx.Response(200, json={"data": {"finished": True}})
    if "/PDF10/" in url:
        return httpx.Response(200, content=_pdf_bytes())
    if "Routes_webmerc" in url:
        path = [[-104.90, 40.40, 1.0], [-104.871, 40.405, 4.4], [-104.80, 40.40, 9.0]]
        return httpx.Response(
            200,
            json={"features": [{"attributes": {"ROUTE": "257A"}, "geometry": {"paths": [path]}}]},
        )
    if "RowPlans/257A" in url:
        plan = {"NUMBER": "S 0057(2)", "YEAR": 1961, "BEG_MP": 3.9, "END_MP": 5.0}
        return httpx.Response(200, json=[{**plan, "LINK2": "https://onbase/plan1"}])
    raise AssertionError(f"unexpected request: {url}")


@pytest.fixture
def weld_offline(monkeypatch, tmp_path):
    """Fake the Weld run's network edges; everything between them is real."""
    import httpx

    from survey_art.id_extraction import ExtractedId, IdExtraction
    from survey_art.scrapers import glo_records, weld_county, weld_road_row
    from survey_art.settings import get_settings

    # No recorder login, whatever a developer's .env says: Advanced Search and
    # Book/Page resolution then skip, as they do when the county account is missing.
    monkeypatch.setenv("WELD_RECORDER_USERNAME", "")
    get_settings.cache_clear()

    async def resolve_parcel(geocoded, **_):
        return weld_county.ParcelInfo(
            account="R1611986",
            parcel_id="095715000012",
            owner="STRATUS DELANTERO LLC",
            section="15",
            township="5N",
            range_="67W",
        )

    history = [
        weld_county._DocRecord("4571638", rec_date="03/01/2020", doc_type="SURV"),
        weld_county._DocRecord("4571640", rec_date="03/02/2020", doc_type="WD"),
    ]
    downloaded: list[str] = []

    async def download_documents(targets, dest):
        downloaded.extend(doc.reception for _, doc in targets)
        return [(role, doc, [_pdf(dest / f"{role}_{doc.reception}.pdf")]) for role, doc in targets]

    def extract_document_ids(path):
        cites = {"alta_4571638.pdf": _ALTA_CITES, "county_road_row_BOCC_60007.pdf": _BOCC_CITES}
        ids = [ExtractedId(id=i, id_type=t, context=c) for i, t, c in cites.get(path.name, [])]
        return IdExtraction(ids=ids, source="text_layer" if ids else "none")

    async def no_map(account, dest):
        return None

    monkeypatch.setattr(weld_county, "_resolve_parcel", resolve_parcel)
    monkeypatch.setattr(weld_county, "_fetch_property_report_html", lambda account: "")
    monkeypatch.setattr(weld_county, "_fetch_document_history", lambda account: history)
    monkeypatch.setattr(weld_county, "_capture_map_image", no_map)
    monkeypatch.setattr(weld_county, "_download_documents", download_documents)
    monkeypatch.setattr(weld_county, "extract_document_ids", extract_document_ids)

    # Phase 4: the GLO browser agent "downloads" a plat and a field-note page.
    agent_dir = tmp_path / "agent-downloads"
    agent = type(
        "Agent",
        (),
        {
            "available_file_paths": [
                str(_pdf(agent_dir / "230823.pdf")),
                str(_pdf(agent_dir / "R0386_112.pdf")),
            ]
        },
    )()
    answer = "PLAT | 2380 | 230823.pdf\nFIELD_NOTES | Vol. R0386 pages 112-118 | R0386_112.pdf"
    prompts: list[str] = []

    async def run_agent(task):
        prompts.append(task)
        return agent, answer, (0.05, 1000, 100)

    monkeypatch.setattr(glo_records, "run_agent", run_agent)

    # Phase 5: county/CDOT HTTP through a mock transport; OnBase is a browser.
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        weld_road_row.httpx,
        "AsyncClient",
        lambda **kw: real_client(**kw, transport=httpx.MockTransport(_weld_county_http)),
    )

    async def onbase_pdfs(urls):
        return {u: _pdf_bytes() for u in urls}

    monkeypatch.setattr(weld_road_row, "_onbase_pdfs", onbase_pdfs)
    yield {"downloaded": downloaded, "glo_prompts": prompts}
    get_settings.cache_clear()


async def test_a_weld_search_collects_the_sop_packet(client, aws_env, weld_offline):
    client.post("/api/jobs", json={"address": "R1611986", "county": "CO_weld"})
    job_id = await _process_next_message(aws_env)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "COMPLETED", job.get("error")
    meta = job["metadata"]

    # Phase 3 + Step 5.1: the ALTA and deed, then the reception the ALTA cites.
    # The BOCC road file's own citation (Step 5.3) is fetched the same way.
    assert weld_offline["downloaded"] == ["4571638", "4571640", "1511418", "2661201"]
    assert meta["decision_matrix"]["path"] == "direct"

    # Phase 4: searched by the parcel's S/T/R, files named for the township,
    # and the Step 4.7 log carries what the agent reported.
    (prompt,) = weld_offline["glo_prompts"]
    assert "Township=5 N, Range=67 W, Section=15" in prompt
    glo = meta["glo_records"]
    assert glo["files"] == ["glo_survey_plat_T5NR67W.pdf", "glo_field_notes_T5NR67W.pdf"]
    assert {"kind": "GLO Survey Field Notes", "reference": "Vol. R0386 pages 112-118"} in glo[
        "records"
    ]
    assert "original survey of record" in glo["note"]

    # Phase 5: abutting roads, the BOCC road file (not the budget minutes), the
    # state highway's plan set, and each Schedule B-2 road exception's status.
    row = meta["road_right_of_way"]
    assert row["abutting_roads"] == ["HIGHWAY 257", "WCR 56"]
    assert [r["file"] for r in row["bocc_road_records"]] == ["county_road_row_BOCC_60007.pdf"]
    (highway,) = row["state_highways"]
    assert (highway["highway"], highway["milepost"]) == ("SH 257", 4.4)
    assert highway["plans"][0]["file"] == "state_highway_row_257A_S_0057_2.pdf"
    status = {r["id"]: r["status"] for r in row["schedule_b_road_exceptions"]}
    # No recorder login, so the 1889 Book/Page can't be resolved — flagged, not fatal.
    assert status == {"1511418": "downloaded", "Book 86 Page 273": "not located"}
    assert row["errors"] == []

    # Everything above reaches the surveyor as a downloadable, categorised file.
    files = {f["name"] for f in client.get(f"/api/jobs/{job_id}/files").json()["files"]}
    assert files >= {
        "alta_4571638.pdf",
        "vesting_deed_4571640.pdf",
        "exception_1511418.pdf",
        "exception_2661201.pdf",
        "glo_survey_plat_T5NR67W.pdf",
        "glo_field_notes_T5NR67W.pdf",
        "county_road_row_BOCC_60007.pdf",
        "state_highway_row_257A_S_0057_2.pdf",
    }
    category = {d["file"]: d["category"] for d in meta["documents"]}
    assert category["glo_field_notes_T5NR67W.pdf"] == "Surveys & Plats"
    assert category["state_highway_row_257A_S_0057_2.pdf"] == "Easements & Rights of Way"


# --- CAD drawing: a second job over a finished search's documents. ---


async def test_a_cad_drawing_runs_over_a_finished_search(client, aws_env, monkeypatch):
    from survey_art import deed_plot, plss
    from survey_art.deed_parse import DeedParse, DocumentExtract

    truth = json.loads((Path(__file__).parent / "deed_ground_truth.json").read_text())

    async def scrape(geocoded, tmp_dir, **_kwargs):
        dest = tmp_dir / geocoded.county.key() / "R1611986"
        saved = [
            _pdf(dest / name)
            for name in ("vesting_deed_4970002.pdf", "exception_1715553.pdf", "alta_4571638.pdf")
        ]
        (dest / "overview.json").write_text(
            json.dumps({"identify_results": {"address": "1 Main St"}})
        )
        return saved, None, 0.0, 0, 0

    def read_document(doc, **_):
        descriptions = truth["documents"][doc.reception]["descriptions"]
        return DeedParse(
            extract=DocumentExtract.model_validate({"descriptions": descriptions}),
            source="bedrock",
            input_tokens=5000,
            output_tokens=500,
            cost_usd=0.02,
        )

    def square(state, meridian, township, range_, section):
        x0 = 3_170_000 - (section - 15) * 5280
        return plss.Quad.from_corners(
            (x0, 1_391_640), (x0 + 5280, 1_391_640), (x0 + 5280, 1_386_360), (x0, 1_386_360)
        )

    _use_scraper(monkeypatch, scrape)
    monkeypatch.setattr(deed_plot, "read_document", read_document)
    monkeypatch.setattr(plss, "fetch_section", square)

    client.post("/api/jobs", json={"address": "R1611986", "county": "CO_weld"})
    search_id = await _process_next_message(aws_env)

    # Not before the search is done, and not for a job that doesn't exist.
    assert client.post("/api/jobs/nope/drawing").status_code == 404

    created = client.post(f"/api/jobs/{search_id}/drawing")
    assert created.status_code == 202
    drawing_id = await _process_next_message(aws_env)
    assert drawing_id == created.json()["jobId"]

    drawing = client.get(f"/api/jobs/{drawing_id}").json()
    assert drawing["status"] == "COMPLETED", drawing.get("error")
    assert drawing["kind"] == "drawing"
    assert drawing["sourceJobId"] == search_id
    assert client.get(f"/api/jobs/{search_id}").json()["drawingJobId"] == drawing_id

    rows = {d["reception"]: d for d in drawing["metadata"]["drawing"]["documents"]}
    assert set(rows) == {"4970002", "1715553"}  # the ALTA isn't plotted
    assert rows["1715553"]["descriptions"][0]["status"] == "ok"
    costs = {line["key"]: line["usd"] for line in drawing["costs"]}
    assert costs["bedrock"] == pytest.approx(0.04)

    names = {f["name"] for f in client.get(f"/api/jobs/{drawing_id}/files").json()["files"]}
    assert names == {"1_Main_St_deeds.dxf", "1_Main_St_points.csv", "1_Main_St_qc_report.csv",
                     "qc.json"}  # fmt: skip
    # ...and none of it leaks into the search's own document grid or the sidebar.
    search_files = {f["name"] for f in client.get(f"/api/jobs/{search_id}/files").json()["files"]}
    assert search_files == {"vesting_deed_4970002.pdf", "exception_1715553.pdf",
                            "alta_4571638.pdf"}  # fmt: skip
    assert [j["jobId"] for j in client.get("/api/jobs").json()["jobs"]] == [search_id]

    # A drawing is made from a search, never from another drawing.
    assert client.post(f"/api/jobs/{drawing_id}/drawing").status_code == 409
