"""API test client, against the repo-wide moto `aws_env` (see /conftest.py)."""

from __future__ import annotations

import pytest


@pytest.fixture
def client(aws_env):
    # Import lazily so settings/clients pick up the env vars the root conftest sets.
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
