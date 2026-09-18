"""Tests for the shared pydantic Job model (DynamoDB item round-trip)."""

from __future__ import annotations

from decimal import Decimal

from survey_shared.jobs import COMPLETED, PENDING, Job, LogEntry


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

    def test_omits_expires_at_when_unset(self):
        assert "expiresAt" not in _job().to_item()

    def test_includes_expires_at_when_set(self):
        assert _job(expires_at=200).to_item()["expiresAt"] == 200


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

    def test_cost_lines_coerce_dynamo_decimals_to_float(self):
        # `costs` is written as Decimal (DynamoDB rejects floats) and has to come
        # back as float, or the API's JSON encoder sees a Decimal on the way out.
        item = _job().to_item()
        item["costs"] = [
            {
                "key": "bedrock",
                "label": "Bedrock / LLM",
                "usd": Decimal("1.9763"),
                "detail": "1,717,584 in / 51,736 out tokens",
                "basis": "measured",
            }
        ]
        job = Job.from_item(item)

        assert isinstance(job.costs[0].usd, float)
        assert job.costs[0].usd == 1.9763

    def test_a_record_written_before_costs_existed_still_loads(self):
        # DynamoDB items don't migrate; older runs carry only the scalar fields.
        item = _job().to_item()
        item.pop("costs", None)

        assert Job.from_item(item).costs == []

    def test_legacy_plain_string_logs_still_load(self):
        # Job records written before `kind` existed store bare strings, not
        # {"message", "kind"} dicts — an old, still-live record must not 500.
        item = _job().to_item()
        item["logs"] = ["Starting your search...", "Advanced Search: 3 row(s)"]
        job = Job.from_item(item)
        assert job.logs == [
            LogEntry(message="Starting your search..."),
            LogEntry(message="Advanced Search: 3 row(s)"),
        ]
        assert all(entry.kind == "detail" for entry in job.logs)


class TestLogEntry:
    def test_round_trips_milestone_kind(self):
        item = _job(logs=[{"message": "Found the property.", "kind": "milestone"}]).to_item()
        restored = Job.from_item(item)
        assert restored.logs == [LogEntry(message="Found the property.", kind="milestone")]
