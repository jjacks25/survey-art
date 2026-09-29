"""A cancelled job must stop the local worker, and must never start."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from survey_art import worker
from survey_shared import jobs


def _fake_job_store(monkeypatch, status: str) -> dict:
    state = {"status": status, "updates": []}
    monkeypatch.setattr(jobs, "get_job", lambda _id: SimpleNamespace(status=state["status"]))
    monkeypatch.setattr(jobs, "update_status", lambda _id, s, **_: state["updates"].append(s))
    return state


async def test_running_scrape_stops_when_job_is_cancelled(monkeypatch):
    monkeypatch.setattr(worker, "_CANCEL_POLL_SECONDS", 0.01)
    state = _fake_job_store(monkeypatch, jobs.RUNNING)
    stopped = asyncio.Event()

    async def scrape():
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    async def cancel_soon():
        await asyncio.sleep(0.05)
        state["status"] = jobs.CANCELLED

    asyncio.ensure_future(cancel_soon())
    with pytest.raises(worker._JobCancelledError):
        await asyncio.wait_for(worker._run_unless_cancelled("j", scrape()), timeout=5)
    assert stopped.is_set()


async def test_result_passes_through_when_not_cancelled(monkeypatch):
    _fake_job_store(monkeypatch, jobs.RUNNING)

    async def scrape():
        return "done"

    assert await worker._run_unless_cancelled("j", scrape()) == "done"


@pytest.mark.parametrize("status", sorted(jobs.TERMINAL))
async def test_terminal_job_is_never_started(monkeypatch, status):
    state = _fake_job_store(monkeypatch, status)
    assert await worker.run_job("j", "R1611986", "CO_weld") == 0
    assert state["updates"] == []
