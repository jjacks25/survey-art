"""Repo-wide test setup.

`survey_shared.aws.client()`/`resource()` are `@cache`d, so the first boto3
client built in a test session is reused by every later test in the same
process. That is right in production (one long-lived client per service) and a
trap in tests: a test that touches AWS *outside* a `moto` mock caches a client
pointed at the real endpoint, and every subsequent moto-based test in any
package then silently reuses it and fails to connect — far away from whatever
actually caused it.

Clearing the caches around each test keeps that ordering dependency from
existing at all, so tests can be run, reordered, or filtered in any combination.
"""

from __future__ import annotations

import pytest

from survey_shared import aws


@pytest.fixture(autouse=True)
def _isolate_boto3_clients():
    aws.client.cache_clear()
    aws.resource.cache_clear()
    yield
    aws.client.cache_clear()
    aws.resource.cache_clear()
