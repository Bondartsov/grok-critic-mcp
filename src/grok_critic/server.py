# FILE: src/grok_critic/server.py
# VERSION: 1.6.1
# START_MODULE_CONTRACT
#   PURPOSE: FastMCP server exposing critic_review, critic_followup and health_check tools
#   SCOPE: Register MCP tools, handle parameter parsing, format metadata, run server
#   DEPENDS: M-CRITIC, M-CONFIG, mcp
#   LINKS: M-SERVER
# END_MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
import os
import subprocess

from mcp.server.fastmcp import FastMCP

from grok_critic.api_client import close_client
from grok_critic.config import config, reload_config
from grok_critic.critic import (
    do_architecture_review,
    do_security_audit,
    followup,
    health_check,
    structured_review,
)

logger = logging.getLogger("grok-critic.server")


# START_BLOCK_FORMAT_METADATA
def _fmt(n: int) -> str:
    """Format number with thin-space thousands separator: 123456 → '123 456'."""
    s = str(abs(n))
    groups: list[str] = []
    while s:
        groups.insert(0, s[-3:])
        s = s[:-3]
    result = "\u2009".join(groups)  # thin space U+2009
    return f"-{result}" if n < 0 else result


def _format_metadata(result) -> str:
    lines = [
        "",
        "---",
        f"📊 Metadata: model={result.model} | agents={result.agent_count} | effort={result.effort}",
        f"📈 Tokens: input={_fmt(result.input_tokens)} output={_fmt(result.output_tokens)} total={_fmt(result.total_tokens)}",
    ]
    if result.reasoning_tokens > 0:
        pct = (result.reasoning_tokens / result.output_tokens * 100) if result.output_tokens > 0 else 0
        lines.append(f"🧠 Reasoning: {_fmt(result.reasoning_tokens)} ({pct:.0f}% of output) — ~4x cost")
    if result.cached_tokens > 0:
        pct = (result.cached_tokens / result.input_tokens * 100) if result.input_tokens > 0 else 0
        lines.append(f"💾 Cached: {_fmt(result.cached_tokens)}/{_fmt(result.input_tokens)} ({pct:.0f}%)")
    cost_parts: list[str] = []
    if result.cost_rub is not None and result.cost_rub > 0:
        cost_parts.append(f"{result.cost_rub:.2f} ₽")
    if result.cost_usd > 0:
        cost_parts.append(f"${result.cost_usd:.4f}")
    if cost_parts:
        lines.append(f"💰 Cost: {' | '.join(cost_parts)}")
    lines.append(f"🏷️ Review ID: {result.review_id}")
    return "\n".join(lines)


def _format_result(result) -> str:
    """Unified result formatting: error or text + metadata."""
    if not result.success:
        return f"❌ Error: {result.error}"
    return result.text + _format_metadata(result)


# END_BLOCK_FORMAT_METADATA


# START_BLOCK_SERVER_INIT
server = FastMCP("grok-critic")


# END_BLOCK_SERVER_INIT


# START_BLOCK_TOOL_CRITIC_REVIEW
@server.tool()
async def critic_review(
    content: str,
    context: str | None = None,
    agent_count: int | None = None,
    focus_areas: str | None = None,
) -> str:
    """Perform a critical code review using grok-4.20-multi-agent.

    Args:
        content: The code to review.
        context: Optional context about the code (project, language, purpose).
        agent_count: Number of reasoning agents (4=low, 16=high effort). Defaults to config value.
        focus_areas: Comma-separated focus areas (e.g. 'security,performance').
    """
    logger.info(
        "[Server][critic_review][TOOL_CALL] content_len=%d agent_count=%s",
        len(content),
        agent_count,
    )

    areas: list[str] | None = None
    if focus_areas:
        areas = [a.strip() for a in focus_areas.split(",") if a.strip()]

    result = await structured_review(
        content=content,
        context=context,
        agent_count=agent_count if agent_count is not None else config.agent_count,
        focus_areas=areas,
    )

    return _format_result(result)


