"""Tests for _DynamoLogHandler — tagging narration vs. diagnostic log records."""

from __future__ import annotations

import logging

from survey_art import worker
from survey_art.worker import _DynamoLogHandler


def _record(logger_name: str, message: str) -> logging.LogRecord:
    return logging.getLogger(logger_name).makeRecord(
        logger_name, logging.INFO, __file__, 0, message, (), None
    )


def test_narration_records_are_tagged_milestone(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "survey_art.worker.jobs.append_log",
        lambda job_id, message, *, kind="detail": calls.append((job_id, message, kind)),
    )

    handler = _DynamoLogHandler("job-1")
    handler.emit(_record("survey_art.narration", "Found the property."))

    assert calls == [("job-1", "Found the property.", "milestone")]
    # The cost breakdown prices DynamoDB writes and CloudWatch ingestion off these.
    assert (handler.appends, handler.bytes) == (1, len("Found the property."))


def test_other_loggers_are_tagged_detail(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "survey_art.worker.jobs.append_log",
        lambda job_id, message, *, kind="detail": calls.append((job_id, message, kind)),
    )

    handler = _DynamoLogHandler("job-1")
    handler.emit(_record("survey_art.scrapers.weld_county", "Advanced Search: 3 row(s)"))

    assert calls == [("job-1", "Advanced Search: 3 row(s)", "detail")]


def test_detail_stops_at_the_budget_but_milestones_keep_going(monkeypatch):
    """The log lives in a 400 KB DynamoDB item that the final status update also
    has to fit in — overflowing it left a finished job stuck RUNNING."""
    calls = []
    monkeypatch.setattr(
        "survey_art.worker.jobs.append_log",
        lambda job_id, message, *, kind="detail": calls.append((message, kind)),
    )
    monkeypatch.setattr("survey_art.worker._LOG_DETAIL_BUDGET_BYTES", 10)

    handler = _DynamoLogHandler("job-1")
    for message in ("0123456789", "dropped", "also dropped"):
        handler.emit(_record("survey_art.scrapers.weld_county", message))
    handler.emit(_record("survey_art.narration", "Done."))

    assert [m for m, _ in calls[:2]] == ["0123456789", worker._LOG_TRUNCATED_NOTICE]
    assert calls[2:] == [("Done.", "milestone")]
