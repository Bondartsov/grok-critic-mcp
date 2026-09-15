# FILE: src/grok_critic/server.py
# VERSION: 1.11.0
# START_MODULE_CONTRACT
#   PURPOSE: FastMCP server exposing 8 tools for code review, architecture, security, admin
#   SCOPE: Register MCP tools, handle parameter parsing, format metadata, run server
#   DEPENDS: M-CRITIC, M-CONFIG, mcp
#   LINKS: M-SERVER
# END_MODULE_CONTRACT

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
import os
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from grok_critic.api_client import close_client
from grok_critic.config import config, reload_config
from grok_critic.critic import (
    do_architecture_review,
    do_security_audit,
    followup,
    general_review,
    health_check,
)

logger = logging.getLogger("grok-critic.server")


# START_BLOCK_FORMAT_METADATA
def _fmt(n: int) -> str:
    """Format number with space thousands separator: 123456 → '123 456'."""
    return f"{n:,}".replace(",", " ")


def _format_metadata(result, elapsed_sec: float | None = None) -> str:
    lines = [
        "",
        "---",
    ]
    if elapsed_sec is not None:
        lines.append(f"⏱ Elapsed: {elapsed_sec:.0f} s")
    lines.append(f"📊 Metadata: model={result.model} | agents={result.agent_count} | effort={result.effort}")
    lines.append(f"📈 Tokens: input={_fmt(result.input_tokens)} output={_fmt(result.output_tokens)} total={_fmt(result.total_tokens)}")
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


def _format_result(result, elapsed_sec: float | None = None) -> str:
    """Unified result formatting: error or text + metadata."""
    if not result.success:
        return f"❌ Error: {result.error}"
    return result.text + _format_metadata(result, elapsed_sec)


