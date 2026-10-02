"""`run_agent` hands back the agent's answer, not its whole action log."""

from __future__ import annotations

from survey_art import llm


async def test_run_agent_returns_the_final_answer(monkeypatch):
    class History:
        def final_result(self):
            return "  R1611986\n"

        def __str__(self):
            return "AgentHistoryList(all_results=[...], all_model_outputs=[...])"

    class Agent:
        history = None

        def __init__(self, **_):
            pass

        async def run(self):
            return History()

    monkeypatch.setattr(llm, "Agent", Agent)
    monkeypatch.setattr(llm, "ChatAWSBedrock", lambda **_: None)
    _, answer, _ = await llm.run_agent("find the parcel")
    assert answer == "R1611986"
