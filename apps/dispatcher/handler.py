"""Dispatcher Lambda, two triggers:

- SQS job message -> ECS RunTask: one Fargate worker in the public subnet
  (public IP, so egress needs no NAT), job params as env overrides. The task ARN
  goes on the job so the API can ecs:StopTask it on cancel.
- EventBridge "ECS Task State Change" (STOPPED): a task killed from outside (OOM,
  Fargate failure) never records its own failure, so its job would sit RUNNING
  forever. Fail it here unless the worker already finished it.

Env: CLUSTER_ARN, TASK_DEFINITION, CONTAINER_NAME, SUBNET_ID, SECURITY_GROUP_ID,
JOBS_TABLE.

The only copy of the handler: `infra/deploy.py` splices it into backend.yaml as
inline `Code.ZipFile`. Keep it under 4096 chars and boto3-only.
"""

from __future__ import annotations

import json
import os
import time

import boto3

ecs = boto3.client("ecs")
dynamodb = boto3.resource("dynamodb")


def _task_stopped(detail, table):
    env = {
        e["name"]: e["value"]
        for o in detail.get("overrides", {}).get("containerOverrides", [])
        for e in o.get("environment", [])
    }
    reasons = [c["reason"] for c in detail.get("containers", []) if c.get("reason")]
    reason = (reasons or [detail.get("stoppedReason") or "unknown reason"])[0]
    try:
        table.update_item(
            Key={"jobId": env["JOB_ID"]},
            UpdateExpression="SET #s = :f, #e = :e, updatedAt = :u",
            ConditionExpression="#s IN (:p, :r)",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={
                ":f": "FAILED",
                ":e": f"The search's server stopped unexpectedly: {reason}",
                ":u": int(time.time()),
                ":p": "PENDING",
                ":r": "RUNNING",
            },
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        pass  # already finished, cancelled, or deleted
    return {"stopped": detail["taskArn"]}


def handler(event, _context):
    table = dynamodb.Table(os.environ["JOBS_TABLE"])
    if event.get("detail-type") == "ECS Task State Change":
        return _task_stopped(event["detail"], table)

    launched = []
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        job_id = body["jobId"]
        env = {"JOB_ID": job_id, "ADDRESS": body["address"], "COUNTY": body.get("county") or ""}
        resp = ecs.run_task(
            cluster=os.environ["CLUSTER_ARN"],
            taskDefinition=os.environ["TASK_DEFINITION"],
            launchType="FARGATE",
            count=1,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": [os.environ["SUBNET_ID"]],
                    "securityGroups": [os.environ["SECURITY_GROUP_ID"]],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": os.environ["CONTAINER_NAME"],
                        "environment": [{"name": k, "value": v} for k, v in env.items()],
                    }
                ]
            },
        )
        task_arns = [t["taskArn"] for t in resp.get("tasks", [])]
        launched.extend(task_arns)
        # If ECS rejected the run (e.g. capacity), raise so SQS retries / DLQs.
        if not task_arns:
            raise RuntimeError(f"RunTask launched no task for job {job_id}: {resp.get('failures')}")

        table.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET taskArn = :t, updatedAt = :u",
            ExpressionAttributeValues={":t": task_arns[0], ":u": int(time.time())},
        )

    return {"launched": launched}
