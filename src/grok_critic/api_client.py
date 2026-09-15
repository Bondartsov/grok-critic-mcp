# FILE: src/grok_critic/api_client.py
# VERSION: 1.11.1
# START_MODULE_CONTRACT
#   PURPOSE: Async HTTP client for the Polza.AI Responses API
#   SCOPE: Build and send requests, parse responses, handle errors, track usage/cost
#   DEPENDS: M-CONFIG, httpx
#   LINKS: M-API
# END_MODULE_CONTRACT

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from grok_critic.config import config

logger = logging.getLogger("grok-critic.api_client")

# Backward-compat aliases — каноничные значения живут в config
# (max_content_chars / max_retries / retry_backoff_base) и читаются на каждый вызов,
# чтобы reload_config подхватывал их без рестарта.
MAX_CONTENT_CHARS = 100_000  # ~100KB — защита от DoS по стоимости
MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 2.0  # seconds


# START_BLOCK_RUNTIME_GUARDS
# FEAT-BUDGET: суточная статистика использования (сбрасывается при смене даты).
_usage_stats: dict[str, Any] = {
    "date": "",
    "calls": 0,
    "errors": 0,
    "cost_usd": 0.0,
    "cost_rub": 0.0,
}


def get_usage_stats() -> dict[str, Any]:
    """Копия статистики за сегодня: вызовы, ошибки, стоимость. Сброс по смене даты."""
    today = datetime.date.today().isoformat()
    if _usage_stats["date"] != today:
        _usage_stats["date"] = today
        _usage_stats["calls"] = 0
        _usage_stats["errors"] = 0
        _usage_stats["cost_usd"] = 0.0
        _usage_stats["cost_rub"] = 0.0
    return dict(_usage_stats)


def _record_result(result: CritiqueResult) -> None:
    """Учёт результата реального запроса в суточной статистике (FEAT-BUDGET)."""
    get_usage_stats()  # триггерим rollover по дате
    if result.success:
        _usage_stats["calls"] += 1
        _usage_stats["cost_usd"] += result.cost_usd
        _usage_stats["cost_rub"] += result.cost_rub or 0.0
    else:
        _usage_stats["errors"] += 1


# FEAT-BUDGET: semaphore ограничивает число одновременных платных запросов.
_semaphore: asyncio.Semaphore | None = None
_semaphore_limit: int | None = None


def _get_semaphore(limit: int) -> asyncio.Semaphore:
    global _semaphore, _semaphore_limit
    if _semaphore is None or _semaphore_limit != limit:
        _semaphore = asyncio.Semaphore(limit)
        _semaphore_limit = limit
        logger.debug("[APIClient][_get_semaphore][GUARD] concurrency limit=%d", limit)
    return _semaphore


# REL-06 companion: in-flight dedup — параллельный вызов с тем же контентом
# присоединяется к уже летящему запросу вместо второго платного вызова.
# Значения — asyncio.Task; подписчики ждут через asyncio.shield, поэтому
# отмена одного подписчика не убивает общий запрос и не подвешивает остальных.
_inflight: dict[str, asyncio.Task[CritiqueResult]] = {}


def _inflight_get(key: str) -> asyncio.Task[CritiqueResult] | None:
    task = _inflight.get(key)
    if task is not None and task.done():
        return None
    return task


# END_BLOCK_RUNTIME_GUARDS

