"""Repo-wide test setup: a moto-mocked AWS environment shared by every package.

`survey_shared.aws.client()`/`resource()` are `@cache`d, so the first boto3
client built in a test session is reused by every later test in the same
process. That is right in production (one long-lived client per service) and a
trap in tests: a test that touches AWS *outside* a `moto` mock caches a client
pointed at the real endpoint, and every subsequent moto-based test then silently
reuses it and fails to connect — far away from whatever actually caused it.
Clearing the caches around each test keeps that ordering dependency from
existing at all.
"""

from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

from survey_shared import aws

REGION = "us-west-2"
os.environ.setdefault("AWS_REGION", REGION)
os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
os.environ.setdefault("JOBS_TABLE", "test-jobs")
os.environ.setdefault("SAVED_PROPERTIES_TABLE", "test-saved-properties")
os.environ.setdefault("STORAGE_BUCKET", "test-storage")
# moto's queue URL is deterministic for a given name/region/account.
os.environ.setdefault("JOB_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123456789012/test-jobs")


@pytest.fixture(autouse=True)
def _isolate_boto3_clients():
    aws.client.cache_clear()
    aws.resource.cache_clear()
    yield
    aws.client.cache_clear()
    aws.resource.cache_clear()


@pytest.fixture
def aws_env():
    """The jobs/saved-properties tables, storage bucket and job queue, mocked."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        for table, key in (("test-jobs", "jobId"), ("test-saved-properties", "propertyKey")):
            dynamodb.create_table(
                TableName=table,
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                BillingMode="PAY_PER_REQUEST",
            )
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket="test-storage", CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        queue_url = boto3.client("sqs", region_name=REGION).create_queue(QueueName="test-jobs")[
            "QueueUrl"
        ]
        assert queue_url == os.environ["JOB_QUEUE_URL"]
        yield {"queue_url": queue_url}
