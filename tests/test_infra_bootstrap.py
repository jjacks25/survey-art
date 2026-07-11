"""Tests for infra/bootstrap.py's stack lifecycle and secret-population logic.

infra/ is a standalone operator tool, not part of the installed package, so it
isn't on the configured pythonpath (tests/ only gets src/). Import it directly
from its file path instead of adding a new sys.path entry project-wide.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

INFRA_DIR = Path(__file__).resolve().parent.parent / "infra"


def _load_bootstrap_module():
    spec = importlib.util.spec_from_file_location("infra_bootstrap", INFRA_DIR / "bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["infra_bootstrap"] = module
    spec.loader.exec_module(module)
    return module


bootstrap = _load_bootstrap_module()

# Minimal template used to exercise stack lifecycle branching without depending
# on moto's coverage of every resource type in the real bootstrap.yaml.
MINIMAL_TEMPLATE = json.dumps(
    {
        "Resources": {
            "TestBucket": {"Type": "AWS::S3::Bucket", "Properties": {}},
        },
        "Outputs": {
            "BucketName": {"Value": {"Ref": "TestBucket"}},
        },
    }
)


@pytest.fixture
def cfn():
    with mock_aws():
        yield boto3.client("cloudformation", region_name="us-west-2")


@pytest.fixture
def secretsmanager():
    with mock_aws():
        yield boto3.client("secretsmanager", region_name="us-west-2")


class TestDescribeStackStatus:
    def test_returns_none_for_nonexistent_stack(self, cfn):
        assert bootstrap.describe_stack_status(cfn, "does-not-exist") is None

    def test_returns_status_for_existing_stack(self, cfn):
        cfn.create_stack(StackName="my-stack", TemplateBody=MINIMAL_TEMPLATE)
        cfn.get_waiter("stack_create_complete").wait(StackName="my-stack")
        assert bootstrap.describe_stack_status(cfn, "my-stack") == "CREATE_COMPLETE"


class TestDeployStack:
    def test_creates_stack_when_absent(self, cfn):
        bootstrap.deploy_stack(cfn, "new-stack", MINIMAL_TEMPLATE, [])
        assert bootstrap.describe_stack_status(cfn, "new-stack") == "CREATE_COMPLETE"

    def test_no_op_update_does_not_raise(self, cfn):
        bootstrap.deploy_stack(cfn, "idempotent-stack", MINIMAL_TEMPLATE, [])
        # Re-running with an identical template should hit either the real
        # "No updates are to be performed." branch (real AWS) or moto's
        # looser update_stack (which just re-applies and succeeds) — either
        # way this must not raise.
        bootstrap.deploy_stack(cfn, "idempotent-stack", MINIMAL_TEMPLATE, [])
        assert bootstrap.describe_stack_status(cfn, "idempotent-stack") in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        }

    def test_raises_on_terminal_failure_status(self, cfn, monkeypatch):
        monkeypatch.setattr(bootstrap, "describe_stack_status", lambda *_: "ROLLBACK_COMPLETE")
        with pytest.raises(RuntimeError, match="terminal failure"):
            bootstrap.deploy_stack(cfn, "broken-stack", MINIMAL_TEMPLATE, [])

    def test_raises_when_in_progress(self, cfn, monkeypatch):
        monkeypatch.setattr(bootstrap, "describe_stack_status", lambda *_: "UPDATE_IN_PROGRESS")
        with pytest.raises(RuntimeError, match="UPDATE_IN_PROGRESS"):
            bootstrap.deploy_stack(cfn, "busy-stack", MINIMAL_TEMPLATE, [])


class TestGetStackOutputs:
    def test_returns_output_key_value_pairs(self, cfn):
        bootstrap.deploy_stack(cfn, "outputs-stack", MINIMAL_TEMPLATE, [])
        outputs = bootstrap.get_stack_outputs(cfn, "outputs-stack")
        assert "BucketName" in outputs
        assert isinstance(outputs["BucketName"], str)


class TestPopulateSecret:
    def test_writes_expected_json_shape(self, secretsmanager):
        created = secretsmanager.create_secret(Name="test-secret", SecretString="{}")
        bootstrap.populate_secret(secretsmanager, created["ARN"], "AKIAEXAMPLE", "supersecretvalue")

        value = secretsmanager.get_secret_value(SecretId=created["ARN"])
        payload = json.loads(value["SecretString"])
        assert payload == {"AccessKeyId": "AKIAEXAMPLE", "SecretAccessKey": "supersecretvalue"}
