"""Tests for _archive_full_log() — best-effort S3 archive of a job's full log."""

from __future__ import annotations

from survey_art.worker import _archive_full_log
from survey_shared.jobs import COMPLETED, Job, LogEntry


def _job(**overrides) -> Job:
    base = dict(
        job_id="job-1",
        address="123 Main St",
        county="CO_weld",
        status=COMPLETED,
        created_at=100,
        updated_at=100,
        logs=[
            LogEntry(message="Starting your search...", kind="milestone"),
            LogEntry(message="Advanced Search: 3 row(s)", kind="detail"),
        ],
    )
    base.update(overrides)
    return Job(**base)


def test_uploads_header_and_every_log_line(monkeypatch):
    calls = []
    monkeypatch.setattr("survey_art.worker.jobs.get_job", lambda job_id: _job())
    monkeypatch.setattr(
        "survey_art.worker.jobs.upload_job_log", lambda job_id, text: calls.append((job_id, text))
    )

    _archive_full_log("job-1")

    assert len(calls) == 1
    job_id, text = calls[0]
    assert job_id == "job-1"
    assert "Address: 123 Main St" in text
    assert "Status: COMPLETED" in text
    assert "[milestone] Starting your search..." in text
    assert "[detail] Advanced Search: 3 row(s)" in text


def test_includes_error_when_set(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "survey_art.worker.jobs.get_job", lambda job_id: _job(status="FAILED", error="boom")
    )
    monkeypatch.setattr(
        "survey_art.worker.jobs.upload_job_log", lambda job_id, text: calls.append((job_id, text))
    )

    _archive_full_log("job-1")

    assert "Error: boom" in calls[0][1]


def test_skips_upload_when_job_missing(monkeypatch):
    calls = []
    monkeypatch.setattr("survey_art.worker.jobs.get_job", lambda job_id: None)
    monkeypatch.setattr(
        "survey_art.worker.jobs.upload_job_log", lambda job_id, text: calls.append((job_id, text))
    )

    _archive_full_log("job-missing")

    assert calls == []


def test_never_raises_when_upload_fails(monkeypatch):
    monkeypatch.setattr("survey_art.worker.jobs.get_job", lambda job_id: _job())

    def _boom(job_id, text):
        raise RuntimeError("S3 is down")

    monkeypatch.setattr("survey_art.worker.jobs.upload_job_log", _boom)

    _archive_full_log("job-1")  # must not raise
