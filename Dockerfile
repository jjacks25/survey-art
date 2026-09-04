# syntax=docker/dockerfile:1
# Scraper / Fargate worker image. Installs the survey-art package (and its
# shared helper dependency) plus a headless Chromium for Playwright/browser-use.
# This is the heavy image; the API image (apps/api/Dockerfile) is kept separate and lean.
FROM python:3.13-slim

# Install curl and certs required by the uv installer, then install latest uv
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && apt-get purge -y --auto-remove curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

##############################
###  Install Dependencies   ###
##############################
# Workspace manifests first so the dependency layer caches independently of source.
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY apps/api/pyproject.toml ./apps/api/pyproject.toml
COPY apps/worker ./apps/worker
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package survey-art
ENV PATH="/app/.venv/bin:$PATH"

##############################
###  Playwright Chromium    ###
##############################
RUN apt-get update && playwright install --with-deps chromium \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy the rest of the source (scrapers, docs, etc.)
COPY . /app

##############################
###    Default Command      ###
##############################
# Default: run one scrape job from JOB_ID/ADDRESS/COUNTY env (Fargate worker).
# The CLI (`make process`) and local `worker` service override this command.
CMD ["python", "-m", "survey_art.worker"]
