"""Dispatcher Lambda: SQS message -> ECS RunTask (Fargate scraper worker).

Triggered by the job queue. For each message it launches one Fargate task in the
public subnet (assignPublicIp=ENABLED so it has egress with no NAT Gateway),
passing the job parameters as container environment overrides.

Environment:
    CLUSTER_ARN        — ECS cluster
    TASK_DEFINITION    — worker task definition (family or ARN)
    CONTAINER_NAME     — container name inside the task definition
    SUBNET_ID          — public subnet id
    SECURITY_GROUP_ID  — worker security group (no inbound, egress only)
    JOBS_TABLE         — DynamoDB jobs table (to record the launched task's ARN,
                         so the API can later ecs:StopTask it on cancel)

This file is the only copy of the handler: `infra/deploy.py` splices it into the
dispatcher-handler marker line in `cloudformation/backend.yaml` at deploy time, where it
becomes the function's inline `Code.ZipFile`. Keep it under 4096 characters
(CloudFormation's inline-code cap) and dependency-free beyond the Lambda runtime's boto3.
"""

from __future__ import annotations

import json
import os
import time

import boto3

ecs = boto3.client("ecs")
dynamodb = boto3.resource("dynamodb")


def handler(event, _context):
    cluster = os.environ["CLUSTER_ARN"]
    task_def = os.environ["TASK_DEFINITION"]
    container = os.environ["CONTAINER_NAME"]
    subnet = os.environ["SUBNET_ID"]
    sg = os.environ["SECURITY_GROUP_ID"]
    jobs_table = dynamodb.Table(os.environ["JOBS_TABLE"])

    launched = []
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        job_id = body["jobId"]
        address = body["address"]
        county = body.get("county") or ""

        resp = ecs.run_task(
            cluster=cluster,
            taskDefinition=task_def,
            launchType="FARGATE",
            count=1,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": [subnet],
                    "securityGroups": [sg],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": container,
                        "environment": [
                            {"name": "JOB_ID", "value": job_id},
                            {"name": "ADDRESS", "value": address},
                            {"name": "COUNTY", "value": county},
                        ],
                    }
                ]
            },
        )
        task_arns = [t["taskArn"] for t in resp.get("tasks", [])]
        launched.extend(task_arns)
        # If ECS rejected the run (e.g. capacity), raise so SQS retries / DLQs.
        if not task_arns:
            raise RuntimeError(f"RunTask launched no task for job {job_id}: {resp.get('failures')}")

        jobs_table.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET taskArn = :t, updatedAt = :u",
            ExpressionAttributeValues={":t": task_arns[0], ":u": int(time.time())},
        )

    return {"launched": launched}
