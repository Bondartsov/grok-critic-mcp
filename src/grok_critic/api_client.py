# FILE: src/grok_critic/api_client.py
# VERSION: 1.6.1
# START_MODULE_CONTRACT
#   PURPOSE: Async HTTP client for the Polza.AI Responses API
#   SCOPE: Build and send requests, parse responses, handle errors, track usage/cost
#   DEPENDS: M-CONFIG, httpx
#   LINKS: M-API
# END_MODULE_CONTRACT

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from grok_critic.config import config

logger = logging.getLogger("grok-critic.api_client")

MAX_CONTENT_CHARS = 100_000  # ~100KB — защита от DoS по стоимости

# agent_count → reasoning.effort mapping.
# According to xAI docs, only 2 modes exist:
#   4 agents → effort "low"
#  16 agents → effort "high"

MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 2.0  # seconds


# START_BLOCK_CRITIQUE_RESULT
@dataclass
class CritiqueResult:
    text: str
    model: str
    agent_count: int
    effort: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cost_rub: float | None = None  # Actual cost from Polza.AI API (usage.cost_rub)
    cached_tokens: int = 0  # Tokens served from cache (prompt_tokens_details.cached_tokens)
    reasoning_tokens: int = 0  # Reasoning tokens (completion_tokens_details.reasoning_tokens) — most expensive part
    review_id: str = ""
    error: str = ""

    @property
    def success(self) -> bool:
        return not self.error


# END_BLOCK_CRITIQUE_RESULT


# START_BLOCK_EFFORT_MAPPING
def _resolve_effort(agent_count: int) -> str:
    """Map agent_count to reasoning.effort.

    Only 4 (low) and 16 (high) are officially supported by xAI.
    Any other value falls back to nearest supported mode.
    """
    if agent_count <= 4:
        return "low"
    return "high"


# END_BLOCK_EFFORT_MAPPING


# START_BLOCK_DYNAMIC_TIMEOUT
def _resolve_timeout(agent_count: int) -> int:
    """Dynamic timeout: fewer agents → shorter timeout."""
    base = config.timeout_seconds
    if agent_count <= 4:
        return min(base, 90)
    if agent_count <= 8:
        return min(base, 150)
    return base  # 16+ agents — full configured timeout (default 180s+)


# END_BLOCK_DYNAMIC_TIMEOUT


# START_BLOCK_RESPONSE_PARSER
def _extract_text(payload: dict[str, Any]) -> str:
    if output_text := payload.get("output_text"):
        return output_text

    output_items = payload.get("output", [])
    for item in output_items:
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "")
    return ""


# END_BLOCK_RESPONSE_PARSER


# START_BLOCK_USAGE_EXTRACTION
def _extract_usage(payload: dict[str, Any]) -> tuple[int, int, int, float | None, int, int]:
    usage = payload.get("usage", {})
    cost_rub = usage.get("cost_rub") or usage.get("cost")
    # cached_tokens can be in prompt_tokens_details or input_tokens_details
    in_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached_tokens = in_details.get("cached_tokens", 0) or 0
    # reasoning_tokens from completion_tokens_details or output_tokens_details
    out_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    reasoning_tokens = out_details.get("reasoning_tokens", 0) or 0
    return (
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
        usage.get("total_tokens", 0),
        float(cost_rub) if cost_rub is not None else None,
        cached_tokens,
        reasoning_tokens,
    )


# END_BLOCK_USAGE_EXTRACTION


# START_BLOCK_COST_CALCULATION
def _calculate_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000 * config.price_input_per_1m) + (
        output_tokens / 1_000_000 * config.price_output_per_1m
    )


# END_BLOCK_COST_CALCULATION


# START_BLOCK_PERSISTENT_CLIENT
_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    """Get or create a persistent httpx.AsyncClient.

    Timeout не задаётся здесь — он передаётся в каждый .post() вызов
    через httpx.Timeout для поддержки динамического timeout по agent_count.
    """
    global _client
    if _client is None or _client.is_closed:
        # Базовый timeout = максимальный из конфига. Реальный — через timeout в .post()
        _client = httpx.AsyncClient(timeout=config.timeout_seconds)
        logger.info("[APIClient][get_client][INIT] Created persistent client")
    return _client


