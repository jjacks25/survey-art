"""Tests for infra/deploy.py: bootstrap stack lifecycle, param merging, stack-status probing.

infra/ is an operator tool, not on the package pythonpath — import by file path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

INFRA_DIR = Path(__file__).resolve().parent.parent


def _load_deploy_module():
    spec = importlib.util.spec_from_file_location("infra_deploy", INFRA_DIR / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["infra_deploy"] = module
    spec.loader.exec_module(module)
    return module


deploy = _load_deploy_module()

# Minimal template used to exercise bootstrap's stack lifecycle branching without
# depending on moto's coverage of every resource type in the real bootstrap.yaml.
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


class TestStackStatus:
    def test_returns_none_for_missing_stack(self, cfn):
        assert deploy.stack_status(cfn, "no-such-stack") is None

    def test_returns_status_for_existing_stack(self, cfn):
        cfn.create_stack(StackName="my-stack", TemplateBody=MINIMAL_TEMPLATE)
        cfn.get_waiter("stack_create_complete").wait(StackName="my-stack")
        assert deploy.stack_status(cfn, "my-stack") == "CREATE_COMPLETE"


class TestDeployStack:
    """deploy_stack() — the raw-TemplateBody create/update path used by --bootstrap."""

    def test_creates_stack_when_absent(self, cfn):
        deploy.deploy_stack(cfn, "new-stack", MINIMAL_TEMPLATE, [])
        assert deploy.stack_status(cfn, "new-stack") == "CREATE_COMPLETE"

    def test_no_op_update_does_not_raise(self, cfn):
        deploy.deploy_stack(cfn, "idempotent-stack", MINIMAL_TEMPLATE, [])
        # Re-running with an identical template should hit either the real
        # "No updates are to be performed." branch (real AWS) or moto's
        # looser update_stack (which just re-applies and succeeds) — either
        # way this must not raise.
        deploy.deploy_stack(cfn, "idempotent-stack", MINIMAL_TEMPLATE, [])
        assert deploy.stack_status(cfn, "idempotent-stack") in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        }

    def test_raises_on_terminal_failure_status(self, cfn, monkeypatch):
        monkeypatch.setattr(deploy, "stack_status", lambda *_: "ROLLBACK_COMPLETE")
        with pytest.raises(RuntimeError, match="terminal failure"):
            deploy.deploy_stack(cfn, "broken-stack", MINIMAL_TEMPLATE, [])

    def test_raises_when_in_progress(self, cfn, monkeypatch):
        monkeypatch.setattr(deploy, "stack_status", lambda *_: "UPDATE_IN_PROGRESS")
        with pytest.raises(RuntimeError, match="UPDATE_IN_PROGRESS"):
            deploy.deploy_stack(cfn, "busy-stack", MINIMAL_TEMPLATE, [])


class TestStackOutputs:
    def test_returns_output_key_value_pairs(self, cfn):
        deploy.deploy_stack(cfn, "outputs-stack", MINIMAL_TEMPLATE, [])
        outputs = deploy.stack_outputs(cfn, "outputs-stack")
        assert "BucketName" in outputs
        assert isinstance(outputs["BucketName"], str)


class TestLoadParams:
    def test_always_sets_project_name(self):
        params = deploy.load_params("nonexistent-stack", {}, "my-project")
        assert {"ParameterKey": "ProjectName", "ParameterValue": "my-project"} in params

    def test_overrides_win(self):
        params = deploy.load_params("nonexistent-stack", {"ApiImageTag": "sha123"}, "p")
        as_dict = {p["ParameterKey"]: p["ParameterValue"] for p in params}
        assert as_dict["ApiImageTag"] == "sha123"


class TestRenderTemplate:
    def test_dispatcher_source_lands_in_zipfile_verbatim(self):
        """The spliced block must parse back out of the YAML identical to handler.py."""
        # CloudFormation's !Ref/!GetAtt tags aren't plain YAML — ignore them; all we
        # care about is that the spliced block kept the document's indentation valid.
        loader = type("CfnLoader", (yaml.SafeLoader,), {})
        loader.add_multi_constructor("!", lambda *_: None)

        rendered = deploy.render_template("backend.yaml")
        assert deploy.DISPATCHER_MARKER not in rendered

        doc = yaml.load(rendered, Loader=loader)  # noqa: S506 — SafeLoader subclass
        code = doc["Resources"]["DispatcherFunction"]["Properties"]["Code"]["ZipFile"]
        assert code == deploy.DISPATCHER_SRC.read_text()
        compile(code, "dispatcher", "exec")

    def test_untouched_template_passes_through(self):
        assert (
            deploy.render_template("network.yaml") == (deploy.CFN_DIR / "network.yaml").read_text()
        )


class TestStacksOrder:
    def test_network_before_ecr_before_backend_before_frontend(self):
        names = [s[0] for s in deploy.STACKS]
        assert names == ["network", "ecr", "backend", "frontend"]
