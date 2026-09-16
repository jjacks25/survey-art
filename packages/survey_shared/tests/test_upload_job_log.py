"""Tests for jobs.upload_job_log() — archiving a job's full log to S3."""

from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("STORAGE_BUCKET", "test-storage")


@pytest.fixture
def s3_bucket():
    with mock_aws():
        region = "us-west-2"
        s3 = boto3.client("s3", region_name=region)
        s3.create_bucket(
            Bucket="test-storage", CreateBucketConfiguration={"LocationConstraint": region}
        )
        yield s3


def test_writes_text_file_under_logs_prefix(s3_bucket):
    from survey_shared.jobs import upload_job_log

    key = upload_job_log("job-123", "line one\nline two")

    assert key == "property-search-logs/job-123.log"
    body = s3_bucket.get_object(Bucket="test-storage", Key=key)["Body"].read()
    assert body == b"line one\nline two"


def test_content_type_is_plain_text(s3_bucket):
    from survey_shared.jobs import upload_job_log

    key = upload_job_log("job-456", "hello")

    head = s3_bucket.head_object(Bucket="test-storage", Key=key)
    assert head["ContentType"] == "text/plain"
