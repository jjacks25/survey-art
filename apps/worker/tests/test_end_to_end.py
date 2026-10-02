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
