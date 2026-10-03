"""FastAPI job-broker API.

Thin, stateless layer: it accepts an address, records a job in DynamoDB, and
enqueues it on SQS for the Fargate worker to process. It never runs the scraper
or a browser itself, so it stays small and fits scale-to-zero Lambda.

Auth: in AWS, API Gateway enforces a Cognito JWT authorizer in front of this app,
so requests reaching here are already authenticated. For local development
(LocalStack has no Cognito in the community edition) set ``AUTH_DISABLED=true``.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from survey_shared import aws, jobs
from survey_shared.config import get_shared_settings

from . import kmz
from .schemas import (
    CreateJobRequest,
    CreateJobResponse,
    FilesResponse,
    FlagsRequest,
    JobListResponse,
    JobResponse,
    JobSummary,
    KmzIdentifyResponse,
    SavedPropertiesResponse,
    SavedPropertySummary,
)

_MAX_KMZ_BYTES = 10 * 1024 * 1024

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
    jobs.save_property(address=req.address, county=req.county or "")

    aws.client("sqs").send_message(
        QueueUrl=get_shared_settings().require("job_queue_url"),
        MessageBody=json.dumps(
            {"jobId": job_id, "address": req.address, "county": req.county or ""}
        ),
    )
    return CreateJobResponse(jobId=job_id, status=jobs.PENDING)


@app.post("/api/jobs/{job_id}/drawing", response_model=CreateJobResponse, status_code=202)
def create_drawing(job_id: str) -> CreateJobResponse:
    """Start a CAD drawing job for a finished search: read its vesting deed,
    exceptions and easements and plot them (survey_art/deed_plot.py).

    Rides the same queue, dispatcher and Fargate task as a search; the worker
    tells the two apart by the job record's `kind`, so the message body and the
    dispatcher's container overrides are unchanged.
    """
    source = jobs.get_job(job_id)
    if not source:
        raise HTTPException(status_code=404, detail="job not found")
    if source.kind != "search" or source.status != jobs.COMPLETED or not source.doc_prefix:
        raise HTTPException(
            status_code=409, detail="a drawing needs a completed search with documents"
        )
    drawing_id = uuid.uuid4().hex
    jobs.create_job(
        drawing_id, address=source.address, county=source.county, kind="drawing",
        source_job_id=job_id,
    )  # fmt: skip
    jobs.set_drawing_job(job_id, drawing_id)
    aws.client("sqs").send_message(
        QueueUrl=get_shared_settings().require("job_queue_url"),
        MessageBody=json.dumps(
            {"jobId": drawing_id, "address": source.address, "county": source.county}
        ),
    )
    return CreateJobResponse(jobId=drawing_id, status=jobs.PENDING)


@app.post("/api/kmz/identify", response_model=KmzIdentifyResponse)
async def identify_kmz(file: UploadFile) -> KmzIdentifyResponse:
    data = await file.read(_MAX_KMZ_BYTES + 1)
    if len(data) > _MAX_KMZ_BYTES:
        raise HTTPException(status_code=413, detail="KMZ file too large (10MB max)")
    identifier = kmz.extract_identifier(data)
    if identifier:
        return KmzIdentifyResponse(identifier=identifier)
    return KmzIdentifyResponse(
        identifier=None, parcels=await asyncio.to_thread(kmz.parcels_for_geometry, data)
    )


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


@app.get("/api/saved-properties", response_model=SavedPropertiesResponse)
def list_saved_properties() -> SavedPropertiesResponse:
    """Every property ever searched, most-recent-first — unlike `GET /api/jobs`,
    unaffected by deleting run history (`DELETE /api/jobs/{id}` /
    `DELETE /api/properties`), so it's always available to re-run."""
    return SavedPropertiesResponse(
        properties=[
            SavedPropertySummary(key=p.key, address=p.address, county=p.county, savedAt=p.saved_at)
            for p in jobs.list_saved_properties()
        ]
    )


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    # get_job() already resolved `metadata` from S3 (see jobs.upload_metadata()).
    return JobResponse.model_validate(job.model_dump())


@app.get("/api/jobs/{job_id}/files", response_model=FilesResponse)
def get_files(job_id: str) -> FilesResponse:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.doc_prefix:
        return FilesResponse(jobId=job_id, files=[])
    return FilesResponse(jobId=job_id, files=jobs.list_result_files(job.doc_prefix))


@app.get("/api/jobs/{job_id}/flags", response_model=FlagsRequest)
def get_flags(job_id: str) -> FlagsRequest:
    """Filenames flagged on this job's property — stored on the saved property
    (keyed by the job's `address`), so they survive re-runs and job expiry."""
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return FlagsRequest(files=jobs.get_flagged(job.address))


@app.put("/api/jobs/{job_id}/flags", response_model=FlagsRequest)
def set_flags(job_id: str, req: FlagsRequest) -> FlagsRequest:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    jobs.set_flagged(job.address, req.files)
    return FlagsRequest(files=jobs.get_flagged(job.address))


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str) -> None:
    """Cancel a running job, or — once it's already terminal — delete its
    record outright. One verb from the frontend's point of view (its button
    reads "Cancel" while a job is in flight, "Delete" once it's finished); which
    of the two happens depends on the job's own status, not the caller."""
    job = jobs.cancel_job(job_id)
    if job is not None:
        if job.task_arn:
            # Local dev has no CLUSTER_ARN/ECS at all; the DB status change is
            # what matters there — stopping the Fargate task is best-effort on
            # top of it.
            try:
                aws.client("ecs").stop_task(
                    cluster=get_shared_settings().require("cluster_arn"),
                    task=job.task_arn,
                    reason="Cancelled by user",
                )
            except (RuntimeError, ClientError):
                pass
        return
    if not jobs.delete_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")


@app.delete("/api/properties", status_code=204)
def delete_property(key: str) -> None:
    """Delete every job record for the property identified by `key` — a job's
    docPrefix, or its address when it has none yet (see `jobs.property_key()`,
    which mirrors the frontend's propertyHistory grouping in Layout.tsx). Used
    by the Results page's Delete button so removing a property clears every
    duplicate/retry run for it, not just the one currently open."""
    if jobs.delete_jobs_for_property(key) == 0:
        raise HTTPException(status_code=404, detail="no jobs found for property")
