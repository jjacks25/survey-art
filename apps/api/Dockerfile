# syntax=docker/dockerfile:1
# FastAPI job-broker API packaged as a Lambda container image.
#
# The AWS Lambda Web Adapter lets the *same* image run a normal uvicorn server
# locally (docker-compose) and as a Lambda function in AWS with no code changes:
# on Lambda the adapter (an extension) starts uvicorn and proxies API Gateway
# events to it over HTTP on $PORT.
#
# This image installs ONLY the API + the shared boto3 helpers (survey-shared).
# It deliberately does NOT install the scraper/worker package, so browser-use and
# Playwright/Chromium never bloat the Lambda image.
#
# Build context is the REPO ROOT (uv workspace):
#   docker build -f apps/api/Dockerfile -t survey-art-api .
FROM python:3.13-slim

# Lambda Web Adapter extension (no-op when run outside Lambda, e.g. docker-compose).
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && apt-get purge -y --auto-remove curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

# Workspace metadata + the two packages this image needs (api + shared). The worker's
# manifest (no source) still has to be present — it's a workspace member, so uv needs
# to see it to resolve the lockfile, even though survey-api never installs it.
COPY pyproject.toml uv.lock ./
COPY packages/survey_shared ./packages/survey_shared
COPY apps/worker/pyproject.toml ./apps/worker/pyproject.toml
COPY apps/api ./apps/api

# Install just the survey-api package (pulls survey-shared via the workspace).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package survey-api

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/apps/api

# LWA / uvicorn config. PORT is where uvicorn listens and where the adapter proxies.
ENV PORT=8000 AWS_LWA_PORT=8000 AWS_LWA_INVOKE_MODE=response_stream
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
