"""Test fixtures for the API: a moto-mocked DynamoDB/SQS/S3 environment."""

from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("JOBS_TABLE", "test-jobs")
os.environ.setdefault("STORAGE_BUCKET", "test-storage")
os.environ.setdefault("JOB_QUEUE_URL", "")  # filled in per-test after the queue is created


@pytest.fixture
def aws_env():
    with mock_aws():
        region = "us-west-2"
        dynamodb = boto3.resource("dynamodb", region_name=region)
        dynamodb.create_table(
            TableName="test-jobs",
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )

        s3 = boto3.client("s3", region_name=region)
        s3.create_bucket(
            Bucket="test-storage",
            CreateBucketConfiguration={"LocationConstraint": region},
        )

        sqs = boto3.client("sqs", region_name=region)
        queue_url = sqs.create_queue(QueueName="test-jobs")["QueueUrl"]
        os.environ["JOB_QUEUE_URL"] = queue_url

        yield {"queue_url": queue_url}


@pytest.fixture
def client(aws_env):
    # Import lazily so settings/clients pick up the env vars set above.
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
