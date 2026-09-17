"""Smoke tests for the FastAPI job-broker endpoints against mocked AWS."""

from __future__ import annotations


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_and_get_job(client):
    created = client.post("/api/jobs", json={"address": "123 Main St, Greeley, CO 80631"})
    assert created.status_code == 202
    job_id = created.json()["jobId"]
    assert created.json()["status"] == "PENDING"

    fetched = client.get(f"/api/jobs/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json()["address"] == "123 Main St, Greeley, CO 80631"


def test_list_jobs_includes_created_jobs(client):
    first = client.post("/api/jobs", json={"address": "111 First St, Greeley, CO 80631"})
    second = client.post("/api/jobs", json={"address": "222 Second St, Greeley, CO 80631"})

    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    job_ids = {j["jobId"] for j in resp.json()["jobs"]}
    assert {first.json()["jobId"], second.json()["jobId"]} <= job_ids


def test_list_jobs_sorts_most_recent_first(client):
    from survey_shared import jobs

    jobs.create_job("older", address="1 Old St", county="CO_weld")
    jobs.create_job("newer", address="2 New St", county="CO_weld")
    jobs._table().update_item(
        Key={"jobId": "older"},
        UpdateExpression="SET createdAt = :t",
        ExpressionAttributeValues={":t": 1000},
    )
    jobs._table().update_item(
        Key={"jobId": "newer"},
        UpdateExpression="SET createdAt = :t",
        ExpressionAttributeValues={":t": 2000},
    )

    resp = client.get("/api/jobs")
    job_ids = [j["jobId"] for j in resp.json()["jobs"]]
    assert job_ids.index("newer") < job_ids.index("older")


def test_get_job_returns_kind_tagged_logs(client):
    from survey_shared import jobs

    created = client.post("/api/jobs", json={"address": "123 Main St, Greeley, CO 80631"})
    job_id = created.json()["jobId"]
    jobs.append_log(job_id, "Starting your search...", kind="milestone")
    jobs.append_log(job_id, "Phase 1: routing account lookup", kind="detail")

    fetched = client.get(f"/api/jobs/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json()["logs"] == [
        {"message": "Starting your search...", "kind": "milestone"},
        {"message": "Phase 1: routing account lookup", "kind": "detail"},
    ]


def test_get_job_not_found(client):
    resp = client.get("/api/jobs/does-not-exist")
    assert resp.status_code == 404


def test_get_files_not_found(client):
    resp = client.get("/api/jobs/does-not-exist/files")
    assert resp.status_code == 404


def test_cancel_pending_job(client):
    created = client.post("/api/jobs", json={"address": "123 Main St, Greeley, CO 80631"})
    job_id = created.json()["jobId"]

    resp = client.delete(f"/api/jobs/{job_id}")
    assert resp.status_code == 204

    fetched = client.get(f"/api/jobs/{job_id}")
    assert fetched.json()["status"] == "CANCELLED"


def test_delete_unknown_job_returns_404(client):
    resp = client.delete("/api/jobs/does-not-exist")
    assert resp.status_code == 404


def test_delete_completed_job_removes_record(client):
    """DELETE on a terminal job deletes its record outright rather than
    cancelling — there's nothing left to cancel."""
    from survey_shared import jobs

    created = client.post("/api/jobs", json={"address": "123 Main St, Greeley, CO 80631"})
    job_id = created.json()["jobId"]
    jobs.update_status(job_id, jobs.COMPLETED, file_count=0)

    resp = client.delete(f"/api/jobs/{job_id}")
    assert resp.status_code == 204

    fetched = client.get(f"/api/jobs/{job_id}")
    assert fetched.status_code == 404


def test_update_status_drops_oversized_metadata_instead_of_failing(client):
    """Regression: a DynamoDB item over the 400KB cap (e.g. a big
    `extracted_ids` list) previously made the whole job report FAILED with
    the raw AWS error, even though the scrape itself succeeded and its
    documents were already uploaded. update_status() should retry without
    metadata and still record the real terminal status."""
    from survey_shared import jobs

    created = client.post("/api/jobs", json={"address": "123 Main St, Greeley, CO 80631"})
    job_id = created.json()["jobId"]

    oversized = {"extracted_ids": [{"context": "x" * 1000} for _ in range(500)]}
    jobs.update_status(job_id, jobs.COMPLETED, file_count=3, metadata=oversized)

    fetched = client.get(f"/api/jobs/{job_id}")
    assert fetched.json()["status"] == "COMPLETED"
    assert fetched.json()["fileCount"] == 3
    assert fetched.json().get("metadata") is None


def test_result_files_expose_inline_and_attachment_urls(client):
    """The Results tab previews `url` in an iframe and saves `downloadUrl`."""
    from survey_shared import aws, jobs

    aws.client("s3").put_object(
        Bucket=aws.storage_bucket(),
        Key="documents/co/weld/R1611986/exception_1766551.pdf",
        Body=b"%PDF-1.4",
    )

    (entry,) = jobs.list_result_files("co/weld/R1611986")

    assert entry["name"] == "exception_1766551.pdf"
    assert "response-content-disposition" not in entry["url"].lower()
    assert "attachment" in entry["downloadUrl"].lower()
    assert "exception_1766551.pdf" in entry["downloadUrl"]
