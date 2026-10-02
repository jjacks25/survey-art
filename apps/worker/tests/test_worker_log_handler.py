"""Tests for _DynamoLogHandler — tagging narration vs. diagnostic log records."""

from __future__ import annotations

import logging

import pytest

from survey_art import worker
from survey_art.worker import _DynamoLogHandler


def _record(logger_name: str, message: str) -> logging.LogRecord:
    return logging.getLogger(logger_name).makeRecord(
        logger_name, logging.INFO, __file__, 0, message, (), None
    )


@pytest.fixture
def appended(monkeypatch) -> list[tuple[str, str, str]]:
    """Every `(job_id, message, kind)` the handler writes to the job record."""
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "survey_art.worker.jobs.append_log",
        lambda job_id, message, *, kind="detail": calls.append((job_id, message, kind)),
    )
    return calls


def test_narration_is_milestone_and_everything_else_is_detail(appended):
    handler = _DynamoLogHandler("job-1")
    handler.emit(_record("survey_art.narration", "Found the property."))
    handler.emit(_record("survey_art.scrapers.weld_county", "Advanced Search: 3 row(s)"))

    assert appended == [
        ("job-1", "Found the property.", "milestone"),
        ("job-1", "Advanced Search: 3 row(s)", "detail"),
    ]
    # The cost breakdown prices DynamoDB writes and CloudWatch ingestion off these.
    assert (handler.appends, handler.bytes) == (
        2,
        len("Found the property.Advanced Search: 3 row(s)"),
    )


def test_detail_stops_at_the_budget_but_milestones_keep_going(appended, monkeypatch):
    """The log lives in a 400 KB DynamoDB item that the final status update also
    has to fit in — overflowing it left a finished job stuck RUNNING."""
    monkeypatch.setattr("survey_art.worker._LOG_DETAIL_BUDGET_BYTES", 10)

    handler = _DynamoLogHandler("job-1")
    for message in ("0123456789", "dropped", "also dropped"):
        handler.emit(_record("survey_art.scrapers.weld_county", message))
    handler.emit(_record("survey_art.narration", "Done."))

    assert [m for _, m, _ in appended] == ["0123456789", worker._LOG_TRUNCATED_NOTICE, "Done."]
    assert appended[-1][2] == "milestone"
