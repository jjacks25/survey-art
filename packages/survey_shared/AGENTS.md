# packages/survey_shared/ — shared job/AWS helpers

A small workspace package (`survey-shared`) shared by `apps/api` and the worker
(`apps/worker/survey_art`) — job state, S3 result storage, and AWS client/config helpers. Kept
dependency-light (no browser/scraper deps) so the API's Lambda image stays lean.

## Files

- `jobs.py` — the `Job` pydantic model + all DynamoDB/S3 operations (see below).
- `aws.py` — boto3 client/resource factories, pointed at `AWS_ENDPOINT_URL` locally
  (LocalStack) or real AWS by default.
- `config.py` — pydantic-settings config, including `AWS_PUBLIC_ENDPOINT_URL` (see the
  presigned-URL note below).

## `Job` model and status flow

Single-key DynamoDB table (`jobId`). Status: `PENDING` → `RUNNING` → `COMPLETED` |
`FAILED` | `CANCELLED`. Fields worth knowing about beyond the obvious:

- `logs: list[str]` — appended to incrementally by the worker via `append_log()` as it
  scrapes, so the frontend's Logs tab can show live progress. See
  [`apps/worker/survey_art/AGENTS.md`](../../apps/worker/survey_art/AGENTS.md) for how these lines get
  produced without blocking the scraper.
- `metadata: dict | None` — the scraper's `overview.json`, stored verbatim (see the same
  doc) once the job completes. Opaque to this package — just round-tripped.
- `location: dict | None` — `{"lat": Decimal, "lon": Decimal}` for the frontend's Map
  tab. DynamoDB rejects native Python `float`; always go through `Decimal(str(x))`,
  never `Decimal(x)` directly (binary float imprecision would corrupt the value).
- `task_arn: str | None` — set by the dispatcher after `ecs:RunTask`, so `cancel_job()`'s
  caller (the API) knows which Fargate task to `ecs:StopTask`.
- `doc_prefix: str | None` — the property-identifying prefix (under `documents/` in the
  storage bucket) this job's files were uploaded under (see `upload_documents()` below),
  set once at COMPLETED. `list_result_files()` needs this to know where to list from —
  a job with no `doc_prefix` (not yet complete, or failed before upload) has no listable
  files.

`list_jobs()` (a single `scan()`, sorted client-side by `created_at`) backs the frontend's
history drawer. Fine at this table's size/access pattern (small team, "show recent jobs");
switch to a GSI on a constant partition + `createdAt` sort key if scan cost ever matters.

`update_status()` uses a `ConditionExpression` so a job already `CANCELLED` by the user
can't be silently flipped back to `RUNNING`/`COMPLETED`/`FAILED` by a worker that hadn't
noticed yet — a cancel always wins.

`cancel_job()` retries its own conditional update a few times, since the worker may flip
`PENDING` → `RUNNING` in the same instant the user cancels; a bare single-shot compare-
and-swap would occasionally lose that race and leave the job stuck.

## Presigned URLs: LocalStack vs. real AWS

`list_result_files()` and `upload_map_image()` both call `_make_browser_reachable(url)`
before returning a presigned URL. This exists **only** for local dev: LocalStack signs
URLs using whatever endpoint the boto3 client was configured with
(`http://localstack:4566`, the docker-compose service hostname), which a host-machine
browser can't resolve. `_make_browser_reachable()` rewrites the scheme+host to
`get_shared_settings().public_endpoint_url` (from `AWS_PUBLIC_ENDPOINT_URL`) when that's set,
and is a no-op otherwise — in real AWS, S3 presigned URLs are already browser-reachable,
so the env var stays unset there.

**Both services that presign URLs need this env var set**, not just one. The API
presigns download URLs (`list_result_files`); the worker *also* presigns a URL when it
uploads the map screenshot (`upload_map_image`). It's an easy mistake to add
`AWS_PUBLIC_ENDPOINT_URL` to only the `api` block in `docker-compose.yml` and forget the
`worker` block — the symptom is a presigned URL that curls fine from inside a container
but 404s/DNS-fails from the host browser.

## One `STORAGE_BUCKET`, split by prefix

There's a single S3 bucket (`STORAGE_BUCKET`) for everything the app writes, divided into
two namespaces that get different retention — not two separate bucket resources, since
S3 lifecycle rules support a `Prefix` filter and one rule scoped to `scratch/` does the
job:

- `scratch/` — ephemeral per-job artifacts. The bucket's lifecycle rule (90-day
  expiry + intelligent tiering) is scoped to this prefix.
- `documents/` — the durable, per-property archive of every document actually
  downloaded from a county site. No expiration — outside the lifecycle rule's prefix
  filter entirely.

`DOCUMENTS_PREFIX = "documents"` and `SCRATCH_PREFIX = "scratch"` in `jobs.py` are the
only places that need to know this split.

## `upload_map_image()`

Uploads a scraper-captured map screenshot to `scratch/maps/{jobId}.png` — ephemeral,
outside any job's document prefix, so it never gets picked up as a spurious "document"
in the Results tab and ages out with the rest of `scratch/`.

## `upload_documents()` / `list_result_files()` — the documents archive

Downloaded county documents (deeds, plats, easements — the actual survey records) go
under `documents/`, then a prefix organized by geography and property, not by job:

```
documents/{state}/{county}/{identifier}/{filename}
```

The `{state}/{county}/{identifier}` part is built by `worker.py`'s `_doc_prefix()`
(state/county from where the scraper actually saved files locally; identifier prefers
the resolved property address, then the original input, then account number, then legal
description, then section/township/range — whichever is known). The computed prefix
(without the `documents/` prefix — that's added by `upload_documents`/`list_result_files`
themselves) is persisted as `Job.doc_prefix` so `list_result_files(prefix)` — called by
`GET /api/jobs/{id}/files` — knows where to list from later; it takes the prefix
directly rather than a `jobId`.

**This is deliberately not per-job.** Two searches for the same property land their
documents in the same prefix, so `documents/` accumulates a durable, browsable archive
by property rather than scattering copies across opaque job IDs.

When listing, `list_result_files()` returns just the file's basename as `name` (`key.rsplit("/", 1)[-1]`),
not the full key — a multi-segment prefix means naively splitting on the *first* `/` (the
old, job-id-shaped assumption) would leak `state/county/...` into what the UI shows as a
filename.

Each entry carries **two** presigned URLs for the same object. `url` is plain, so the
browser renders it inline — the Results tab previews PDFs in an `<iframe>`. `downloadUrl`
adds `ResponseContentDisposition: attachment; filename="…"`, so the download button saves
the file instead. They have to be separate URLs: putting the disposition on `url` would
turn every thumbnail into a download. Keep the `filename` in sync with `name` or the saved
file gets S3's key instead.

## Tests

`tests/test_jobs_model.py` covers the `Job` model's field defaults/serialization.
`infra/tests/test_deploy_harness.py` and friends use `moto` to mock DynamoDB/S3/SQS
rather than hitting real AWS — follow that pattern for new tests here.
