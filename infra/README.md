# infra/

One-time AWS bootstrap for survey-art. **Manual operator tool — not run in CI.**

## What it provisions

`cloudformation/bootstrap.yaml`, deployed via `deploy.py --bootstrap`:

- **S3 template bucket** — versioned, SSE-S3 encrypted, all public access blocked.
  Name is `{ProjectName}-{Environment}-cfn-templates-{account}-{region}` (see the
  `EnvironmentConfig` mapping in the template). Holds every CloudFormation template
  for later `TemplateURL`-based deploys + change sets. Disposable by design — templates
  expire after 7 days (`TemplateRetentionDays`), so recreating the bucket just means
  re-uploading.

This is the *only* resource bootstrap creates. There's no separate deploy IAM user or
Secrets Manager secret: every stack (including this one) deploys using your own local
AWS credentials (`aws sso login` or a profile), never a dedicated CI/deploy identity —
see [`AGENTS.md`](AGENTS.md) for the full deploy model.

This first stack is deployed via `TemplateBody` (template content sent directly in the
API call) because the S3 bucket it creates doesn't exist yet to host it as
`TemplateURL`. Every later stack (`network` → `ecr` → `backend` → `frontend`, via
`deploy.py --all`/`--stack`) deploys via `TemplateURL` against the bucket this stack
creates.

## Running it

```bash
# Validate the template only, no AWS resources created
uv run python infra/deploy.py --bootstrap --dry-run --region us-west-2

# Deploy (dev is the default environment)
uv run python infra/deploy.py --bootstrap --region us-west-2

# Optional: specific AWS profile, environment, or bootstrap stack name
uv run python infra/deploy.py --bootstrap --profile my-profile --environment prod --bootstrap-stack my-stack
```

Or via `make deploy bootstrap` (runs the same script in a container — see the root
[`Makefile`](../Makefile)):

```bash
make deploy bootstrap
make deploy bootstrap ARGS="--environment prod"
```

Safe to re-run — `deploy.py` detects an existing stack and updates it; an identical
template produces a no-op ("No updates are to be performed") rather than an error.

## Security notes

- No credentials are created or handled by this stack — it's a plain S3 bucket, so
  there's nothing for `deploy.py` to print, log, or rotate.
- The operator identity you run this with just needs S3 + CloudFormation permissions;
  it doesn't need IAM-user-creation privileges like an earlier version of this stack did.
