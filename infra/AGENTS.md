# infra/ — Infrastructure & Deployment

CloudFormation IaC and the boto3 deploy harness for the AWS deployment. See
[`docs/architecture.md`](../docs/architecture.md) for the full picture.

## IAM check: do this before/with every AWS API call you add or change

Every `aws.client(...)`/boto3 call the API Lambda, dispatcher Lambda, or worker task
makes has to be matched by an explicit `Action` in its role's policy here — nothing is
implicitly allowed. This has already bitten the project twice (worker's `ecs:StopTask`
missing, then `ApiFunctionRole` missing `dynamodb:DeleteItem` for `DELETE
/api/jobs/{id}` — see the "Job cancellation" and `DeleteItem` bullets below), both times
shipping as a silent 500/AccessDeniedException in prod that local dev never caught
(LocalStack doesn't enforce IAM).

So: whenever you add or change a call to `aws.client("<service>").<operation>(...)`
anywhere in `apps/api`, `apps/dispatcher`, `apps/worker`, or `packages/survey_shared`,
before calling the change done —

1. Grep this file (`infra/cloudformation/backend.yaml`) for the relevant role
   (`ApiFunctionRole`, `DispatcherFunctionRole`, `TaskRole`) and confirm the exact
   action you're calling is already listed. `dynamodb:UpdateItem` does **not** cover
   `DeleteItem`/`Scan`/`Query` — list every action you actually call, not just "enough".
2. If it's missing, add it to that role's policy statement in the same change, with a
   one-line comment (see the existing `Scan`/`DeleteItem` comment) saying which
   endpoint/code path needs it.
3. Local dev (LocalStack) does **not** enforce IAM, so `make test`/`make up` passing is
   *no signal* that permissions are correct — this class of bug only surfaces against
   real AWS. If you can't verify directly, say so explicitly rather than reporting the
   change as done; don't rely on tests as proof.

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
- **github-oidc.yaml** — not in the exported-dependency deploy order above (nothing
  else depends on its outputs); a standalone stack (`survey-art-github-oidc`) holding
  only the GitHub Actions OIDC provider + CD deploy role, deployed like bootstrap via
  TemplateBody. `make deploy all` runs it right after `--bootstrap`. See "CD on merge
  to main" below.

## Key decisions & rationale

- **CD on merge to main, via GitHub OIDC — no stored AWS keys.**
  [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) runs on every push
  to `main`: `make deploy all` — the exact same command (and target order:
  bootstrap → github-oidc → network → ecr → build+push → backend → frontend → web) as
  a manual full deploy from a laptop, so CI doesn't carry a second, narrower deploy path
  to keep in sync with the Makefile. `bootstrap`/`github-oidc`/`network`/`ecr` are cheap
  no-ops via empty change sets on every run after the first — including `github-oidc`
  itself, so the deploy role's own permissions and trust policy stay in sync with the
  template even when CD is what's applying the update. It assumes
  `survey-art-github-deploy` (`cloudformation/github-oidc.yaml`, its own stack — the
  *only* CI-related AWS resources) via `aws-actions/configure-aws-credentials` +
  `sts:AssumeRoleWithWebIdentity`, scoped to `repo:jjacks25/survey-art:ref:refs/heads/main`
  — no long-lived key ever leaves AWS. Runs on GitHub's free hosted runner, so the CD
  pipeline itself costs nothing beyond Actions minutes; it triggers no extra AWS compute
  (no CodeBuild/Lambda-driven deploy). You can still deploy everything from your own
  machine too — `deploy.py` uses whatever AWS credentials are active in your shell
  (`aws sso login` or a profile via `--profile`); the OIDC role is CI's identity, not a
  replacement for local access. The **very first** `make deploy github-oidc` (or
  `make deploy all`) after a fresh account still has to run from an operator's own
  credentials — chicken-and-egg, nothing exists yet for CI to assume — but every run
  after that, CD keeps the stack itself up to date too, including its own IAM policy
  (the role's `IamForAppRoles`/`IamOidcProvider` statements cover updating itself).
  Compute (Lambda/Fargate) still uses its own task/execution roles, never the deploy
  role.
- **Public-subnet Fargate, no NAT Gateway.** The scraper needs heavy egress to
  arbitrary county sites; a public IP with an egress-only SG avoids the ~$32/mo NAT
  Gateway. Trade-off: the task is not in a private subnet (a deliberate cost choice).
- **Cognito for auth**, enforced at the API Gateway JWT authorizer — the API compute is
  never publicly reachable without a valid token.
- **DynamoDB + S3 only** — no relational DB. Jobs table is single-key (`jobId`).
- **7-day expiry, kept in sync between `JobsTable` and `documents/`.** `JobsTable` has
  TTL enabled on `expiresAt` (set by `create_job()` at `createdAt + 7 days` — see
  [`packages/survey_shared/AGENTS.md`](../../packages/survey_shared/AGENTS.md)); the
  `StorageBucket`'s `documents/` prefix has its own 7-day `ExpirationInDays` lifecycle
  rule. The two aren't transactionally linked — they're just set to the same duration —
  so a repeated search after a week re-downloads from the county site rather than
  serving (or referencing) an expired document. If you change one, change the other.
- **Every compute log group is declared explicitly with `RetentionInDays: 30`**
  (`WorkerLogGroup`, `ApiFunctionLogGroup`, `DispatcherFunctionLogGroup`). Lambda/ECS
  auto-create a log group on first write with *no* retention cap if one doesn't already
  exist under the expected name (`/aws/lambda/{FunctionName}` / `/ecs/{family}`), so
  logs otherwise accumulate forever. If you add a new Lambda or ECS task here, declare
  its log group up front — don't let it get auto-created first, since CloudFormation
  can't later "adopt" a log group that already exists without deleting it or a stack
  **import** (`aws cloudformation create-change-set --change-set-type IMPORT`).
- **Job cancellation needs `ecs:StopTask` on the API Lambda's role.** `DELETE
  /api/jobs/{id}` marks the job CANCELLED in DynamoDB, then stops the job's Fargate task
  via its `taskArn` (captured by the dispatcher at `RunTask` time and written back to the
  job record) — without this permission the DynamoDB status flips but the scrape keeps
  running in the background.
- **One `StorageBucket`, split by prefix, not by resource.** `documents/{state}/{county}/
  {identifier}/` is the per-property archive of documents downloaded from a county site
  (see [`packages/survey_shared/AGENTS.md`](../../packages/survey_shared/AGENTS.md));
  `scratch/maps/{jobId}.png` is ephemeral per-job output (map screenshots);
  `property-search-logs/{jobId}.log` is one complete text log per job
  (`jobs.upload_job_log()`, same doc). The bucket's `LifecycleConfiguration` has three
  `Prefix`-scoped rules — S3 lifecycle rules support prefix filters, so one bucket can
  carry multiple differently-aging namespaces without a second bucket resource:
  `scratch/` expires after 90 days (+ intelligent tiering), `documents/` after 7 days
  (kept in sync with `JobsTable`'s TTL, see above), `property-search-logs/` after 30
  days — deliberately longer than `JobsTable`'s 7-day TTL, so a run's exact log outlives
  the job record it came from. `TaskRole` gets `s3:PutObject` on the whole bucket
  (worker writes to all three prefixes); `ApiFunctionRole` gets `s3:GetObject`/
  `ListBucket` (API presigns downloads from `documents/`). If a future write needs
  different retention than these three prefixes, give it its own prefix and its own
  scoped lifecycle rule rather than reaching for a new bucket.

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

`CallbackUrls`/`LogoutUrls` (the Cognito redirect allow-list) need no manual param —
`deploy.py`'s `_auto_params()` looks up the frontend stack's `CloudFrontUrl` output
itself and passes it for the `backend` stack automatically. Before the frontend stack
exists (first-ever deploy, before `make deploy frontend`), there's nothing to look up,
so the template's `http://localhost:5173/` default applies until it does. An explicit
`--param CallbackUrls=...`/`LogoutUrls=...` still wins over the auto-detected value if
you ever need to override it.

## Gotchas

- WAF for CloudFront must be **us-east-1 / CLOUDFRONT scope**; deploy it separately and
  pass its ARN as `WebAclArn` (blank = no WAF).
- Secrets Manager values (`<project>/config`) are created empty — populate them
  out-of-band (never commit secret values). The worker fetches this secret itself at
  startup via pydantic-settings' `AWSSecretsManagerSettingsSource` (see
  `apps/worker/survey_art/settings.py`), not via an ECS `Secrets` env injection — only
  the secret's ARN (`APP_CONFIG_SECRET_ID`, not a secret value) is passed as a plain
  container env var, and `TaskRole` (not `TaskExecutionRole`) holds the
  `secretsmanager:GetSecretValue` grant.
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
