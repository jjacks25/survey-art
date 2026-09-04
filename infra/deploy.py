"""All CloudFormation + SPA deploys for survey-art, in one operator tool.

Manual tool — run from your own machine using your own local AWS credentials
(e.g. `aws sso login`), never from CI. Three modes, chosen by the mutually exclusive
flags below:

  --bootstrap   Tier 1: create/update the CloudFormation template bucket via
                TemplateBody (infra/cloudformation/bootstrap.yaml). No template bucket
                exists yet at this point, so this is the one stack that can't use
                TemplateURL. Run once per account. Safe to re-run (identical template
                is a no-op).
  --all/--stack Tier 2: deploy the network / ecr / backend / frontend stacks (in that
                order for --all) via TemplateURL + change sets against the bootstrap bucket.
                Uploads the template, creates a change set (CREATE if new, else
                UPDATE), prints the summary, and executes it unless --diff. An empty
                change set is a clean no-op; --destroy deletes the app stacks instead
                (bootstrap is left intact).
  --web         Publish the already-built SPA: write config.json from the backend/
                frontend stack outputs, sync apps/web/dist to the site bucket, and
                invalidate CloudFront. Run `npm run build` in apps/web first (the
                Makefile's `make deploy web` target does this for you).

Usage:
    uv run python infra/deploy.py --bootstrap
    uv run python infra/deploy.py --all
    uv run python infra/deploy.py --stack backend --param ApiImageTag=abc123
    uv run python infra/deploy.py --all --diff        # preview only
    uv run python infra/deploy.py --web
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, WaiterError

logger = logging.getLogger(__name__)

CFN_DIR = Path(__file__).parent / "cloudformation"
PARAMS_DIR = Path(__file__).parent / "params"
BOOTSTRAP_TEMPLATE = CFN_DIR / "bootstrap.yaml"
DISPATCHER_SRC = Path(__file__).parent.parent / "apps" / "dispatcher" / "handler.py"
DISPATCHER_MARKER = "# {{ dispatcher_handler }}"
# CloudFormation's inline Lambda code (`Code.ZipFile`) caps at 4096 characters.
MAX_INLINE_LAMBDA = 4096
WEB_DIST = Path(__file__).parent.parent / "apps" / "web" / "dist"

DEFAULT_PROJECT = "survey-art"
DEFAULT_REGION = "us-west-2"
DEFAULT_ENVIRONMENT = "dev"
DEFAULT_BOOTSTRAP_STACK = "survey-art-bootstrap"

# Deploy order. Each entry: (logical name, template file).
STACKS: list[tuple[str, str]] = [
    ("network", "network.yaml"),
    ("ecr", "ecr.yaml"),
    ("backend", "backend.yaml"),
    ("frontend", "frontend.yaml"),
]

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
REVIEW_STATUS = "REVIEW_IN_PROGRESS"
NO_CHANGE_REASONS = ("didn't contain changes", "No updates are to be performed", "no updates")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--profile", default=None, help="AWS named profile to use.")
    p.add_argument(
        "--region",
        default=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
        help=f"AWS region to deploy into (default: {DEFAULT_REGION}).",
    )
    p.add_argument("--project-name", default=DEFAULT_PROJECT)
    p.add_argument(
        "--bootstrap-stack",
        default=DEFAULT_BOOTSTRAP_STACK,
        help=f"Bootstrap stack name — created by --bootstrap, looked up by --all/--stack "
        f"for the template bucket (default: {DEFAULT_BOOTSTRAP_STACK}).",
    )

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--bootstrap", action="store_true", help="Create/update the Tier-1 template-bucket stack."
    )
    mode.add_argument(
        "--web",
        action="store_true",
        help="Publish the built SPA (config.json, S3 sync, CF invalidation).",
    )
    mode.add_argument(
        "--all", action="store_true", help="Deploy network+ecr+backend+frontend, in order."
    )
    mode.add_argument("--stack", choices=[s[0] for s in STACKS], help="Deploy a single app stack.")

    g_bootstrap = p.add_argument_group("--bootstrap options")
    g_bootstrap.add_argument(
        "--environment",
        default=os.environ.get("ENVIRONMENT", DEFAULT_ENVIRONMENT),
        choices=("dev", "prod"),
        help=f"Environment to bootstrap (default: {DEFAULT_ENVIRONMENT}).",
    )
    g_bootstrap.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the bootstrap template only; create nothing.",
    )

    g_stacks = p.add_argument_group("--all/--stack options")
    g_stacks.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override a template parameter (repeatable).",
    )
    g_stacks.add_argument("--diff", action="store_true", help="Print change sets; do not execute.")
    g_stacks.add_argument(
        "--destroy",
        action="store_true",
        help="Delete the app stacks (reverse order) instead of deploying.",
    )

    return p.parse_args(argv)


# --------------------------------------------------------------------------
# Shared CloudFormation helpers
# --------------------------------------------------------------------------


def stack_status(cfn, stack_name: str) -> str | None:
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
    except ClientError as err:
        if "does not exist" in str(err):
            return None
        raise
    return resp["Stacks"][0]["StackStatus"]


def stack_outputs(cfn, stack_name: str) -> dict[str, str]:
    resp = cfn.describe_stacks(StackName=stack_name)
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}


def _failure_reasons(cfn, stack_name: str) -> list[str]:
    """Resource-level failure reasons from stack events (e.g. the actual "image does
    not exist" message), most recent first — this is the part the waiter itself
    throws away, and it's usually the only line worth reading."""
    events = cfn.describe_stack_events(StackName=stack_name)["StackEvents"]
    seen: set[str] = set()
    reasons = []
    for e in events:
        if not e["ResourceStatus"].endswith("FAILED"):
            continue
        reason = e.get("ResourceStatusReason", "")
        if not reason or reason in seen or "cancelled" in reason.lower():
            continue
        seen.add(reason)
        reasons.append(f"{e['LogicalResourceId']}: {reason}")
    return reasons


# --------------------------------------------------------------------------
# --bootstrap: Tier 1 — the template bucket, deployed via TemplateBody
# --------------------------------------------------------------------------


def deploy_stack(cfn, stack_name: str, template_body: str, parameters: list[dict]) -> None:
    """Create-or-update a stack by raw TemplateBody.

    Used only for bootstrap — every other stack goes through the TemplateURL +
    change-set path in deploy_one().
    """
    status = stack_status(cfn, stack_name)

    if status is None:
        logger.info("Creating stack %s", stack_name)
        cfn.create_stack(StackName=stack_name, TemplateBody=template_body, Parameters=parameters)
        cfn.get_waiter("stack_create_complete").wait(StackName=stack_name)
        return

    if status in TERMINAL_FAILURE_STATUSES:
        raise RuntimeError(
            f"Stack {stack_name} is in terminal failure status {status}. "
            "Inspect and delete it manually in the AWS console before re-running."
        )
    if status in IN_PROGRESS_STATUSES:
        raise RuntimeError(
            f"Stack {stack_name} is currently {status}. Wait for it to settle and re-run."
        )

    logger.info("Stack %s exists (status %s); attempting update", stack_name, status)
    try:
        cfn.update_stack(StackName=stack_name, TemplateBody=template_body, Parameters=parameters)
    except ClientError as err:
        if err.response["Error"]["Code"] == "ValidationError" and NO_UPDATES_MESSAGE in str(err):
            logger.info("No updates to the stack detected, skipping")
            return
        raise
    cfn.get_waiter("stack_update_complete").wait(StackName=stack_name)


def run_bootstrap(cfn, args: argparse.Namespace) -> int:
    template_body = BOOTSTRAP_TEMPLATE.read_text()

    if args.dry_run:
        cfn.validate_template(TemplateBody=template_body)
        print("Template is valid.")
        return 0

    parameters = [
        {"ParameterKey": "ProjectName", "ParameterValue": args.project_name},
        {"ParameterKey": "Environment", "ParameterValue": args.environment},
    ]
    deploy_stack(cfn, args.bootstrap_stack, template_body, parameters)

    outputs = stack_outputs(cfn, args.bootstrap_stack)
    print("Bootstrap complete.")
    print(f"  Template bucket : {outputs.get('TemplateBucketName')}")
    return 0


# --------------------------------------------------------------------------
# --all/--stack: Tier 2 — network/backend/frontend via TemplateURL + change sets
# --------------------------------------------------------------------------


def render_template(filename: str) -> str:
    """Read a template, substituting any inline-code markers with their real source.

    The dispatcher Lambda is deployed as inline `Code.ZipFile`, but its source lives in
    `apps/dispatcher/handler.py` so it stays lintable and testable. Rather than keep two
    copies in sync by hand, the template carries a marker line that gets replaced here.
    """
    text = (CFN_DIR / filename).read_text()
    if DISPATCHER_MARKER not in text:
        return text

    indent = text.split(DISPATCHER_MARKER)[0].rsplit("\n", 1)[-1]
    source = DISPATCHER_SRC.read_text()
    if len(source) > MAX_INLINE_LAMBDA:
        raise RuntimeError(
            f"{DISPATCHER_SRC.name} is {len(source)} chars; CloudFormation caps inline "
            f"Lambda code at {MAX_INLINE_LAMBDA}. Package it as a zip/container instead."
        )
    # The marker line already carries the indent, so only lines 2..n need it added.
    first, *rest = source.splitlines()
    block = "\n".join([first, *(indent + line if line else "" for line in rest)])
    return text.replace(DISPATCHER_MARKER, block, 1)


def upload_template(s3, bucket: str, project: str, filename: str) -> str:
    key = f"templates/{project}/{filename}"
    s3.put_object(Bucket=bucket, Key=key, Body=render_template(filename).encode())
    region = s3.meta.region_name
    return f"https://s3.{region}.amazonaws.com/{bucket}/{key}"


def load_params(name: str, overrides: dict[str, str], project: str) -> list[dict]:
    """Merge params/{name}.json (if present) with CLI overrides; always set ProjectName."""
    params: dict[str, str] = {"ProjectName": project}
    param_file = PARAMS_DIR / f"{name}.json"
    if param_file.is_file():
        params.update(json.loads(param_file.read_text()))
    params.update(overrides)
    return [{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()]


def deploy_one(
    cfn,
    s3,
    project: str,
    bucket: str,
    name: str,
    filename: str,
    overrides: dict[str, str],
    diff_only: bool,
) -> None:
    stack_name = f"{project}-{name}"
    status = stack_status(cfn, stack_name)

    if status in TERMINAL_FAILURE_STATUSES:
        reasons = "\n    ".join(_failure_reasons(cfn, stack_name)) or "(no resource-level reason found)"
        raise RuntimeError(
            f"[{stack_name}] is stuck in {status} from a previous failed deploy — "
            "CloudFormation can't update a stack in this state.\n"
            f"    {reasons}\n"
            f"Fix the underlying cause above, then delete the stack "
            f"(`aws cloudformation delete-stack --stack-name {stack_name}`) and re-run."
        )
    if status in IN_PROGRESS_STATUSES:
        raise RuntimeError(f"[{stack_name}] is currently {status}. Wait for it to settle and re-run.")

    change_set_type = "CREATE" if status in (None, REVIEW_STATUS) else "UPDATE"

    template_url = upload_template(s3, bucket, project, filename)
    params = load_params(name, overrides, project)
    cs_name = f"{name}-{int(time.time())}"

    logger.info("[%s] creating %s change set", stack_name, change_set_type)
    cfn.create_change_set(
        StackName=stack_name,
        ChangeSetName=cs_name,
        TemplateURL=template_url,
        Parameters=params,
        Capabilities=["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
        ChangeSetType=change_set_type,
    )

    try:
        cfn.get_waiter("change_set_create_complete").wait(
            StackName=stack_name,
            ChangeSetName=cs_name,
            WaiterConfig={"Delay": 5, "MaxAttempts": 60},
        )
    except WaiterError:
        desc = cfn.describe_change_set(StackName=stack_name, ChangeSetName=cs_name)
        reason = desc.get("StatusReason", "")
        if any(r.lower() in reason.lower() for r in NO_CHANGE_REASONS):
            logger.info("[%s] no changes — skipping", stack_name)
            cfn.delete_change_set(StackName=stack_name, ChangeSetName=cs_name)
            if change_set_type == "CREATE":
                # An empty CREATE leaves a REVIEW_IN_PROGRESS stack; clean it up.
                cfn.delete_stack(StackName=stack_name)
            return
        raise RuntimeError(f"[{stack_name}] change set failed: {reason}")

    _print_changes(cfn, stack_name, cs_name)

    if diff_only:
        logger.info("[%s] --diff: not executing", stack_name)
        return

    logger.info("[%s] executing change set", stack_name)
    cfn.execute_change_set(StackName=stack_name, ChangeSetName=cs_name)
    waiter = "stack_create_complete" if change_set_type == "CREATE" else "stack_update_complete"
    try:
        cfn.get_waiter(waiter).wait(
            StackName=stack_name, WaiterConfig={"Delay": 10, "MaxAttempts": 180}
        )
    except WaiterError as err:
        reasons = "\n    ".join(_failure_reasons(cfn, stack_name)) or str(err)
        raise RuntimeError(f"[{stack_name}] deploy failed:\n    {reasons}") from None
    logger.info("[%s] done", stack_name)


def _print_changes(cfn, stack_name: str, cs_name: str) -> None:
    desc = cfn.describe_change_set(StackName=stack_name, ChangeSetName=cs_name)
    changes = desc.get("Changes", [])
    print(f"\nChange set for {stack_name} ({len(changes)} change(s)):")
    for c in changes:
        rc = c.get("ResourceChange", {})
        action = rc.get("Action", "?")
        rtype = rc.get("ResourceType", "")
        rid = rc.get("LogicalResourceId", "")
        print(f"  {action:8} {rtype:40} {rid}")
    print()


def run_stacks(cfn, s3, args: argparse.Namespace) -> int:
    overrides = dict(kv.split("=", 1) for kv in args.param)
    targets = STACKS if args.all else [s for s in STACKS if s[0] == args.stack]

    if args.destroy:
        for name, _ in reversed(targets):
            stack_name = f"{args.project_name}-{name}"
            if stack_status(cfn, stack_name) is None:
                logger.info("[%s] does not exist — skipping", stack_name)
                continue
            logger.info("[%s] deleting", stack_name)
            cfn.delete_stack(StackName=stack_name)
            cfn.get_waiter("stack_delete_complete").wait(
                StackName=stack_name, WaiterConfig={"Delay": 10, "MaxAttempts": 180}
            )
            logger.info("[%s] deleted", stack_name)
        return 0

    bucket = stack_outputs(cfn, args.bootstrap_stack).get("TemplateBucketName")
    if not bucket:
        raise RuntimeError(
            f"Bootstrap stack {args.bootstrap_stack} has no TemplateBucketName output. "
            "Run `infra/deploy.py --bootstrap` first."
        )
    for name, filename in targets:
        deploy_one(cfn, s3, args.project_name, bucket, name, filename, overrides, args.diff)
    return 0


# --------------------------------------------------------------------------
# --web: publish the built SPA
# --------------------------------------------------------------------------


def run_web(cfn, s3, cf, args: argparse.Namespace) -> int:
    if not WEB_DIST.is_dir():
        raise SystemExit(f"{WEB_DIST} not found — run `npm run build` in apps/web first.")

    be = stack_outputs(cfn, f"{args.project_name}-backend")
    fe = stack_outputs(cfn, f"{args.project_name}-frontend")

    # Same-origin API (CloudFront proxies /api/* to API Gateway), Cognito enabled.
    config = {
        "apiBase": "",
        "authDisabled": False,
        "cognito": {
            "authority": f"https://cognito-idp.{args.region}.amazonaws.com/{be['UserPoolId']}",
            "clientId": be["UserPoolClientId"],
            "domain": be["CognitoDomain"],
            "scope": "openid email profile",
        },
    }
    (WEB_DIST / "config.json").write_text(json.dumps(config, indent=2))
    logger.info("Wrote config.json (userPool=%s)", be["UserPoolId"])

    bucket = fe["SiteBucketName"]
    for path in WEB_DIST.rglob("*"):
        if not path.is_file():
            continue
        key = str(path.relative_to(WEB_DIST))
        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        cache = "no-cache" if key in ("index.html", "config.json") else "public, max-age=31536000"
        s3.upload_file(
            str(path), bucket, key, ExtraArgs={"ContentType": ctype, "CacheControl": cache}
        )
    logger.info("Synced %s to s3://%s", WEB_DIST, bucket)

    cf.create_invalidation(
        DistributionId=fe["DistributionId"],
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": str(time.time_ns()),
        },
    )
    logger.info("Invalidated CloudFront %s", fe["DistributionId"])
    print(f"Deployed. URL: {fe['CloudFrontUrl']}")
    return 0


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cfn = session.client("cloudformation")

    if args.bootstrap:
        return run_bootstrap(cfn, args)
    if args.web:
        return run_web(cfn, session.client("s3"), session.client("cloudfront"), args)
    return run_stacks(cfn, session.client("s3"), args)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        sys.exit(main())
    except RuntimeError as err:
        print(f"\nerror: {err}", file=sys.stderr)
        sys.exit(1)
