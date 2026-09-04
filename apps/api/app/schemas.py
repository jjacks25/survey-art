"""Request/response models for the job-broker API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateJobRequest(BaseModel):
    address: str = Field(..., min_length=3, max_length=500)
    county: str | None = Field(default=None, max_length=32)


class CreateJobResponse(BaseModel):
    jobId: str
    status: str


class JobResponse(BaseModel):
    jobId: str
    address: str
    county: str
    status: str
    createdAt: int
    updatedAt: int
    fileCount: int
    error: str | None = None
    logs: list[str] = []
    metadata: dict | None = None
    location: dict | None = None
    docPrefix: str | None = None


class JobSummary(BaseModel):
    """Lightweight per-job listing for the history sidebar — omits `logs`/
    `metadata`, which can be large and aren't needed until a job is opened."""

    jobId: str
    address: str
    county: str
    status: str
    createdAt: int
    fileCount: int
    docPrefix: str | None = None


class JobListResponse(BaseModel):
    jobs: list[JobSummary]


class FileEntry(BaseModel):
    name: str
    size: int
    url: str
    # Same object, signed with `Content-Disposition: attachment` so the browser
    # saves it instead of rendering it inline (`url` stays inline for previews).
    downloadUrl: str


class FilesResponse(BaseModel):
    jobId: str
    files: list[FileEntry]