def _parse_json_loose(text: str) -> dict[str, Any] | None:
    """Парсинг JSON из ответа модели: чистый JSON, ```json-блок или объект в тексте."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        first_nl = candidate.find("\n")
        candidate = candidate[first_nl + 1 :] if first_nl != -1 else candidate
        candidate = candidate.strip()
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


# END_BLOCK_FORMAT_METADATA


# START_BLOCK_HELPERS
# SEC-02: denylist секретов через fnmatch-глобы. Прежняя версия сверяла ТОЧНЫЕ
# имена и пропускала .env.local, credentials.prod.json, id_rsa.pub и пр.
# Сопоставление по имени файла, без учёта регистра. Содержимое файла уходит
# во внешний API, поэтому секреты режем на входе.
_SENSITIVE_GLOBS: tuple[str, ...] = (
    ".env*",
    "*id_rsa*", "*id_ed25519*", "*id_ecdsa*", "*id_dsa*",
    "*credential*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "*.jks", "*.keystore",
    ".git-credentials*", ".netrc", ".htpasswd", ".npmrc", ".pypirc",
)
# Файлы внутри любого каталога .git (config содержит токены из remote-URL).
_SENSITIVE_GIT_DIR_FILES: tuple[str, ...] = ("config", "config.*")

# Верхний предел размера файла для file_path (1 МБ).
# Достаточно для любого исходника; большие файлы всё равно режутся MAX_CONTENT_CHARS.
_MAX_FILE_BYTES = 1_000_000


def _is_sensitive_file(path: Path) -> bool:
    name = path.name.lower()
    if any(fnmatch.fnmatch(name, pattern) for pattern in _SENSITIVE_GLOBS):
        return True
    parts_lower = [part.lower() for part in path.parts]
    return ".git" in parts_lower[:-1] and any(
        fnmatch.fnmatch(name, pattern) for pattern in _SENSITIVE_GIT_DIR_FILES
    )


def _allowed_roots() -> list[Path]:
    """Allowed base directories for file_path reads.

    Default: server CWD only. Extra roots come from POLZA_ALLOWED_READ_DIRS
    (os.pathsep-separated). Resolved once per call — дёшево и всегда актуально
    после reload_config.
    """
    roots = [Path.cwd().resolve()]
    extra = config.allowed_read_dirs or ""
    for part in extra.split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(part).expanduser().resolve())
    return roots


def _read_file_content(file_path: str) -> tuple[str, str | None]:
    """Read file content for review. Returns (content, error_message).

    Sandbox: файл обязан лежать внутри allowed roots (cwd + POLZA_ALLOWED_READ_DIRS)
    и не быть типичным файлом секретов. Защита от path traversal и эксфильтрации
    секретов во внешний API (SEC-01).
    """
    try:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            return "", f"File not found: {path}"
        if not path.is_file():
            return "", f"Not a file: {path}"
        if _is_sensitive_file(path):
            logger.warning("[Server][_read_file_content][DENIED] Sensitive file blocked: %s", path.name)
            return "", f"Access denied: sensitive file type ({path.name})"
        if not any(path.is_relative_to(root) for root in _allowed_roots()):
            logger.warning("[Server][_read_file_content][DENIED] Outside allowed dirs: %s", path)
            return "", (
                f"Access denied: {path} is outside allowed directories. "
                "Allowed: server working directory + POLZA_ALLOWED_READ_DIRS."
            )
        if path.stat().st_size > _MAX_FILE_BYTES:
            return "", f"File too large: {path} ({path.stat().st_size} bytes > {_MAX_FILE_BYTES})"
        content = path.read_text(encoding="utf-8", errors="replace")
        if not content.strip():
            return "", f"File is empty: {path}"
        return content, None
    except Exception as exc:
        return "", f"Cannot read file: {exc}"


def _validate_agent_count(agent_count: int | None) -> int | None:
    """Clamp agent_count to valid range: 1-64."""
    if agent_count is None:
        return None
    if agent_count < 1:
        return 1
    if agent_count > 64:
        return 64
    return agent_count


# END_BLOCK_HELPERS


# START_BLOCK_HEARTBEAT
# FEAT-PROGRESS: ревью с 16 агентами занимает 1–3 минуты. Без уведомлений
# клиент и пользователь не видят признаков жизни. Heartbeat шлёт info-логи
# в MCP-сессию каждые _HEARTBEAT_INTERVAL секунд, пока идёт вызов.
_HEARTBEAT_INTERVAL_SECONDS = 20.0


async def _heartbeat(ctx: Any, tool_name: str, stop: asyncio.Event, interval: float) -> None:
    """Периодические log-уведомления в MCP-сессию до установки stop-события."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return  # stop set — ревью завершилось
        except TimeoutError:
            pass
        try:
            await ctx.info(f"⏳ {tool_name}: ещё выполняется ({int(loop.time() - started)} c)…")
        except Exception as exc:  # уведомления не должны ронять ревью
            logger.debug("[Server][_heartbeat][NOTIFY] notify failed: %s", exc)


# END_BLOCK_HEARTBEAT