# agent_count → reasoning.effort mapping.
# According to xAI docs, only 2 modes exist:
#   4 agents → effort "low"
#  16 agents → effort "high"


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
        return min(base, config.timeout_low)
    if agent_count <= 8:
        return min(base, config.timeout_mid)
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
        # follow_redirects=True: Polza.AI может отвечать 3xx при смене endpoint'ов.
        _client = httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=True)
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
        self._api_key = config.api_key.get_secret_value()
        self._model = config.model
        self._timeout_seconds = config.timeout_seconds
        logger.info(
            "[APIClient][__init__][INIT] base_url=%s model=%s timeout=%ds",
            self._base_url,
            self._model,
            self._timeout_seconds,
        )

    @staticmethod
    def _dedup_key(
        prompt: str,
        agent_count: int,
        system_prompt: str | None,
        messages: list[dict[str, str]] | None,
    ) -> str:
        """Стабильный ключ in-flight dedup: модель + усилие + полный payload сообщений."""
        sys_part = system_prompt or ""
        if messages:
            sys_part = next((m["content"] for m in messages if m.get("role") == "system"), "")
        raw = json.dumps({"p": prompt, "a": agent_count, "s": sys_part}, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def call(
        self,
        prompt: str,
        agent_count: int = 4,
        system_prompt: str | None = None,
        messages: list[dict[str, str]] | None = None,
    ) -> CritiqueResult:
        """Публичная точка входа: budget-guard → in-flight dedup → semaphore → транспорт."""
        # FEAT-BUDGET: превышение дневного лимита — отказ ДО обращения к платному API.
        budget = config.daily_budget_usd
        if isinstance(budget, (int, float)) and budget > 0:
            spent = get_usage_stats()["cost_usd"]
            if spent >= budget:
                logger.warning(
                    "[APIClient][call][BUDGET] Exceeded: spent=$%.4f limit=$%.2f", spent, budget
                )
                return CritiqueResult(
                    text="", model=self._model, agent_count=agent_count,
                    effort=_resolve_effort(agent_count),
                    error=(
                        f"Превышен дневной бюджет: ${spent:.4f} из ${budget:.2f}. "
                        "Увеличьте POLZA_DAILY_BUDGET_USD или дождитесь следующего дня."
                    ),
                )

        key = self._dedup_key(prompt, agent_count, system_prompt, messages)
        existing = _inflight_get(key)
        if existing is not None:
            logger.info("[APIClient][call][DEDUP] join in-flight request key=%s…", key[:12])
            return await asyncio.shield(existing)

        limit_raw = config.max_concurrent_requests
        limit = limit_raw if isinstance(limit_raw, int) and limit_raw >= 1 else 2
        async with _get_semaphore(limit):
            # Double-check после ожидания слота: пока ждали semaphore,
            # такой же запрос мог начать кто-то другой.
            existing = _inflight_get(key)
            if existing is not None:
                logger.info("[APIClient][call][DEDUP] join after semaphore key=%s…", key[:12])
                return await asyncio.shield(existing)
            task = asyncio.create_task(
                self._perform_request(prompt, agent_count, system_prompt, messages)
            )
            _inflight[key] = task
            try:
                return await asyncio.shield(task)
            finally:
                if _inflight.get(key) is task:
                    _inflight.pop(key, None)

    async def _perform_request(
        self,
        prompt: str,
        agent_count: int,
        system_prompt: str | None,
        messages: list[dict[str, str]] | None,
    ) -> CritiqueResult:
        """Транспорт + однократный учёт результата в суточной статистике."""
        result = await self._request_once(prompt, agent_count, system_prompt, messages)
        _record_result(result)
        return result

    async def _request_once(
        self,
        prompt: str,
        agent_count: int = 4,
        system_prompt: str | None = None,
        messages: list[dict[str, str]] | None = None,
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

        input_messages: list[dict[str, str]] = list(messages) if messages else []
        if not messages:
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
        effective_system = system_prompt
        if effective_system is None:
            effective_system = next(
                (m["content"] for m in input_messages if m.get("role") == "system"), None
            )
        if effective_system:
            # Детерминированный хэш: встроенный hash() рандомизирован PYTHONHASHSEED
            # и менялся бы при каждом рестарте процесса, убивая prompt caching (BUG-03).
            cache_key = f"gc-{hashlib.sha256(effective_system.encode('utf-8')).hexdigest()[:8]}"
            body["prompt_cache_key"] = cache_key

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self._base_url}/responses"

        # START_BLOCK_SEND_WITH_RETRY
        resp: httpx.Response | None = None
        last_error = ""
        max_retries = config.max_retries
        backoff = config.retry_backoff_base
        # REL-06: общий дедлайн retry-цикла. По умолчанию = per-attempt timeout,
        # чтобы суммарное время не превышало таймаут MCP-клиента (иначе клиент
        # отваливается по своему таймауту и платит за повтор поверх ещё
        # выполняющегося запроса).
        rd_cfg = config.retry_deadline_seconds
        deadline = (
            float(rd_cfg) if isinstance(rd_cfg, (int, float)) and rd_cfg > 0 else float(timeout)
        )
        monotonic = asyncio.get_running_loop().time
        started_at = monotonic()

        for attempt in range(max_retries + 1):
            remaining = deadline - (monotonic() - started_at)
            if attempt > 0 and remaining < 1.0:
                logger.error(
                    "[APIClient][call][DEADLINE] Retry deadline %.0fs exhausted after %d attempt(s)",
                    deadline,
                    attempt,
                )
                last_error = last_error or f"Исчерпан retry-дедлайн ({deadline:.0f} c)"
                break
            request_timeout = httpx.Timeout(min(timeout, max(remaining, 1.0)))
            try:
                client = await get_client()
                resp = await client.post(url, json=body, headers=headers, timeout=request_timeout)
            except httpx.TransportError as exc:
                # httpx.TransportError — базовый класс для ConnectError, ReadError,
                # RemoteProtocolError, ConnectTimeout и пр. (TimeoutException — тоже его
                # наследник). Сетевые сбои ретраим наравне с таймаутами (REL-01).
                is_timeout = isinstance(exc, httpx.TimeoutException)
                last_error = (
                    "Превышен таймаут запроса"
                    if is_timeout
                    else f"Сетевая ошибка: {type(exc).__name__}"
                )
                logger.error(
                    "[APIClient][call][CALL] %s (attempt %d/%d): %s",
                    last_error, attempt + 1, max_retries + 1, exc,
                )
                if attempt < max_retries and remaining > backoff ** attempt:
                    # Пересоздаём клиент: пул мог сохранить мёртвое keep-alive соединение
                    # (классическая причина RemoteProtocolError).
                    await close_client()
                    await asyncio.sleep(backoff ** attempt)
                    continue
                return CritiqueResult(
                    text="", model=self._model, agent_count=agent_count,
                    effort=effort, review_id=review_id, error=last_error,
                )

            # Retryable status codes: 429 (rate limit) and 5xx (server error)
            wait = backoff ** attempt
            if resp.status_code in (429, *range(500, 600)) and attempt < max_retries and remaining > wait:
                logger.warning(
                    "[APIClient][call][RETRY] %d — retrying in %.1fs (attempt %d/%d)",
                    resp.status_code, wait, attempt + 1, max_retries + 1,
                )
                await asyncio.sleep(wait)
                continue
            break  # non-retryable or last attempt — proceed to error handling

        if resp is None:
            # Safety net: all retry paths exhausted without a response.
            logger.error("[APIClient][call][ERROR] No response received after %d attempts", max_retries + 1)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id,
                error=last_error or "Нет ответа от сервера",
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
            msg = api_error_msg or "API-ключ недействителен"
            logger.error("[APIClient][call][ERROR] 401 — %s", msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Ошибка авторизации: {msg}",
            )
        if resp.status_code == 402:
            msg = api_error_msg or "Недостаточно средств на балансе"
            logger.error("[APIClient][call][ERROR] 402 — %s", msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Недостаточно средств: {msg}",
            )
        if resp.status_code == 429:
            msg = api_error_msg or "Превышен лимит запросов"
            logger.error("[APIClient][call][ERROR] 429 — %s (all retries exhausted)", msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Превышен лимит запросов: {msg}",
            )
        if resp.status_code == 502:
            msg = api_error_msg or "Провайдер недоступен"
            logger.error("[APIClient][call][ERROR] 502 — %s", msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Провайдер недоступен: {msg}",
            )
        if resp.status_code == 503:
            msg = api_error_msg or "Нет доступных провайдеров"
            logger.error("[APIClient][call][ERROR] 503 — %s", msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Нет доступных провайдеров: {msg}",
            )
        if resp.status_code >= 500:
            msg = api_error_msg or f"HTTP {resp.status_code}"
            logger.error("[APIClient][call][ERROR] %d — %s", resp.status_code, msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Ошибка сервера: {msg}",
            )
        if resp.status_code >= 400:
            msg = api_error_msg or f"HTTP {resp.status_code}"
            logger.error("[APIClient][call][ERROR] %d — %s", resp.status_code, msg)
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id, error=f"Ошибка клиента: {msg}",
            )
        # END_BLOCK_ERROR_HANDLING

        try:
            payload = resp.json()
        except json.JSONDecodeError:
            logger.error("[APIClient][call][ERROR] Invalid JSON in response")
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id,
                error="Некорректный JSON в ответе API",
            )

        text = _extract_text(payload)
        if not text.strip():
            # REL-05: 200 OK, но текст не извлёкся — неожиданный формат payload.
            # Возвращаем явную ошибку вместо молчаливого пустого "успеха".
            logger.error(
                "[APIClient][call][ERROR] Empty text in 200 response, payload keys: %s",
                list(payload.keys()),
            )
            return CritiqueResult(
                text="", model=self._model, agent_count=agent_count,
                effort=effort, review_id=review_id,
                error="Пустой ответ от провайдера (неожиданный формат payload)",
            )

        input_tokens, output_tokens, total_tokens, cost_rub, cached_tokens, reasoning_tokens = _extract_usage(payload)
        cost_usd = _calculate_cost(input_tokens, output_tokens)

        # Debug: log full usage payload to understand what Polza.AI actually returns
        usage_raw = payload.get("usage", {})
        logger.debug(
            "[APIClient][call][USAGE_RAW] usage=%s",
            {k: v for k, v in usage_raw.items()},
        )

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