# END_BLOCK_TOOL_CRITIC_REVIEW


# START_BLOCK_TOOL_CRITIC_FOLLOWUP
@server.tool()
async def critic_followup(
    previous_review: str,
    question: str,
    agent_count: int | None = None,
) -> str:
    """Ask a follow-up question about a previous code review.

    Args:
        previous_review: The full text of the previous review.
        question: Your follow-up question.
        agent_count: Override agent count (4=fast, 16=deep). Defaults to config.
    """
    logger.info(
        "[Server][critic_followup][TOOL_CALL] prev_len=%d question_len=%d",
        len(previous_review),
        len(question),
    )

    result = await followup(
        previous_review=previous_review,
        question=question,
        agent_count=agent_count,
    )

    return _format_result(result)


# END_BLOCK_TOOL_CRITIC_FOLLOWUP


# START_BLOCK_TOOL_HEALTH_CHECK
@server.tool()
async def check_health() -> str:
    """Check the health of the grok-critic MCP server and configuration."""
    logger.info("[Server][check_health][TOOL_CALL] Health check requested")
    result = await health_check()
    lines = [f"Status: {result['status']}"]
    lines.append(f"Model: {result['model']}")
    lines.append(f"Base URL: {result['base_url']}")
    if result["issues"]:
        lines.append(f"Issues: {', '.join(result['issues'])}")
    if "pricing" in result:
        pricing = result["pricing"]
        lines.append(f"Pricing: input=${pricing['input_per_1m']}/1M output=${pricing['output_per_1m']}/1M")
    if "balance_rub" in result:
        lines.append(f"Balance: {result['balance_rub']:.2f} ₽")
    return "\n".join(lines)


# END_BLOCK_TOOL_HEALTH_CHECK


# START_BLOCK_TOOL_ARCHITECTURE_REVIEW
@server.tool()
async def architecture_review(
    content: str,
    context: str | None = None,
    agent_count: int | None = None,
) -> str:
    """Specialized architecture review: patterns, dependencies, scalability, risks.

    Args:
        content: Architecture description, diagram, or code to review.
        context: Optional project context (tech stack, constraints, team size).
        agent_count: Override agent count (4=fast, 16=deep). Defaults to config.
    """
    logger.info(
        "[Server][architecture_review][TOOL_CALL] content_len=%d agent_count=%s",
        len(content),
        agent_count,
    )

    result = await do_architecture_review(
        content=content,
        context=context,
        agent_count=agent_count,
    )

    return _format_result(result)


# END_BLOCK_TOOL_ARCHITECTURE_REVIEW


# START_BLOCK_TOOL_SECURITY_AUDIT
@server.tool()
async def security_audit(
    content: str,
    context: str | None = None,
    agent_count: int | None = None,
) -> str:
    """Specialized security audit: injection, auth, secrets, infrastructure.

    Args:
        content: Code or configuration to audit for security vulnerabilities.
        context: Optional context (framework, deployment, threat model).
        agent_count: Override agent count (4=fast, 16=deep). Defaults to config.
    """
    logger.info(
        "[Server][security_audit][TOOL_CALL] content_len=%d agent_count=%s",
        len(content),
        agent_count,
    )

    result = await do_security_audit(
        content=content,
        context=context,
        agent_count=agent_count,
    )

    return _format_result(result)


# END_BLOCK_TOOL_SECURITY_AUDIT