# START_BLOCK_DECORATOR
def _review_tool(tool_name: str, *, allow_file_path: bool = True) -> Callable:
    """Decorator: logging + heartbeat + try/except + _format_result for review tools.

    Wrapped function returns a CritiqueResult; decorator handles formatting and errors.

    - allow_file_path=False (critic_followup): file_path не поддерживается —
      явная ошибка вместо TypeError (BUG-01).
    - SEC-03: чтение файлов через file_path — явный opt-in POLZA_ALLOW_FILE_PATH=true.
    - FEAT-PROGRESS: при наличии ctx из FastMCP шлёт heartbeat-уведомления,
      в metadata добавляется ⏱ Elapsed.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            # Validate agent_count
            if "agent_count" in kwargs:
                kwargs["agent_count"] = _validate_agent_count(kwargs.get("agent_count"))

            # FEAT-PROGRESS: ctx инжектится FastMCP по аннотации Context; прямые
            # вызовы в тестах идут без него — heartbeat просто не запускается.
            ctx = kwargs.pop("ctx", None)
            stop = asyncio.Event()
            hb_task = (
                asyncio.create_task(_heartbeat(ctx, tool_name, stop, _HEARTBEAT_INTERVAL_SECONDS))
                if ctx is not None
                else None
            )

            # Resolve file_path → content
            file_path = kwargs.pop("file_path", None)
            try:
                if file_path and not allow_file_path:
                    return f"❌ {tool_name} does not support file_path (pass content directly)"
                if file_path:
                    # SEC-03: чтение файлов выключено по умолчанию — cwd MCP-клиента
                    # непредсказуем (часто $HOME), поэтому включается явным флагом.
                    if not config.allow_file_path:
                        logger.warning(
                            "[Server][%s][DENIED] file_path disabled by POLZA_ALLOW_FILE_PATH",
                            tool_name,
                        )
                        return (
                            "❌ file_path отключён (SEC-03): установите "
                            "POLZA_ALLOW_FILE_PATH=true в .env и вызовите reload_config_tool, "
                            "либо передайте content напрямую."
                        )
                    # to_thread: синхронное чтение файла не блокирует event loop (REL-02)
                    file_content, err = await asyncio.to_thread(_read_file_content, file_path)
                    if err:
                        return f"❌ {err}"
                    kwargs["content"] = file_content
                    if kwargs.get("context") is None:
                        kwargs["context"] = f"File: {file_path}"

                content_len = len(kwargs.get("content", ""))
                agent_count = kwargs.get("agent_count")
                logger.info(
                    "[Server][%s][TOOL_CALL] content_len=%d agent_count=%s",
                    tool_name, content_len, agent_count,
                )

                loop = asyncio.get_running_loop()
                started_at = loop.time()
                try:
                    result = await func(*args, **kwargs)
                    elapsed = loop.time() - started_at
                    return _format_result(result, elapsed_sec=elapsed)
                except Exception as exc:
                    logger.exception("[Server][%s][ERROR]", tool_name)
                    return f"❌ {tool_name} failed: {exc}"
            finally:
                if hb_task is not None:
                    stop.set()
                    hb_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await hb_task
        return wrapper
    return decorator


# END_BLOCK_DECORATOR


# START_BLOCK_SERVER_INIT
server = FastMCP("grok-critic")


# END_BLOCK_SERVER_INIT


# START_BLOCK_TOOL_CRITIC_REVIEW
@server.tool()
@_review_tool("critic_review")
async def critic_review(
    content: str,
    context: str | None = None,
    agent_count: int | None = None,
    focus_areas: str | None = None,
    output_format: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Perform a critical code review using grok-4.20-multi-agent.

    Args:
        content: The code to review.
        context: Optional context about the code (project, language, purpose).
        agent_count: Number of reasoning agents (4=low, 16=high effort). Defaults to config value.
        focus_areas: Comma-separated focus areas (e.g. 'security,performance').
        output_format: 'json' for strict JSON output (summary + findings), omit for text.
    """
    areas: list[str] | None = None
    if focus_areas:
        areas = [a.strip() for a in focus_areas.split(",") if a.strip()]

    want_json = (output_format or "").strip().lower() == "json"
    result = await general_review(
        content=content,
        context=context,
        agent_count=agent_count if agent_count is not None else config.agent_count,
        focus_areas=areas,
        output_format="json" if want_json else None,
    )

    # FEAT-JSON: нормализуем ответ модели к чистому JSON, если модель его соблюла.
    if want_json and result.success:
        parsed = _parse_json_loose(result.text)
        if parsed is not None:
            result.text = json.dumps(parsed, ensure_ascii=False, indent=2)
        else:
            result.text = "⚠️ Модель вернула не-JSON, сырой ответ:\n\n" + result.text

    return result


# END_BLOCK_TOOL_CRITIC_REVIEW


