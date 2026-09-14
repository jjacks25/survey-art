"""FastAPI job-broker API.

Thin, stateless layer: it accepts an address, records a job in DynamoDB, and
enqueues it on SQS for the Fargate worker to process. It never runs the scraper
or a browser itself, so it stays small and fits scale-to-zero Lambda.

Auth: in AWS, API Gateway enforces a Cognito JWT authorizer in front of this app,
so requests reaching here are already authenticated. For local development
(LocalStack has no Cognito in the community edition) set ``AUTH_DISABLED=true``.
"""

from __future__ import annotations

import json
import os
import uuid

from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from survey_shared import aws, jobs
from survey_shared.config import get_shared_settings

from .schemas import (
    CreateJobRequest,
    CreateJobResponse,
    FilesResponse,
    JobListResponse,
    JobResponse,
    JobSummary,
)

app = FastAPI(title="Survey Art API", version="0.1.0")

# CORS is only needed for local dev where the Vite dev server is a separate origin.
# In AWS the SPA and API share the CloudFront origin, so this is a no-op there.
_cors_origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/jobs", response_model=CreateJobResponse, status_code=202)
def create_job(req: CreateJobRequest) -> CreateJobResponse:
    job_id = uuid.uuid4().hex
    jobs.create_job(job_id, address=req.address, county=req.county or "")

    aws.client("sqs").send_message(
        QueueUrl=get_shared_settings().require_job_queue_url(),
        MessageBody=json.dumps(
            {"jobId": job_id, "address": req.address, "county": req.county or ""}
        ),
    )
    return CreateJobResponse(jobId=job_id, status=jobs.PENDING)


@app.get("/api/jobs", response_model=JobListResponse)
def list_jobs() -> JobListResponse:
    return JobListResponse(
        jobs=[
            JobSummary(
                jobId=j.job_id,
                address=j.address,
                county=j.county,
                status=j.status,
                createdAt=j.created_at,
                fileCount=j.file_count,
                docPrefix=j.doc_prefix,
            )
            for j in jobs.list_jobs()
        ]
    )


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return JobResponse(**job.to_item())


@app.get("/api/jobs/{job_id}/files", response_model=FilesResponse)
def get_files(job_id: str) -> FilesResponse:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.doc_prefix:
        return FilesResponse(jobId=job_id, files=[])
    return FilesResponse(jobId=job_id, files=jobs.list_result_files(job.doc_prefix))


@app.delete("/api/jobs/{job_id}", status_code=204)
def cancel_job(job_id: str) -> None:
    job = jobs.cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=409, detail="job not found or already finished")
    if job.task_arn:
        # Local dev has no CLUSTER_ARN/ECS at all; the DB status change is what
        # matters there — stopping the Fargate task is best-effort on top of it.
        try:
            aws.client("ecs").stop_task(
                cluster=get_shared_settings().require_cluster_arn(),
                task=job.task_arn,
                reason="Cancelled by user",
            )
        except (RuntimeError, ClientError):
            pass