# START_BLOCK_TOOL_RELOAD_CONFIG
@server.tool()
async def reload_config_tool() -> str:
    """Hot-reload configuration from .env without restarting the server.

    Use when you change POLZA_* env vars (API key, prices, timeout, etc.)
    and want the server to pick up new values immediately.
    """
    logger.info("[Server][reload_config_tool][TOOL_CALL] Reloading config")
    try:
        new_cfg = reload_config()
        # Close stale HTTP client (it may have old base_url / timeout).
        await close_client()
        lines = [
            "✅ Config reloaded from .env",
            f"  api_key: {'***' + new_cfg.api_key[-4:] if len(new_cfg.api_key) > 4 else '(not set)'}",
            f"  base_url: {new_cfg.base_url}",
            f"  model: {new_cfg.model}",
            f"  agent_count: {new_cfg.agent_count}",
            f"  timeout_seconds: {new_cfg.timeout_seconds}",
            f"  log_level: {new_cfg.log_level}",
            f"  price_input_per_1m: ${new_cfg.price_input_per_1m}",
            f"  price_output_per_1m: ${new_cfg.price_output_per_1m}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        logger.error("[Server][reload_config_tool][ERROR] %s", exc)
        return f"❌ Reload failed: {exc}"


# END_BLOCK_TOOL_RELOAD_CONFIG


# START_BLOCK_TOOL_SELF_UPDATE
@server.tool()
async def self_update() -> str:
    """Update the server from GitHub (git pull + pip install) and restart.

    Pulls the latest code from the remote repository, reinstalls the package,
    then restarts the MCP server. The MCP client will auto-restart the process.
    Use this when a new version is pushed to GitHub and you want to update.
    """
    logger.info("[Server][self_update][TOOL_CALL] Starting self-update")

    lines: list[str] = ["🔄 Self-update started..."]
    repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # Step 1: git pull
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "pull",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        git_out = stdout.decode().strip()
        git_err = stderr.decode().strip()

        if proc.returncode != 0:
            return f"❌ git pull failed (code {proc.returncode}):\n{git_err or git_out}"

        if "Already up to date" in git_out:
            return f"✅ Already up to date. No changes to pull.\n{git_out}"

        lines.append(f"📦 git pull:\n{git_out}")
    except asyncio.TimeoutError:
        return "❌ git pull timed out (60s)"
    except Exception as exc:
        return f"❌ git pull error: {exc}"

    # Step 2: pip install -e .
    try:
        proc = await asyncio.create_subprocess_exec(
            "pip", "install", "-e", ".",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        pip_out = stdout.decode().strip()
        pip_err = stderr.decode().strip()

        if proc.returncode != 0:
            return f"❌ pip install failed (code {proc.returncode}):\n{pip_err or pip_out}"

        lines.append(f"📥 pip install: OK")
    except asyncio.TimeoutError:
        return "❌ pip install timed out (120s)"
    except Exception as exc:
        return f"❌ pip install error: {exc}"

    # Step 3: restart (MCP client will auto-restart the process)
    lines.append("🔄 Restarting server with new code...")
    logger.info("[Server][self_update][EXIT] Update complete, restarting")
    await close_client()
    os._exit(0)


# END_BLOCK_TOOL_SELF_UPDATE


# START_BLOCK_TOOL_RESTART_SERVER
@server.tool()
async def restart_server(reason: str | None = None) -> str:
    """Full server restart. Closes connections and exits the process.

    The MCP client (Kilo Code, Claude Code, etc.) will automatically
    restart the server process after it exits.

    Args:
        reason: Optional reason for restart (logged before exit).
    """
    logger.info(
        "[Server][restart_server][TOOL_CALL] Restarting server. reason=%s",
        reason or "(none)",
    )
    await close_client()
    log_msg = f"Restarting grok-critic MCP server. Reason: {reason or 'requested by agent'}"
    logger.info("[Server][restart_server][EXIT] %s", log_msg)
    # os._exit(0) — hard exit without running async cleanup.
    # The MCP client will detect the exit and restart the process.
    os._exit(0)


# END_BLOCK_TOOL_RESTART_SERVER


# START_BLOCK_ENTRY_POINT
def main() -> None:
    logger.info("[Server][main][ENTRY] Starting grok-critic MCP server")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()


# END_BLOCK_ENTRY_POINT
