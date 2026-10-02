"""AWS Bedrock LLM for browser-use agents.

All LLM calls go through AWS Bedrock. Auth is IAM only (the Fargate task role in
prod, or boto3's default credential chain locally, e.g. `aws sso login`) — there
is no API key. MODEL comes from settings/.env, so switching models is just an env
var change; use a Bedrock model ID or cross-region inference profile ID (e.g.
"us.anthropic.claude-haiku-4-5-20251001-v1:0").
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from browser_use import Agent
from browser_use.llm.aws.chat_bedrock import ChatAWSBedrock

from survey_art.settings import get_settings

logger = logging.getLogger(__name__)

# File types a county viewer can hand back that are worth keeping.
_DOCUMENT_SUFFIXES = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}


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


async def run_agent(task: str) -> tuple[Agent, str, tuple[float, int, int]]:
    """Run one browser-use agent on Bedrock. Returns `(agent, final_text, agent_cost)`."""
    s = get_settings()
    agent = Agent(
        task=task,
        llm=ChatAWSBedrock(model=s.model, aws_region=s.aws_region),
        use_thinking=False,
        calculate_cost=True,
    )
    result = await agent.run()
    return agent, str(result).strip(), agent_cost(agent)


def copy_agent_downloads(
    agent: Agent, dest_dir: Path, *, exclude_stems: tuple[str, ...] = ()
) -> list[Path]:
    """Copy the documents a browser-use agent downloaded (into its own temp dir)
    to `dest_dir`. A file whose name contains any of `exclude_stems` is skipped."""
    saved: list[Path] = []
    for p in map(Path, agent.available_file_paths or []):
        if p.suffix.lower() not in _DOCUMENT_SUFFIXES or not p.exists():
            continue
        if any(frag in p.stem.lower() for frag in exclude_stems):
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved.append(Path(shutil.copy(p, dest_dir / p.name)))
    return saved


async def run_download_agent(
    task: str, dest_dir: Path, *, exclude_stems: tuple[str, ...] = ()
) -> tuple[list[Path], float, int, int]:
    """Run an agent whose job is downloading documents, and collect them into
    `dest_dir`. Returns `(saved_paths, cost_usd, in_tokens, out_tokens)`."""
    agent, _, (cost, in_tok, out_tok) = await run_agent(task)
    saved = copy_agent_downloads(agent, dest_dir, exclude_stems=exclude_stems)
    logger.info("Browser agent downloaded %d file(s) to %s", len(saved), dest_dir)
    return saved, cost, in_tok, out_tok