# START_BLOCK_TOOL_CRITIC_FOLLOWUP
@server.tool()
@_review_tool("critic_followup", allow_file_path=False)
async def critic_followup(
    question: str,
    previous_review: str | None = None,
    agent_count: int | None = None,
    review_id: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Ask a follow-up question about a previous code review.

    Args:
        previous_review: The full text of the previous review (OR pass review_id instead — cheaper).
        question: Your follow-up question.
        agent_count: Override agent count (4=fast, 16=deep). Defaults to config.
        review_id: Review ID from metadata footer — server replays the stored
            dialogue instead of resending the full review text (saves ~25k input tokens).
    """
    return await followup(
        previous_review=previous_review or None,
        question=question,
        agent_count=agent_count,
        review_id=review_id,
    )


# END_BLOCK_TOOL_CRITIC_FOLLOWUP


# START_BLOCK_TOOL_HEALTH_CHECK
@server.tool()
async def check_health() -> str:
    """Check the health of the grok-critic MCP server and configuration."""
    logger.info("[Server][check_health][TOOL_CALL] Health check requested")
    try:
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
        if "usage_today" in result:
            usage = result["usage_today"]
            lines.append(
                f"📊 Today: {usage['calls']} calls | "
                f"${usage['cost_usd']:.4f} | {usage['cost_rub']:.2f} ₽"
                f" ({usage['errors']} errors)"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.exception("[Server][check_health][ERROR]")
        return f"❌ Health check failed: {exc}"


# END_BLOCK_TOOL_HEALTH_CHECK


# START_BLOCK_TOOL_ARCHITECTURE_REVIEW
@server.tool()
@_review_tool("architecture_review")
async def architecture_review(
    content: str,
    context: str | None = None,
    agent_count: int | None = None,
    ctx: Context | None = None,
) -> str:
    """Specialized architecture review: patterns, dependencies, scalability, risks.

    Args:
        content: Architecture description, diagram, or code to review.
        context: Optional project context (tech stack, constraints, team size).
        agent_count: Override agent count (4=fast, 16=deep). Defaults to config.
    """
    return await do_architecture_review(
        content=content,
        context=context,
        agent_count=agent_count,
    )


# END_BLOCK_TOOL_ARCHITECTURE_REVIEW


# START_BLOCK_TOOL_SECURITY_AUDIT
@server.tool()
@_review_tool("security_audit")
async def security_audit(
    content: str,
    context: str | None = None,
    agent_count: int | None = None,
    ctx: Context | None = None,
) -> str:
    """Specialized security audit: injection, auth, secrets, infrastructure.

    Args:
        content: Code or configuration to audit for security vulnerabilities.
        context: Optional context (framework, deployment, threat model).
        agent_count: Override agent count (4=fast, 16=deep). Defaults to config.
    """
    return await do_security_audit(
        content=content,
        context=context,
        agent_count=agent_count,
    )


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
        api_key_val = new_cfg.api_key.get_secret_value()
        masked_key = f"***{api_key_val[-4:]}" if len(api_key_val) > 4 else "(not set)"
        lines = [
            "✅ Config reloaded from .env",
            f"  api_key: {masked_key}",
            f"  base_url: {new_cfg.base_url}",
            f"  model: {new_cfg.model}",
            f"  agent_count: {new_cfg.agent_count}",
            f"  timeout_seconds: {new_cfg.timeout_seconds}",
            f"  log_level: {new_cfg.log_level}",
            f"  price_input_per_1m: ${new_cfg.price_input_per_1m}",
            f"  price_output_per_1m: ${new_cfg.price_output_per_1m}",
            f"  allow_file_path: {new_cfg.allow_file_path}",
            f"  daily_budget_usd: ${new_cfg.daily_budget_usd}",
            f"  max_concurrent_requests: {new_cfg.max_concurrent_requests}",
            f"  retry_deadline_seconds: {new_cfg.retry_deadline_seconds or '(auto = timeout_seconds)'}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        logger.exception("[Server][reload_config_tool][ERROR]")
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

    if not config.allow_self_update:
        return "❌ self_update is disabled. Set POLZA_ALLOW_SELF_UPDATE=true in .env and reload_config."

    lines: list[str] = ["🔄 Self-update started..."]
    repo_dir = str(Path(__file__).resolve().parents[2])

    # FIX-ENV-PATH: self_update осмыслен только для editable/git-установки.
    # При обычном pip install parents[2] — site-packages: git pull там невалиден.
    repo_path = Path(repo_dir)
    if not (repo_path / "pyproject.toml").is_file() or not (repo_path / ".git").exists():
        logger.error("[Server][self_update][ERROR] Not a git checkout: %s", repo_dir)
        return (
            "❌ self_update требует установки из git-клона (pip install -e .): "
            f"{repo_dir} не похож на репозиторий. Обновите пакет вручную."
        )

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
    except TimeoutError:
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

        lines.append("📥 pip install: OK")
    except TimeoutError:
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
