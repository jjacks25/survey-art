"""A worker task that stops from outside must fail its job — and only an unfinished one."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

HANDLER = Path(__file__).resolve().parents[2] / "apps" / "dispatcher" / "handler.py"


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("JOBS_TABLE", "jobs")
    with mock_aws():
        t = boto3.resource("dynamodb").create_table(
            TableName="jobs",
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        spec = importlib.util.spec_from_file_location("dispatcher_handler", HANDLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield t, module.handler


def _stopped(job_id: str) -> dict:
    return {
        "detail-type": "ECS Task State Change",
        "detail": {
            "taskArn": "arn:task/x",
            "lastStatus": "STOPPED",
            "stoppedReason": "Essential container in task exited",
            "containers": [{"reason": "OutOfMemoryError: Container killed due to memory usage"}],
            "overrides": {
                "containerOverrides": [{"environment": [{"name": "JOB_ID", "value": job_id}]}]
            },
        },
    }


def test_running_job_fails_with_the_container_reason(table):
    t, handler = table
    t.put_item(Item={"jobId": "a", "status": "RUNNING"})
    handler(_stopped("a"), None)
    item = t.get_item(Key={"jobId": "a"})["Item"]
    assert item["status"] == "FAILED"
    assert "OutOfMemoryError" in item["error"]


def test_finished_or_deleted_jobs_are_left_alone(table):
    t, handler = table
    t.put_item(Item={"jobId": "done", "status": "COMPLETED"})
    handler(_stopped("done"), None)
    handler(_stopped("gone"), None)
    assert t.get_item(Key={"jobId": "done"})["Item"]["status"] == "COMPLETED"
    assert "Item" not in t.get_item(Key={"jobId": "gone"})
