"""LLM factory for browser-use agents.

Reads MODEL and provider credentials from settings (.env).

Priority:
  1. OpenRouter  — if OPENROUTER_API_KEY is set (supports free models)
  2. Anthropic   — if ANTHROPIC_API_KEY is set

Recommended free model for POC: google/gemini-2.0-flash-exp:free
  - Strong tool use / function calling (required by browser-use)
  - 1500 requests/day free on OpenRouter
  - Fast enough for interactive navigation tasks

To switch models, change MODEL in .env — no code changes needed.
"""

from __future__ import annotations

from browser_use.agent.service import Agent
from browser_use.llm.base import BaseChatModel

from land_survey_scraper.settings import get_settings


def agent_cost(agent: Agent) -> tuple[float, int, int]:
    """Extract (total_cost_usd, input_tokens, output_tokens) from a completed agent run."""
    usage = getattr(agent.history, "usage", None)
    if usage is None:
        return 0.0, 0, 0
    return (
        usage.total_cost or 0.0,
        usage.total_prompt_tokens or 0,
        usage.total_completion_tokens or 0,
    )


def get_llm() -> BaseChatModel:
    """Return the configured LLM instance for browser-use agents."""
    s = get_settings()

    if s.openrouter_api_key:
        from browser_use.llm.openrouter.chat import ChatOpenRouter

        return ChatOpenRouter(
            model=s.model,
            api_key=s.openrouter_api_key,
        )

    if s.anthropic_api_key:
        from browser_use.llm.anthropic.chat import ChatAnthropic

        return ChatAnthropic(
            model=s.model,
            api_key=s.anthropic_api_key,
        )

    raise RuntimeError(
        "No LLM credentials configured. "
        "Set OPENROUTER_API_KEY (free models) or ANTHROPIC_API_KEY in .env."
    )
