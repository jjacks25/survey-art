# infra/ — Infrastructure & Deployment

CloudFormation IaC and the boto3 deploy harness for the AWS deployment. See
[`docs/architecture.md`](../docs/architecture.md) for the full picture.

## Two-tier deploy model

Deploys are **change-set driven** and run through a single Python + boto3 tool,
[`deploy.py`](deploy.py) (invoked via `make deploy <target>`, which runs it in a
container so the host needs no `uv`/`aws`; run `make deploy help` for the target list).
It also handles publishing the built SPA (`--web`) — see [`apps/web/AGENTS.md`](../apps/web/AGENTS.md).
`build_push.sh` (docker build/push to ECR) stays a separate bash script deliberately —
it only shells out to the `docker`/`aws` CLIs, nothing boto3/CloudFormation, so folding
it into `deploy.py` would just be `subprocess.run(["docker", ...])` calls in Python for
no benefit.

1. **Tier 1 — bootstrap** (`deploy.py --bootstrap` → `cloudformation/bootstrap.yaml`),
   deployed via **TemplateBody** because the template S3 bucket doesn't exist yet.
   Creates only the template bucket — nothing else belongs here. Run once:
   `make deploy bootstrap`. The bucket name is explicit, not CFN-generated: an
   `EnvironmentConfig` mapping (`dev`/`prod`, chosen by `--environment`, default `dev`)
   supplies the base name, plus account + region for global uniqueness. Templates
   expire after 7 days, so the bucket is disposable by design — renaming it just means
   re-uploading.
2. **Tier 2 — app stacks** (`deploy.py --all`/`--stack <name>`), deployed via
   **TemplateURL** against the bootstrap bucket. For each stack it uploads the
   template, creates a change set (CREATE if new, else UPDATE), prints the summary,
   and executes it. Empty change sets are a clean no-op.
   `make deploy network|ecr|backend|frontend` / `make deploy diff` / `make deploy destroy`.

## Stacks (deploy order, wired via exports)

`bootstrap → network → ecr → backend → frontend`

- **network.yaml** — single-AZ VPC, 1 public + 1 private subnet, IGW, worker SG
  (no inbound, all egress), NACLs. Purely networking — no unrelated resources. Private
  subnet is reserved for future use.
- **ecr.yaml** — the ECR repos (`<project>/api`, `<project>/worker`). Split into its own
  stack rather than living in network.yaml or bootstrap: it's an ordinary versioned
  resource with its own lifecycle (image scanning, tag mutability), unrelated to the
  VPC, but it still needs to exist before `make build-push` and before the backend
  stack (which references an image tag at create time) — hence its own stack between
  network and backend rather than folding into either.
- **backend.yaml** — one **S3 storage bucket** split by prefix (see below), DynamoDB
  jobs, SQS + DLQ, Secrets Manager, Cognito user pool/client/domain, IAM roles, ECS
  cluster + Fargate worker task def, FastAPI Lambda (container), dispatcher Lambda
  (SQS→ECS RunTask), API Gateway HTTP API with a Cognito JWT authorizer.
- **frontend.yaml** — private S3 SPA bucket (OAC), CloudFront (default → S3, `/api/*`
  → API Gateway origin), optional WAF WebACL (`WebAclArn`, must be us-east-1 scope).

## Key decisions & rationale

- **Deploys run from your local machine only.** No CI, no GitHub OIDC provider, no
  separate deploy role — `deploy.py` uses whatever AWS credentials are active in your
  shell (`aws sso login` or a profile via `--profile`). Compute (Lambda/Fargate) still
  uses its own task/execution roles, never your deploy creds.
- **Public-subnet Fargate, no NAT Gateway.** The scraper needs heavy egress to
  arbitrary county sites; a public IP with an egress-only SG avoids the ~$32/mo NAT
  Gateway. Trade-off: the task is not in a private subnet (a deliberate cost choice).
