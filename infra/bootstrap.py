"""One-time bootstrap of the deploy/automation AWS infra via direct boto3 CloudFormation calls.

Manual operator tool — NOT run in CI. Run once per AWS account.
Safe to re-run: detects existing stack, falls back to update-or-skip.

Deploys infra/cloudformation/bootstrap.yaml via TemplateBody (no S3 bucket
exists yet to host it as TemplateURL). Future stacks should upload templates
to the bucket this stack creates and deploy via TemplateURL + change sets.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).parent / "cloudformation" / "bootstrap.yaml"
DEFAULT_STACK_NAME = "land-survey-scraper-bootstrap"
DEFAULT_SECRET_NAME = "land-survey-scraper/deploy-credentials"
DEFAULT_REGION = "us-west-2"

TERMINAL_FAILURE_STATUSES = {
    "ROLLBACK_COMPLETE",
    "CREATE_FAILED",
    "DELETE_FAILED",
    "UPDATE_FAILED",
    "ROLLBACK_FAILED",
    "UPDATE_ROLLBACK_FAILED",
}
IN_PROGRESS_STATUSES = {
    "CREATE_IN_PROGRESS",
    "UPDATE_IN_PROGRESS",
    "UPDATE_ROLLBACK_IN_PROGRESS",
    "ROLLBACK_IN_PROGRESS",
}
NO_UPDATES_MESSAGE = "No updates are to be performed."


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="AWS named profile to use.")
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
        help=f"AWS region to deploy into (default: {DEFAULT_REGION}).",
    )
    parser.add_argument(
        "--stack-name", default=DEFAULT_STACK_NAME, help="CloudFormation stack name."
    )
    parser.add_argument(
        "--secret-name",
        default=DEFAULT_SECRET_NAME,
        help="Secrets Manager secret name to hold the deploy user's access key.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the template only; do not create or update the stack.",
    )
    return parser.parse_args(argv)


def build_clients(profile: str | None, region: str):
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("cloudformation"), session.client("secretsmanager")


def describe_stack_status(cfn, stack_name: str) -> str | None:
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
    except ClientError as err:
        if "does not exist" in str(err):
            return None
        raise
    return resp["Stacks"][0]["StackStatus"]


def deploy_stack(cfn, stack_name: str, template_body: str, parameters: list[dict]) -> None:
    status = describe_stack_status(cfn, stack_name)

    if status is None:
        logger.info("Creating stack %s", stack_name)
        cfn.create_stack(
            StackName=stack_name,
            TemplateBody=template_body,
            Parameters=parameters,
            Capabilities=["CAPABILITY_NAMED_IAM"],
        )
        cfn.get_waiter("stack_create_complete").wait(StackName=stack_name)
        return

    if status in TERMINAL_FAILURE_STATUSES:
        raise RuntimeError(
            f"Stack {stack_name} is in terminal failure status {status}. "
            "Inspect and delete it manually in the AWS console before re-running this script."
        )

    if status in IN_PROGRESS_STATUSES:
        raise RuntimeError(
            f"Stack {stack_name} is currently {status}. Wait for it to settle and re-run."
        )

    logger.info("Stack %s exists (status %s); attempting update", stack_name, status)
    try:
        cfn.update_stack(
            StackName=stack_name,
            TemplateBody=template_body,
            Parameters=parameters,
            Capabilities=["CAPABILITY_NAMED_IAM"],
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ValidationError" and NO_UPDATES_MESSAGE in str(err):
            logger.info("No updates to the stack detected, skipping")
            return
        raise
    cfn.get_waiter("stack_update_complete").wait(StackName=stack_name)


def get_stack_outputs(cfn, stack_name: str) -> dict[str, str]:
    resp = cfn.describe_stacks(StackName=stack_name)
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}


def populate_secret(
    secretsmanager, secret_arn: str, access_key_id: str, secret_access_key: str
) -> None:
    secretsmanager.put_secret_value(
        SecretId=secret_arn,
        SecretString=json.dumps(
            {"AccessKeyId": access_key_id, "SecretAccessKey": secret_access_key}
        ),
    )
    logger.info("Secret populated: %s", secret_arn)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    template_body = TEMPLATE_PATH.read_text()
    cfn, secretsmanager = build_clients(args.profile, args.region)

    if args.dry_run:
        cfn.validate_template(TemplateBody=template_body)
        print("Template is valid.")
        return 0

    deploy_stack(
        cfn,
        args.stack_name,
        template_body,
        [{"ParameterKey": "SecretName", "ParameterValue": args.secret_name}],
    )

    outputs = get_stack_outputs(cfn, args.stack_name)
    populate_secret(
        secretsmanager,
        outputs["DeploySecretArn"],
        outputs["DeployAccessKeyId"],
        outputs["DeployAccessKeySecret"],
    )

    print(f"Bootstrap complete. Bucket: {outputs['DeployBucketName']}")
    print(f"Secret: {outputs['DeploySecretArn']}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
