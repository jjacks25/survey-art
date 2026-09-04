# syntax=docker/dockerfile:1
# Single-stage: uv installs into the container's system Python (no virtual env).
# Use .dockerignore so .venv and other local artifacts are not in the build context.
# uv is installed via the official installer so each build gets the latest release.

##############################
###     Base & UV           ###
##############################
FROM python:3.13-slim

# Install curl and certs required by the uv installer, then install latest uv
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && apt-get purge -y --auto-remove curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Install into system Python (no .venv); copy mode for Docker; no dev deps
ENV UV_SYSTEM_PYTHON=1
ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_DEV=1

##############################
###  Install Dependencies   ###
##############################
# Reuse this layer when only source changes (deps from lockfile only)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-editable

##############################
###   Install Project       ###
##############################
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

##############################
###  Playwright Chromium    ###
##############################
RUN apt-get update && uv run playwright install --with-deps chromium \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

##############################
###    Default Command      ###
##############################
CMD ["tail", "-f", "/dev/null"]