- **Cognito for auth**, enforced at the API Gateway JWT authorizer — the API compute is
  never publicly reachable without a valid token.
- **DynamoDB + S3 only** — no relational DB. Jobs table is single-key (`jobId`).
- **Job cancellation needs `ecs:StopTask` on the API Lambda's role.** `DELETE
  /api/jobs/{id}` marks the job CANCELLED in DynamoDB, then stops the job's Fargate task
  via its `taskArn` (captured by the dispatcher at `RunTask` time and written back to the
  job record) — without this permission the DynamoDB status flips but the scrape keeps
  running in the background.
- **One `StorageBucket`, split by prefix, not by resource.** `documents/{state}/{county}/
  {identifier}/` is the durable, per-property archive of every document actually
  downloaded from a county site (see
  [`packages/survey_shared/AGENTS.md`](../../packages/survey_shared/AGENTS.md));
  `scratch/maps/{jobId}.png` is ephemeral per-job output (map screenshots). The bucket's
  `LifecycleConfiguration` has a single rule **scoped with `Prefix: scratch/`** (90-day
  expiry + intelligent tiering) — S3 lifecycle rules support prefix filters, so one
  bucket can carry both a durable and an expiring namespace without a second bucket
  resource. `TaskRole` gets `s3:PutObject` on the whole bucket (worker writes to both
  prefixes); `ApiFunctionRole` gets `s3:GetObject`/`ListBucket` (API presigns downloads
  from both). If a future write needs different retention than these two prefixes, give
  it its own prefix and its own scoped lifecycle rule rather than reaching for a new
  bucket.

## Deploy flow

**First deploy in a fresh account** — the ECR repos (ecr.yaml) must exist before you can
push images, and the backend stack needs images to already be pushed before it can
reference them:

```bash
make deploy bootstrap                                         # once: template bucket
make deploy network                                           # creates the VPC
make deploy ecr                                                # creates the ECR repos
make build-push TAG=$(git rev-parse --short HEAD)              # push images to ECR
make deploy backend ARGS="--param ApiImageTag=<sha> --param WorkerImageTag=<sha>"
make deploy frontend
make deploy web                                                # build SPA + sync + invalidate
```

Or just `make deploy all`, which runs that whole sequence (TAG defaults to the current
git SHA).

**Every deploy after that** — `network`/`frontend` rarely change, so day-to-day is
usually just:

```bash
make build-push TAG=$(git rev-parse --short HEAD)
make deploy backend ARGS="--param ApiImageTag=<sha> --param WorkerImageTag=<sha>"
```

After the first frontend deploy, add the CloudFront URL to the backend stack's
`CallbackUrls`/`LogoutUrls` params (Cognito redirect allow-list) and redeploy backend.

## Gotchas

- WAF for CloudFront must be **us-east-1 / CLOUDFRONT scope**; deploy it separately and
  pass its ARN as `WebAclArn` (blank = no WAF).
- Secrets Manager values (`<project>/app-credentials`) are created empty — populate them
  out-of-band (never commit secret values).
- The dispatcher Lambda deploys as inline `Code.ZipFile`, but its only copy of the source
  is `apps/dispatcher/handler.py` — `deploy.py`'s `render_template()` splices it into the
  `# {{ dispatcher_handler }}` marker in `backend.yaml` at upload time. Edit the `.py`
  file, never the template. CloudFormation caps inline code at 4096 chars; `deploy.py`
  raises past that, at which point the handler needs real zip/container packaging.
- `deploy.py` is one file with three mutually exclusive modes (`--bootstrap`/`--web`/
  `--all`+`--stack`) rather than three scripts — they shared enough helpers
  (`stack_status`, `stack_outputs`, the change-set/param plumbing) that keeping them
  separate meant copy-pasted CloudFormation boilerplate. `make deploy <target>` is the
  thin dispatcher on top; `make deploy help` lists every target.
