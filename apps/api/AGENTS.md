# apps/api/ — FastAPI job broker

A thin, stateless HTTP API (`survey-api` package). It accepts an address, records a
job in DynamoDB, and enqueues it on SQS for the Fargate worker. It **never** runs the
scraper or a browser, so it stays small and fits scale-to-zero Lambda.

## Runtime

- Packaged as a **Lambda container image** with the **AWS Lambda Web Adapter**, so the
  *same* image runs `uvicorn` locally (docker-compose) and as a Lambda in AWS with no
  code changes. See `Dockerfile`.
- Dependencies are its own (`fastapi`, `uvicorn`, `survey-shared`) — it deliberately
  does **not** install the scraper package, so `browser-use`/Playwright never bloat the
  image. Shared job/AWS logic comes from the `survey-shared` workspace package.

## Endpoints

- `POST /api/jobs {address, county?}` → create job (PENDING), enqueue, return `{jobId}`.
- `GET /api/jobs` → most-recent-first `JobSummary[]` (jobId/address/county/status/
  createdAt/fileCount, no `logs`/`metadata`) for the frontend's history drawer — see
  `jobs.list_jobs()`.
- `GET /api/jobs/{id}` → full job status from DynamoDB, including `logs` (streamed
  narration — see [`apps/worker/survey_art/AGENTS.md`](../worker/survey_art/AGENTS.md)),
  `metadata` (property details once complete — from the scraper's `overview.json`),
  `location` (`{lat, lon}` for the frontend's Map tab), and `docPrefix` (the documents
  bucket prefix, see below).
- `DELETE /api/jobs/{id}` → cancel a non-terminal job: `jobs.cancel_job()` flips status to
  `CANCELLED` (a no-op if the job already reached a terminal state), then, if the job has a
  `taskArn` (set by the dispatcher when it launched the Fargate task), calls
  `ecs:StopTask` to actually kill the in-flight scrape. Returns 204.
- `GET /api/jobs/{id}/files` → presigned S3 download URLs for the job's documents, listed
  from `job.doc_prefix` under the storage bucket's `documents/` namespace (see below) —
  returns an empty list if the job has no `doc_prefix` yet (not completed, or failed
  before any upload).
- `GET /api/health` → liveness.

## Auth

In AWS, **API Gateway enforces a Cognito JWT authorizer** in front of this app, so
requests arriving here are already authenticated. There is no auth code in the app.
Locally (LocalStack has no Cognito) there is no authorizer and the SPA runs with auth
disabled.

## Conventions

- **Use pydantic** for all request/response models (`app/schemas.py`) — data-model
  enforcement lives at the edge.
- Config (queue URL, table, bucket) comes from `survey_shared.config` (pydantic-settings),
  not raw `os.environ`, and fails fast if a required value is missing.
- IAM scope (backend stack): DynamoDB RW on the jobs table, `sqs:SendMessage`,
  `s3:GetObject`/list on the storage bucket (for presigning — see below), and
  `ecs:StopTask` (for `DELETE /api/jobs/{id}`) — nothing more.
- **One `STORAGE_BUCKET`, two prefixes, two different jobs.** `scratch/`
  (`jobs.upload_map_image()`) holds ephemeral per-job artifacts (map screenshots) and is
  the only prefix the bucket's 90-day expiry lifecycle rule applies to (S3 lifecycle
  rules support a `Prefix` filter, so one bucket carries both namespaces). `documents/`
  (`jobs.upload_documents()` / `list_result_files()`) is the durable archive of every
  document actually downloaded from a county site, keyed by
  `documents/{state}/{county}/{identifier}/{filename}` (see
  [`packages/survey_shared/AGENTS.md`](../../packages/survey_shared/AGENTS.md) and
  `worker.py`'s `_doc_prefix()`) — no expiry, since these are the actual survey records,
  not scratch output. Don't conflate the two prefixes when adding a new S3 write.
- **Presigned URLs, LocalStack vs. AWS.** `jobs.list_result_files()` rewrites each
  presigned S3 URL's host via `_make_browser_reachable()` before returning it, driven by
  `AWS_PUBLIC_ENDPOINT_URL`. This only matters locally: LocalStack signs URLs with the
  docker-compose service hostname (`localstack`), which a host-machine browser can't
  resolve, so `docker-compose.yml` sets `AWS_PUBLIC_ENDPOINT_URL=http://localhost:4566`
  for this service. In real AWS, S3 presigned URLs are already browser-reachable and the
  setting is left unset (a no-op). **The worker also generates a presigned URL** (for the
  map screenshot upload, see `jobs.upload_map_image()`) and needs the *same* env var set
  in its own service block — it's easy to fix this only on the api side and miss it there.

## Lint, format, tests

- Ruff (check + format) covers this directory as part of the root `uv` workspace —
  run via `make lint` / `make fmt` from the repo root.
- Tests live in `apps/api/tests/` and run as part of the root `make test` (pytest
  picks up both `tests/` and `apps/api/tests/` — see `[tool.pytest.ini_options]` in
  the root `pyproject.toml`). `apps/api/tests/conftest.py` spins up a `moto`-mocked
  DynamoDB table, S3 bucket, and SQS queue per test and exposes a `client` fixture
  (a `TestClient` wrapping the FastAPI app) — use it for endpoint tests instead of
  hitting real AWS.
