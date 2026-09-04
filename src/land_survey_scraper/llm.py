"""LLM factory for browser-use agents.

Reads LLM_PROVIDER, MODEL, and provider credentials from settings (.env).

Supported providers (set LLM_PROVIDER in .env):
  nvidia      — NVIDIA NIM API (OpenAI-compatible). Free tier at build.nvidia.com.
                NVIDIA_API_KEY required. Good free models: meta/llama-3.3-70b-instruct
  openrouter  — OpenRouter (multi-provider proxy). Free models available.
                OPENROUTER_API_KEY required.
  anthropic   — Anthropic direct. ANTHROPIC_API_KEY required.
  openai      — OpenAI direct. OPENAI_API_KEY required.

If LLM_PROVIDER is not set, the first provider with a configured key is used
(nvidia → openrouter → anthropic → openai).
"""

from __future__ import annotations

from browser_use.agent.service import Agent
from browser_use.llm.base import BaseChatModel

from land_survey_scraper.settings import get_settings

_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"


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


def _make_nvidia(s) -> BaseChatModel:
    from browser_use.llm.openai.chat import ChatOpenAI

    return ChatOpenAI(model=s.model, api_key=s.nvidia_api_key, base_url=_NVIDIA_BASE_URL)


def _make_openrouter(s) -> BaseChatModel:
    from browser_use.llm.openrouter.chat import ChatOpenRouter

    return ChatOpenRouter(model=s.model, api_key=s.openrouter_api_key)


def _make_anthropic(s) -> BaseChatModel:
    from browser_use.llm.anthropic.chat import ChatAnthropic

    return ChatAnthropic(model=s.model, api_key=s.anthropic_api_key)


def _make_openai(s) -> BaseChatModel:
    from browser_use.llm.openai.chat import ChatOpenAI

    return ChatOpenAI(model=s.model)


_PROVIDER_FACTORIES = {
    "nvidia": (_make_nvidia, lambda s: s.nvidia_api_key),
    "openrouter": (_make_openrouter, lambda s: s.openrouter_api_key),
    "anthropic": (_make_anthropic, lambda s: s.anthropic_api_key),
    "openai": (_make_openai, lambda s: True),
}


def get_llm() -> BaseChatModel:
    """Return the configured LLM instance for browser-use agents."""
    s = get_settings()
    provider = s.llm_provider.lower() if s.llm_provider else ""

    if provider:
        if provider not in _PROVIDER_FACTORIES:
            raise RuntimeError(
                f"Unknown LLM_PROVIDER '{provider}'. "
                f"Choose one of: {', '.join(_PROVIDER_FACTORIES)}"
            )
        factory, has_key = _PROVIDER_FACTORIES[provider]
        if not has_key(s):
            raise RuntimeError(
                f"LLM_PROVIDER={provider} but no API key is configured. "
                f"Set the corresponding key in .env."
            )
        return factory(s)

    # Auto-detect: first provider with a configured key
    for name, (factory, has_key) in _PROVIDER_FACTORIES.items():
        if has_key(s):
            return factory(s)

    raise RuntimeError(
        "No LLM credentials configured. "
        "Set LLM_PROVIDER and the corresponding API key in .env."
    )