async def close_client() -> None:
    """Gracefully close the persistent client. Called on server shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        logger.info("[APIClient][close_client][CLOSE] Client closed")
    _client = None


# END_BLOCK_PERSISTENT_CLIENT


# START_BLOCK_RESPONSES_CLIENT
class ResponsesClient:
    def __init__(self) -> None:
        self._base_url = config.base_url
        self._api_key = config.api_key
        self._model = config.model
        self._timeout_seconds = config.timeout_seconds
        logger.info(
            "[APIClient][__init__][INIT] base_url=%s model=%s timeout=%ds",
            self._base_url,
            self._model,
            self._timeout_seconds,
        )

    async def call(
        self,
        prompt: str,
        agent_count: int = 4,
        system_prompt: str | None = None,
    ) -> CritiqueResult:
        effort = _resolve_effort(agent_count)
        timeout = _resolve_timeout(agent_count)
        review_id = f"rev_{uuid.uuid4().hex[:12]}"
        logger.info(
            "[APIClient][call][CALL] agent_count=%d effort=%s timeout=%ds prompt_len=%d review_id=%s",
            agent_count,
            effort,
            timeout,
            len(prompt),
            review_id,
        )

        input_messages: list[dict[str, str]] = []
        if system_prompt:
            input_messages.append({"role": "system", "content": system_prompt})
        input_messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {
            "model": self._model,
            "reasoning": {"effort": effort},
            "input": input_messages,
        }

        # Enable prompt caching via Polza.AI's prompt_cache_key parameter.
        # Stable key per system prompt type maximises cache hit rate.
        if system_prompt:
            # Use a hash of the system prompt as cache key
            cache_key = f"gc-{hash(system_prompt) & 0xFFFFFFFF:x}"
            body["prompt_cache_key"] = cache_key

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self._base_url}/responses"

        # START_BLOCK_SEND_WITH_RETRY
        resp: httpx.Response | None = None
        last_error = ""
        request_timeout = httpx.Timeout(timeout)
        for attempt in range(MAX_RETRIES + 1):
            try:
                client = await get_client()
                resp = await client.post(url, json=body, headers=headers, timeout=request_timeout)
            except httpx.TimeoutException:
                logger.error(
                    "[APIClient][call][CALL] Timeout after %ds (attempt %d/%d)",
                    timeout, attempt + 1, MAX_RETRIES + 1,
                )
                last_error = "Request timed out"
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF_BASE ** attempt)
                    continue
                return CritiqueResult(
                    text="", model=self._model, agent_count=agent_count,
                    effort=effort, review_id=review_id, error=last_error,
                )

            # Retryable status codes: 429 (rate limit) and 5xx (server error)
            if resp.status_code in (429, *range(500, 600)) and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "[APIClient][call][RETRY] %d — retrying in %.1fs (attempt %d/%d)",
                    resp.status_code, wait, attempt + 1, MAX_RETRIES + 1,
                )
                await asyncio.sleep(wait)
                continue
            break  # non-retryable or last attempt — proceed to error handling

        if resp is None:
            # Safety net: all retry paths exhausted without a response.
            logger.error("[APIClient][call][ERROR] No response received after %d attempts", MAX_RETRIES + 1)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id,
                error=last_error or "No response from server",
            )
        # END_BLOCK_SEND_WITH_RETRY

        # START_BLOCK_ERROR_HANDLING
        # Polza.AI returns: {"error": {"code": "...", "message": "..."}}
        api_error_msg = ""
        try:
            err_body = resp.json()
            api_error_msg = err_body.get("error", {}).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            api_error_msg = resp.text[:200] if resp.text else ""

        if resp.status_code == 401:
            msg = api_error_msg or "API key invalid"
            logger.error("[APIClient][call][ERROR] 401 — %s", msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Auth error: {msg}",
            )
        if resp.status_code == 402:
            msg = api_error_msg or "Insufficient funds"
            logger.error("[APIClient][call][ERROR] 402 — %s", msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Payment required: {msg}",
            )
        if resp.status_code == 429:
            msg = api_error_msg or "Rate limit exceeded"
            logger.error("[APIClient][call][ERROR] 429 — %s (all retries exhausted)", msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Rate limited: {msg}",
            )
        if resp.status_code == 502:
            msg = api_error_msg or "Provider unavailable"
            logger.error("[APIClient][call][ERROR] 502 — %s", msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Provider down: {msg}",
            )
        if resp.status_code == 503:
            msg = api_error_msg or "No providers available"
            logger.error("[APIClient][call][ERROR] 503 — %s", msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"No providers: {msg}",
            )
        if resp.status_code >= 500:
            msg = api_error_msg or f"HTTP {resp.status_code}"
            logger.error("[APIClient][call][ERROR] %d — %s", resp.status_code, msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Server error: {msg}",
            )
        if resp.status_code >= 400:
            msg = api_error_msg or f"HTTP {resp.status_code}"
            logger.error("[APIClient][call][ERROR] %d — %s", resp.status_code, msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Client error: {msg}",
            )
        # END_BLOCK_ERROR_HANDLING

        try:
            payload = resp.json()
        except json.JSONDecodeError:
            logger.error("[APIClient][call][ERROR] Invalid JSON in response")
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error="Invalid JSON response",
            )

        text = _extract_text(payload)
        input_tokens, output_tokens, total_tokens, cost_rub, cached_tokens, reasoning_tokens = _extract_usage(payload)
        cost_usd = _calculate_cost(input_tokens, output_tokens)

        logger.info(
            "[APIClient][call][CALL] Response received, text_len=%d tokens=%d cost_usd=%.6f cost_rub=%s cached=%d reasoning=%d",
            len(text),
            total_tokens,
            cost_usd,
            f"{cost_rub:.4f}" if cost_rub is not None else "N/A",
            cached_tokens,
            reasoning_tokens,
        )

        return CritiqueResult(
            text=text,
            model=self._model,
            agent_count=agent_count,
            effort=effort,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            cost_rub=cost_rub,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            review_id=review_id,
        )


# END_BLOCK_RESPONSES_CLIENT
