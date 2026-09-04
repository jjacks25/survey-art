"""AWS Bedrock LLM for browser-use agents.

All LLM calls go through AWS Bedrock — Anthropic direct, OpenRouter, and NVIDIA
are no longer wired up. Auth is IAM only (the Fargate task role in prod, or
boto3's default credential chain locally, e.g. `aws sso login`) — there is no
API key. MODEL still comes from settings/.env, so switching models is just an
env var change; use a Bedrock model ID or cross-region inference profile ID
(e.g. "us.anthropic.claude-haiku-4-5-20251001-v1:0").
"""

from __future__ import annotations

from browser_use.agent.service import Agent
from browser_use.llm.aws.chat_bedrock import ChatAWSBedrock
from browser_use.llm.base import BaseChatModel

from survey_art.settings import get_settings


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


class BedrockLLM:
    """Configures and creates the Bedrock chat model used by browser-use agents.

    Model and region default from settings (.env) but can be overridden per
    instance, e.g. to run a one-off agent against a different model.
    """

    def __init__(self, model: str | None = None, region: str | None = None) -> None:
        s = get_settings()
        self.model = model or s.model
        self.region = region or s.aws_region

    def chat_model(self) -> BaseChatModel:
        """Build a fresh ChatAWSBedrock instance for this model/region."""
        return ChatAWSBedrock(model=self.model, aws_region=self.region)


def get_llm() -> BaseChatModel:
    """Return the configured Bedrock LLM instance for browser-use agents."""
    return BedrockLLM().chat_model()
