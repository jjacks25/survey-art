"""Tests for the shared pydantic Job model (DynamoDB item round-trip)."""

from __future__ import annotations

from decimal import Decimal

from survey_shared.jobs import COMPLETED, PENDING, Job


def _job(**overrides) -> Job:
    base = dict(
        job_id="abc123",
        address="123 Main St",
        county="CO_weld",
        status=PENDING,
        created_at=100,
        updated_at=100,
    )
    base.update(overrides)
    return Job(**base)


class TestToItem:
    def test_uses_dynamo_alias_keys(self):
        item = _job().to_item()
        assert item["jobId"] == "abc123"
        assert item["createdAt"] == 100
        assert item["fileCount"] == 0

    def test_omits_error_when_unset(self):
        assert "error" not in _job().to_item()

    def test_includes_error_when_set(self):
        assert _job(status=COMPLETED, error="boom").to_item()["error"] == "boom"

    def test_logs_default_to_empty_list(self):
        assert _job().to_item()["logs"] == []

    def test_omits_metadata_when_unset(self):
        assert "metadata" not in _job().to_item()

    def test_includes_metadata_when_set(self):
        item = _job(metadata={"owner": "Jane Doe"}).to_item()
        assert item["metadata"] == {"owner": "Jane Doe"}


class TestFromItem:
    def test_round_trip(self):
        item = _job(file_count=3).to_item()
        restored = Job.from_item(item)
        assert restored.job_id == "abc123"
        assert restored.file_count == 3

    def test_coerces_dynamo_decimals(self):
        # DynamoDB returns numbers as Decimal; the int fields must accept them.
        item = {
            "jobId": "x",
            "address": "a",
            "county": "CO_weld",
            "status": COMPLETED,
            "createdAt": Decimal("111"),
            "updatedAt": Decimal("222"),
            "fileCount": Decimal("5"),
        }
        job = Job.from_item(item)
        assert job.created_at == 111
        assert job.file_count == 5
