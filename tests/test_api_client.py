# FILE: tests/test_api_client.py
# VERSION: 1.12.0
# START_MODULE_CONTRACT
#   PURPOSE: Tests for M-API ResponsesClient with mocked HTTP
#   SCOPE: call(), error handling, parsing, usage/cost (₽, tariff estimate), model pricing,
#          retry deadline, dedup, budget guard
#   DEPENDS: M-API, M-CONFIG
#   LINKS: M-API
# END_MODULE_CONTRACT

from __future__ import annotations

import asyncio
import datetime
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr

import grok_critic.api_client as api_mod
from grok_critic.api_client import (
    MAX_RETRIES,
    CritiqueResult,
    ModelPricing,
    ResponsesClient,
    _extract_text,
    _extract_usage,
    _resolve_effort,
    _resolve_timeout,
    close_client,
    estimate_cost_rub,
    get_client,
    get_model_pricing,
    get_usage_stats,
)
from grok_critic.config import config


# START_BLOCK_RUNTIME_STATE_RESET
@pytest.fixture(autouse=True)
def _reset_api_runtime_state(monkeypatch):
    """Чистит module-level состояние (stats/inflight/semaphore/кэш тарифа) между тестами.

    PRICING-RUB: разбор ответа без cost_rub зовёт get_model_pricing() — по умолчанию
    тариф «недоступен» (без сети). Тесты самой get_model_pricing используют
    импортированную по имени оригинальную функцию, а не атрибут модуля.
    """
    api_mod._usage_stats.update(
        {
            "date": datetime.date.today().isoformat(),
            "calls": 0,
            "errors": 0,
            "cost_rub": 0.0,
        }
    )
    api_mod._inflight.clear()
    api_mod._semaphore = None
    api_mod._semaphore_limit = None
    api_mod._pricing_cache = None
    api_mod._pricing_failure = None
    monkeypatch.setattr(api_mod, "get_model_pricing", AsyncMock(return_value=None))
    yield
    api_mod._pricing_cache = None
    api_mod._pricing_failure = None


# Тариф x-ai/grok-4.20-multi-agent из GET /models/{model} (проверено 16.09.2026).
MODEL_PAYLOAD: dict[str, object] = {
    "id": "x-ai/grok-4.20-multi-agent",
    "top_provider": {
        "pricing": {
            "prompt_per_million": "147.35000000",
            "completion_per_million": "294.70000000",
            "web_search_per_thousand": "589.40000000",
            "input_cache_read_per_million": "23.57600000",
            "currency": "RUB",
        },
        "context_length": 2000000,
        "max_completion_tokens": 1800000,
    },
}

GROK_PRICING = ModelPricing(
    input_per_1m_rub=147.35,
    output_per_1m_rub=294.70,
    cache_read_per_1m_rub=23.576,
    context_length=2_000_000,
    max_output_tokens=1_800_000,
)


# END_BLOCK_RUNTIME_STATE_RESET


# START_BLOCK_EFFORT_TESTS
class TestEffortMapping:
    def test_4_agents_is_low(self) -> None:
        assert _resolve_effort(4) == "low"

    def test_16_agents_is_high(self) -> None:
        assert _resolve_effort(16) == "high"

    def test_small_count_is_low(self) -> None:
        assert _resolve_effort(2) == "low"

    def test_large_count_is_high(self) -> None:
        assert _resolve_effort(20) == "high"


# END_BLOCK_EFFORT_TESTS


# START_BLOCK_EXTRACT_TEXT
class TestExtractText:
    def test_output_text_shortcut(self) -> None:
        payload = {"output_text": "hello world"}
        assert _extract_text(payload) == "hello world"

    def test_output_array(self) -> None:
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "from array"}],
                }
            ]
        }
        assert _extract_text(payload) == "from array"

    def test_empty_payload(self) -> None:
        assert _extract_text({}) == ""


# END_BLOCK_EXTRACT_TEXT


# START_BLOCK_EXTRACT_USAGE
class TestExtractUsage:
    def test_with_usage(self) -> None:
        payload = {"usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}}
        inp, out, total, cost_rub, cached, reasoning = _extract_usage(payload)
        assert inp == 100
        assert out == 50
        assert total == 150
        assert cost_rub is None
        assert cached == 0

    def test_with_cost_rub(self) -> None:
        payload = {"usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_rub": 1.23}}
        inp, out, total, cost_rub, cached, reasoning = _extract_usage(payload)
        assert inp == 100
        assert cost_rub == 1.23

    def test_with_cost_alias(self) -> None:
        payload = {"usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost": 2.50}}
        inp, out, total, cost_rub, cached, reasoning = _extract_usage(payload)
        assert cost_rub == 2.50

    @pytest.mark.parametrize("key", ["cost_rub", "cost"])
    def test_explicit_zero_cost_is_not_missing(self, key) -> None:
        """Честный 0 из API — это стоимость, а не её отсутствие (не уходит в оценку по тарифу)."""
        payload = {"usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, key: 0}}
        _, _, _, cost_rub, _, _ = _extract_usage(payload)
        assert cost_rub == 0.0

    def test_missing_usage(self) -> None:
        inp, out, total, cost_rub, cached, reasoning = _extract_usage({})
        assert inp == 0
        assert out == 0
        assert total == 0
        assert cost_rub is None
        assert cached == 0

    def test_partial_usage(self) -> None:
        payload = {"usage": {"input_tokens": 200}}
        inp, out, total, cost_rub, cached, reasoning = _extract_usage(payload)
        assert inp == 200
        assert out == 0
        assert total == 0
        assert cost_rub is None

    def test_with_cached_tokens(self) -> None:
        payload = {"usage": {
            "input_tokens": 2000, "output_tokens": 500, "total_tokens": 2500,
            "prompt_tokens_details": {"cached_tokens": 1800},
        }}
        inp, out, total, cost_rub, cached, reasoning = _extract_usage(payload)
        assert inp == 2000
        assert cached == 1800

    def test_with_input_tokens_details(self) -> None:
        payload = {"usage": {
            "input_tokens": 2000, "output_tokens": 500, "total_tokens": 2500,
            "input_tokens_details": {"cached_tokens": 1500},
        }}
        inp, out, total, cost_rub, cached, reasoning = _extract_usage(payload)
        assert cached == 1500

    def test_with_reasoning_tokens(self) -> None:
        payload = {"usage": {
            "input_tokens": 2000, "output_tokens": 5000, "total_tokens": 7000,
            "completion_tokens_details": {"reasoning_tokens": 4200},
        }}
        inp, out, total, cost_rub, cached, reasoning = _extract_usage(payload)
        assert reasoning == 4200

    def test_with_output_tokens_details(self) -> None:
        payload = {"usage": {
            "input_tokens": 2000, "output_tokens": 5000, "total_tokens": 7000,
            "output_tokens_details": {"reasoning_tokens": 3500},
        }}
        inp, out, total, cost_rub, cached, reasoning = _extract_usage(payload)
        assert reasoning == 3500


# END_BLOCK_EXTRACT_USAGE


# START_BLOCK_COST_ESTIMATE
class TestEstimateCostRub:
    """PRICING-RUB: оценка стоимости в ₽ по тарифу — сверка с фактической cost_rub API."""

    def test_matches_real_api_cost(self) -> None:
        # Реальный запрос: input=619370, cached=477162, output=72853 → API cost_rub=53.67 ₽
        cost = estimate_cost_rub(619_370, 72_853, 477_162, GROK_PRICING)
        assert round(cost, 2) == 53.67

    def test_cached_greater_than_input_clamped_to_input(self) -> None:
        """Аномальный cached > input: кэш — часть входа, тарифицируется не больше input."""
        cost = estimate_cost_rub(100, 0, 1_000, GROK_PRICING)
        assert cost >= 0.0
        assert cost == pytest.approx(100 * 23.576 / 1e6)

    def test_negative_cached_treated_as_zero(self) -> None:
        cost = estimate_cost_rub(1_000, 0, -5, GROK_PRICING)
        assert cost == pytest.approx(1_000 * 147.35 / 1e6)

    def test_zero_tokens(self) -> None:
        assert estimate_cost_rub(0, 0, 0, GROK_PRICING) == 0.0


# END_BLOCK_COST_ESTIMATE


# START_BLOCK_MODEL_PRICING
def _pricing_http(response: object) -> AsyncMock:
    """Мок общего httpx-клиента: .get возвращает response (или бросает исключение)."""
    mock_httpx = AsyncMock()
    if isinstance(response, BaseException):
        mock_httpx.get = AsyncMock(side_effect=response)
    else:
        mock_httpx.get = AsyncMock(return_value=response)
    return mock_httpx


class TestGetModelPricing:
    """PRICING-RUB: get_model_pricing — разбор тарифа, отказоустойчивость, TTL-кэш."""

    @pytest.fixture(autouse=True)
    def _cfg(self):
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.api_key = SecretStr("secret-key-xyz")
            yield mock_cfg

    async def test_parses_rub_from_top_provider(self) -> None:
        mock_httpx = _pricing_http(httpx.Response(200, json=MODEL_PAYLOAD))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=mock_httpx):
            pricing = await get_model_pricing()
        assert pricing == GROK_PRICING
        url = mock_httpx.get.call_args.args[0]
        assert url == "https://polza.ai/api/v1/models/x-ai/grok-4.20-multi-agent"
        headers = mock_httpx.get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer secret-key-xyz"

    async def test_fallback_to_first_provider(self) -> None:
        payload = {
            "id": "m",
            "top_provider": {"name": "x"},
            "providers": [
                {
                    "pricing": {
                        "prompt_per_million": "100.5",
                        "completion_per_million": "200",
                        "input_cache_read_per_million": "10",
                        "currency": "RUB",
                    },
                    "context_length": 1000,
                    "max_completion_tokens": 500,
                }
            ],
        }
        mock_httpx = _pricing_http(httpx.Response(200, json=payload))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=mock_httpx):
            pricing = await get_model_pricing()
        assert pricing == ModelPricing(100.5, 200.0, 10.0, 1000, 500)

    async def test_non_rub_currency_returns_none(self, caplog) -> None:
        payload = {
            "top_provider": {
                "pricing": {"prompt_per_million": "2.6", "completion_per_million": "6.6", "currency": "USD"}
            }
        }
        mock_httpx = _pricing_http(httpx.Response(200, json=payload))
        with caplog.at_level(logging.WARNING, logger="grok-critic"), patch(
            "grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=mock_httpx
        ):
            assert await get_model_pricing() is None
        assert "currency" in caplog.text

    async def test_http_500_returns_none(self, caplog) -> None:
        mock_httpx = _pricing_http(httpx.Response(500, text="boom"))
        with caplog.at_level(logging.WARNING, logger="grok-critic"), patch(
            "grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=mock_httpx
        ):
            assert await get_model_pricing() is None
        assert "secret-key-xyz" not in caplog.text

    async def test_network_error_returns_none(self, caplog) -> None:
        mock_httpx = _pricing_http(httpx.ConnectError("refused"))
        with caplog.at_level(logging.WARNING, logger="grok-critic"), patch(
            "grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=mock_httpx
        ):
            assert await get_model_pricing() is None
        assert "ConnectError" in caplog.text
        assert "secret-key-xyz" not in caplog.text

    async def test_broken_json_returns_none(self) -> None:
        mock_httpx = _pricing_http(httpx.Response(200, text="<html>not json"))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=mock_httpx):
            assert await get_model_pricing() is None

    async def test_malformed_price_returns_none(self) -> None:
        payload = {"top_provider": {"pricing": {"prompt_per_million": "abc", "completion_per_million": "1", "currency": "RUB"}}}
        mock_httpx = _pricing_http(httpx.Response(200, json=payload))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=mock_httpx):
            assert await get_model_pricing() is None

    async def test_cache_hit_skips_request(self) -> None:
        mock_httpx = _pricing_http(httpx.Response(200, json=MODEL_PAYLOAD))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=mock_httpx):
            first = await get_model_pricing()
            second = await get_model_pricing()
        assert first == second == GROK_PRICING
        assert mock_httpx.get.await_count == 1

    async def test_force_bypasses_cache(self) -> None:
        mock_httpx = _pricing_http(httpx.Response(200, json=MODEL_PAYLOAD))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=mock_httpx):
            await get_model_pricing()
            await get_model_pricing(force=True)
        assert mock_httpx.get.await_count == 2

    async def test_expired_ttl_refetches(self) -> None:
        mock_httpx = _pricing_http(httpx.Response(200, json=MODEL_PAYLOAD))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=mock_httpx):
            await get_model_pricing()
            assert api_mod._pricing_cache is not None
            ts, key, cached = api_mod._pricing_cache
            # Кэш «почти истёк» — ещё валиден, запроса нет.
            api_mod._pricing_cache = (ts - api_mod.PRICING_CACHE_TTL_SECONDS + 60, key, cached)
            await get_model_pricing()
            assert mock_httpx.get.await_count == 1
            # Кэш старше TTL — повторный запрос.
            api_mod._pricing_cache = (ts - api_mod.PRICING_CACHE_TTL_SECONDS - 1, key, cached)
            await get_model_pricing()
        assert mock_httpx.get.await_count == 2

    async def test_failure_cached_briefly(self) -> None:
        """Сбой кэшируется на PRICING_FAILURE_TTL_SECONDS: повтор без сети; force обходит; успех сбрасывает отметку."""
        failing = _pricing_http(httpx.Response(503, text="down"))
        ok = _pricing_http(httpx.Response(200, json=MODEL_PAYLOAD))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=failing):
            assert await get_model_pricing() is None
            assert await get_model_pricing() is None
        assert failing.get.await_count == 1
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=ok):
            assert await get_model_pricing() is None  # окно неудачи ещё действует
            assert ok.get.await_count == 0
            assert await get_model_pricing(force=True) == GROK_PRICING
        assert api_mod._pricing_failure is None

    async def test_failure_window_expires(self) -> None:
        failing = _pricing_http(httpx.Response(503, text="down"))
        ok = _pricing_http(httpx.Response(200, json=MODEL_PAYLOAD))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=failing):
            assert await get_model_pricing() is None
        assert api_mod._pricing_failure is not None
        ts, key = api_mod._pricing_failure
        api_mod._pricing_failure = (ts - api_mod.PRICING_FAILURE_TTL_SECONDS - 1, key)
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=ok):
            assert await get_model_pricing() == GROK_PRICING
        assert ok.get.await_count == 1

    async def test_network_exception_cached_briefly(self) -> None:
        failing = _pricing_http(httpx.ConnectError("boom"))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=failing):
            assert await get_model_pricing() is None
            assert await get_model_pricing() is None
        assert failing.get.await_count == 1

    async def test_failure_for_other_model_does_not_block(self, _cfg) -> None:
        failing = _pricing_http(httpx.Response(503, text="down"))
        ok = _pricing_http(httpx.Response(200, json=MODEL_PAYLOAD))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=failing):
            assert await get_model_pricing() is None
        _cfg.model = "x-ai/other-model"
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=ok):
            assert await get_model_pricing() == GROK_PRICING
        assert ok.get.await_count == 1

    async def test_model_change_invalidates_cache(self, _cfg) -> None:
        mock_httpx = _pricing_http(httpx.Response(200, json=MODEL_PAYLOAD))
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock, return_value=mock_httpx):
            await get_model_pricing()
            _cfg.model = "other/model"
            await get_model_pricing()
        assert mock_httpx.get.await_count == 2


# END_BLOCK_MODEL_PRICING


# START_BLOCK_CRITIQUE_RESULT
class TestCritiqueResult:
    def test_success_no_error(self) -> None:
        r = CritiqueResult(text="ok", model="m", agent_count=4, effort="low")
        assert r.success is True

    def test_failure_with_error(self) -> None:
        r = CritiqueResult(text="", model="m", agent_count=4, effort="low", error="bad")
        assert r.success is False

    def test_defaults(self) -> None:
        r = CritiqueResult(text="t", model="m", agent_count=4, effort="low")
        assert r.input_tokens == 0
        assert r.output_tokens == 0
        assert r.total_tokens == 0
        assert r.cost_rub is None
        assert r.cost_is_estimate is False
        assert not hasattr(r, "cost_usd")
        assert r.review_id == ""
        assert r.error == ""


# END_BLOCK_CRITIQUE_RESULT


# START_BLOCK_PERSISTENT_CLIENT
class TestGetClient:
    async def test_creates_client(self) -> None:
        api_mod._client = None
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.timeout_seconds = 30
            client = await get_client()
            assert isinstance(client, httpx.AsyncClient)
            await client.aclose()
            api_mod._client = None

    async def test_reuses_client(self) -> None:
        api_mod._client = None
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.timeout_seconds = 30
            c1 = await get_client()
            c2 = await get_client()
            assert c1 is c2
            await c1.aclose()
            api_mod._client = None


# END_BLOCK_PERSISTENT_CLIENT


# START_BLOCK_CLIENT_CALL
class TestResponsesClientCall:
    @pytest.fixture()
    def client(self) -> ResponsesClient:
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.api_key = SecretStr("test-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.timeout_seconds = 30
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.daily_budget_rub = 0.0
            mock_cfg.max_concurrent_requests = 2
            return ResponsesClient()

    async def test_successful_call(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(
            200,
            json={
                "id": "resp_123",
                "output_text": "review result here",
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
        )
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("review this code")
            assert result.success is True
            assert result.text == "review result here"
            assert result.input_tokens == 100
            assert result.output_tokens == 50
            assert result.total_tokens == 150

    async def test_401_error(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(401, text="Unauthorized")
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Ошибка авторизации" in result.error
            assert "Unauthorized" in result.error

    async def test_429_error(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(429, text="Too Many Requests")
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Превышен лимит запросов" in result.error

    async def test_500_error(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(500, text="Internal Server Error")
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Ошибка сервера" in result.error

    async def test_402_insufficient_funds(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(
            402,
            json={"error": {"code": "INSUFFICIENT_FUNDS", "message": "Недостаточно средств на балансе"}},
        )
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Недостаточно средств" in result.error
            assert "Недостаточно средств на балансе" in result.error

    async def test_502_provider_down(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(
            502,
            json={"error": {"code": "PROVIDER_ERROR", "message": "xAI provider unavailable"}},
        )
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Провайдер недоступен" in result.error
            assert "xAI provider unavailable" in result.error

    async def test_503_no_providers(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(503, text="Service Unavailable")
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Нет доступных провайдеров" in result.error

    async def test_error_body_parsed(self, client: ResponsesClient) -> None:
        """Polza.AI returns structured error: {"error": {"code": "...", "message": "..."}}"""
        mock_response = httpx.Response(
            429,
            json={"error": {"code": "RATE_LIMIT", "message": "Too many requests for grok-4.20-multi-agent"}},
        )
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Too many requests for grok-4.20-multi-agent" in result.error

    async def test_timeout(self, client: ResponsesClient) -> None:
        with (
            patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc,
            patch("grok_critic.api_client.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "таймаут" in result.error.lower()

    async def test_network_error_retried_then_success(self, client: ResponsesClient) -> None:
        """REL-01: ConnectError/ReadError ретраятся, запрос в итоге успешен."""
        mock_response = httpx.Response(200, json={"output_text": "ok"})
        with (
            patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc,
            patch("grok_critic.api_client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(side_effect=[
                httpx.ConnectError("connection refused"),
                httpx.ReadError("connection reset"),
                mock_response,
            ])
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert result.success
            assert result.text == "ok"
            assert mock_httpx.post.call_count == 3
            assert mock_sleep.call_count == 2

    async def test_network_error_exhausted(self, client: ResponsesClient) -> None:
        """REL-01: после исчерпания ретраев — понятная ошибка, а не падение."""
        with (
            patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc,
            patch("grok_critic.api_client.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Сетевая ошибка" in result.error
            assert "ConnectError" in result.error
            assert mock_httpx.post.call_count == MAX_RETRIES + 1

    async def test_remote_protocol_error_retried(self, client: ResponsesClient) -> None:
        """REL-01: RemoteProtocolError (мёртвое keep-alive соединение) ретраится."""
        mock_response = httpx.Response(200, json={"output_text": "ok"})
        with (
            patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc,
            patch("grok_critic.api_client.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(side_effect=[
                httpx.RemoteProtocolError("Server disconnected"),
                mock_response,
            ])
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert result.success
            assert mock_httpx.post.call_count == 2

    async def test_system_prompt_included(self, client: ResponsesClient) -> None:
        captured_body: dict = {}
        mock_response = httpx.Response(200, json={"output_text": "ok"})

        async def capture_post(url: str, **kwargs: object) -> httpx.Response:
            body = kwargs.get("json")
            if isinstance(body, dict):
                captured_body.update(body)
            return mock_response

        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = capture_post
            mock_gc.return_value = mock_httpx
            await client.call("prompt", system_prompt="be critical")
            assert captured_body["input"][0]["role"] == "system"
            assert captured_body["input"][0]["content"] == "be critical"

    async def test_messages_override(self, client: ResponsesClient) -> None:
        """FEAT-FOLLOWUP-ID: полный диалог можно передать через messages."""
        captured_body: dict = {}
        mock_response = httpx.Response(200, json={"output_text": "ok"})

        async def capture_post(url: str, **kwargs: object) -> httpx.Response:
            body = kwargs.get("json")
            if isinstance(body, dict):
                captured_body.update(body)
            return mock_response

        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = capture_post
            mock_gc.return_value = mock_httpx
            messages = [
                {"role": "system", "content": "dialog system"},
                {"role": "user", "content": "original"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "question"},
            ]
            await client.call("question", messages=messages)
            assert captured_body["input"] == messages

    async def test_json_decode_error(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(
            200,
            text="not json at all",
            headers={"content-type": "text/plain"},
        )
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Некорректный JSON" in result.error

    async def test_review_id_generated(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(200, json={"output_text": "ok"})
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert result.review_id.startswith("rev_")
            assert len(result.review_id) == 16

    async def _call_with_usage(self, client: ResponsesClient, usage: dict[str, object]):
        mock_response = httpx.Response(200, json={"output_text": "ok", "usage": usage})
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            return await client.call("test")

    async def test_cost_rub_from_api_is_not_estimate(self, client: ResponsesClient, monkeypatch) -> None:
        """PRICING-RUB: фактическая cost_rub из API приоритетна, тариф даже не запрашивается."""
        pricing_mock = AsyncMock(return_value=GROK_PRICING)
        monkeypatch.setattr(api_mod, "get_model_pricing", pricing_mock)
        result = await self._call_with_usage(
            client,
            {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500, "cost_rub": 53.67},
        )
        assert result.cost_rub == 53.67
        assert result.cost_is_estimate is False
        pricing_mock.assert_not_awaited()

    async def test_cost_estimated_by_tariff_when_api_has_no_cost(self, client: ResponsesClient, monkeypatch) -> None:
        monkeypatch.setattr(api_mod, "get_model_pricing", AsyncMock(return_value=GROK_PRICING))
        result = await self._call_with_usage(
            client,
            {
                "input_tokens": 619_370,
                "output_tokens": 72_853,
                "total_tokens": 692_223,
                "input_tokens_details": {"cached_tokens": 477_162},
            },
        )
        assert result.cost_is_estimate is True
        assert result.cost_rub is not None
        assert round(result.cost_rub, 2) == 53.67

    async def test_cost_none_without_api_cost_and_tariff(self, client: ResponsesClient) -> None:
        # autouse-фикстура: get_model_pricing → None (тариф недоступен)
        result = await self._call_with_usage(
            client, {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}
        )
        assert result.success
        assert result.cost_rub is None
        assert result.cost_is_estimate is False


# END_BLOCK_CLIENT_CALL


# START_BLOCK_RESOLVE_TIMEOUT
class TestResolveTimeout:
    """TEST-01: три ветки динамического таймаута."""

    def test_le_4_agents_uses_timeout_low(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "timeout_seconds", 180)
        assert _resolve_timeout(4) == 90
        assert _resolve_timeout(1) == 90

    def test_le_8_agents_uses_timeout_mid(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "timeout_seconds", 180)
        assert _resolve_timeout(8) == 150
        assert _resolve_timeout(5) == 150

    def test_over_8_agents_uses_base(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "timeout_seconds", 180)
        assert _resolve_timeout(16) == 180
        assert _resolve_timeout(64) == 180

    def test_small_base_caps_all(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "timeout_seconds", 30)
        assert _resolve_timeout(4) == 30
        assert _resolve_timeout(8) == 30
        assert _resolve_timeout(16) == 30

    def test_env_override_thresholds(self, monkeypatch) -> None:
        """QUAL-01: пороги конфигурируются через config."""
        monkeypatch.setattr(config, "timeout_seconds", 500)
        monkeypatch.setattr(config, "timeout_low", 45)
        monkeypatch.setattr(config, "timeout_mid", 120)
        assert _resolve_timeout(4) == 45
        assert _resolve_timeout(8) == 120
        assert _resolve_timeout(16) == 500


# END_BLOCK_RESOLVE_TIMEOUT


# START_BLOCK_CACHE_KEY
class TestPromptCacheKey:
    """BUG-03: prompt_cache_key детерминирован (sha256), не зависит от процесса."""

    @pytest.fixture()
    def client(self) -> ResponsesClient:
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.api_key = SecretStr("test-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.timeout_seconds = 30
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.daily_budget_rub = 0.0
            mock_cfg.max_concurrent_requests = 2
            return ResponsesClient()

    async def test_same_prompt_same_key(self, client: ResponsesClient) -> None:
        import hashlib

        captured: list[dict] = []
        mock_response = httpx.Response(200, json={"output_text": "ok"})

        async def capture_post(url: str, **kwargs: object) -> httpx.Response:
            body = kwargs.get("json")
            if isinstance(body, dict):
                captured.append(body)
            return mock_response

        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = capture_post
            mock_gc.return_value = mock_httpx
            await client.call("p1", system_prompt="be critical")
            await client.call("p2", system_prompt="be critical")

        assert len(captured) == 2
        expected = f"gc-{hashlib.sha256(b'be critical').hexdigest()[:8]}"
        assert captured[0]["prompt_cache_key"] == expected
        assert captured[1]["prompt_cache_key"] == expected

    async def test_no_system_prompt_no_key(self, client: ResponsesClient) -> None:
        captured: dict = {}
        mock_response = httpx.Response(200, json={"output_text": "ok"})

        async def capture_post(url: str, **kwargs: object) -> httpx.Response:
            body = kwargs.get("json")
            if isinstance(body, dict):
                captured.update(body)
            return mock_response

        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = capture_post
            mock_gc.return_value = mock_httpx
            await client.call("prompt")

        assert "prompt_cache_key" not in captured

    async def test_key_from_messages_override(self, client: ResponsesClient) -> None:
        """При messages-override system берётся из первого system-сообщения."""
        import hashlib

        captured: list[dict] = []
        mock_response = httpx.Response(200, json={"output_text": "ok"})

        async def capture_post(url: str, **kwargs: object) -> httpx.Response:
            body = kwargs.get("json")
            if isinstance(body, dict):
                captured.append(body)
            return mock_response

        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = capture_post
            mock_gc.return_value = mock_httpx
            messages = [
                {"role": "system", "content": "dialog system"},
                {"role": "user", "content": "q"},
            ]
            await client.call("q", messages=messages)

        expected = f"gc-{hashlib.sha256(b'dialog system').hexdigest()[:8]}"
        assert captured[0]["prompt_cache_key"] == expected


# END_BLOCK_CACHE_KEY


# START_BLOCK_CLIENT_FEATURES
class TestClientFeatures:
    @pytest.fixture()
    def client(self) -> ResponsesClient:
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.api_key = SecretStr("test-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.timeout_seconds = 30
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.daily_budget_rub = 0.0
            mock_cfg.max_concurrent_requests = 2
            return ResponsesClient()

    async def test_client_follows_redirects(self) -> None:
        """REL-04: persistent client создаётся с follow_redirects=True."""
        c = await get_client()
        try:
            assert c.follow_redirects is True
        finally:
            await close_client()

    async def test_empty_response_is_error(self, client: ResponsesClient) -> None:
        """REL-05: 200 OK с payload без текста — явная ошибка, не молчаливый пустой успех."""
        mock_response = httpx.Response(200, json={"unexpected": "format"})
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Пустой ответ" in result.error

    async def test_generic_4xx(self, client: ResponsesClient) -> None:
        """TEST-10: прочие 4xx — ошибка клиента с сообщением провайдера."""
        mock_response = httpx.Response(
            400, json={"error": {"code": "BAD_REQUEST", "message": "model not found"}}
        )
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Ошибка клиента" in result.error
            assert "model not found" in result.error


# END_BLOCK_CLIENT_FEATURES


# START_BLOCK_RETRY_DEADLINE
class TestRetryDeadline:
    """REL-06: общий дедлайн retry-цикла не должен превышать таймаут клиента."""

    async def test_deadline_stops_timeout_retries(self, monkeypatch) -> None:
        """После исчерпания дедлайна таймауты НЕ ретраятся (клиент бы всё равно отвалился)."""
        monkeypatch.setattr(config, "retry_deadline_seconds", 0.05)
        client = ResponsesClient()
        with (
            patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc,
            patch("grok_critic.api_client.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert mock_httpx.post.call_count == 1
            assert "таймаут" in result.error.lower()

    async def test_deadline_auto_allows_fast_error_retries(self, monkeypatch) -> None:
        """retry_deadline_seconds=0 → дедлайн = per-attempt timeout;
        быстрые сетевые ошибки успевают ретраиться."""
        monkeypatch.setattr(config, "retry_deadline_seconds", 0.0)
        client = ResponsesClient()
        mock_response = httpx.Response(200, json={"output_text": "ok"})
        with (
            patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc,
            patch("grok_critic.api_client.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(side_effect=[httpx.ConnectError("x"), mock_response])
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert result.success
            assert mock_httpx.post.call_count == 2

    async def test_deadline_exhausted_error_message(self, monkeypatch) -> None:
        """Явное сообщение об исчерпании дедлайна, если последний error — не таймаут."""
        monkeypatch.setattr(config, "retry_deadline_seconds", 0.05)
        monkeypatch.setattr(config, "max_retries", 3)
        client = ResponsesClient()
        with (
            patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc,
            patch("grok_critic.api_client.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert mock_httpx.post.call_count < 4


# END_BLOCK_RETRY_DEADLINE


# START_BLOCK_INFLIGHT_DEDUP
class TestInFlightDedup:
    """Параллельные вызовы с тем же контентом присоединяются к летящему запросу."""

    @pytest.fixture()
    def client(self) -> ResponsesClient:
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.api_key = SecretStr("test-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.timeout_seconds = 30
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.daily_budget_rub = 0.0
            mock_cfg.max_concurrent_requests = 4
            return ResponsesClient()

    async def test_parallel_same_content_single_api_call(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(200, json={"output_text": "ok"})
        post_calls = 0
        release = asyncio.Event()

        async def slow_post(url: str, **kwargs: object) -> httpx.Response:
            nonlocal post_calls
            post_calls += 1
            await release.wait()
            return mock_response

        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = slow_post
            mock_gc.return_value = mock_httpx
            t1 = asyncio.create_task(client.call("same", system_prompt="s"))
            await asyncio.sleep(0.02)
            t2 = asyncio.create_task(client.call("same", system_prompt="s"))
            await asyncio.sleep(0.02)
            release.set()
            r1, r2 = await asyncio.gather(t1, t2)

        assert post_calls == 1
        assert r1.text == "ok"
        assert r2.text == "ok"

    async def test_different_content_not_deduped(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(200, json={"output_text": "ok"})
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            r1, r2 = await asyncio.gather(
                client.call("first"),
                client.call("second"),
            )
        assert r1.success and r2.success
        assert mock_httpx.post.call_count == 2

    async def test_sequential_same_content_not_deduped(self, client: ResponsesClient) -> None:
        """Dedup только in-flight: завершённый запрос не кэшируется."""
        mock_response = httpx.Response(200, json={"output_text": "ok"})
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            await client.call("same")
            await client.call("same")
        assert mock_httpx.post.call_count == 2

    async def test_same_prompt_different_messages_not_deduped(
        self, client: ResponsesClient
    ) -> None:
        """A3: followup по разным review_id с одинаковым prompt, но разной историей
        диалога (messages) НЕ должен присоединяться к чужому in-flight запросу."""
        response_a = httpx.Response(200, json={"output_text": "answer-A"})
        response_b = httpx.Response(200, json={"output_text": "answer-B"})
        post_calls: list[dict[str, object]] = []
        release = asyncio.Event()

        async def slow_post(url: str, **kwargs: object) -> httpx.Response:
            body = kwargs.get("json")
            post_calls.append(body)  # type: ignore[arg-type]
            await release.wait()
            # Разные тела запроса → разные ответы, чтобы поймать возможную склейку.
            assert isinstance(body, dict)
            first_msg = body["input"][0]["content"]
            return response_a if "review-A" in first_msg else response_b

        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = slow_post
            mock_gc.return_value = mock_httpx
            t1 = asyncio.create_task(
                client.call(
                    "Ты уверен?",
                    messages=[{"role": "user", "content": "review-A context"}],
                )
            )
            await asyncio.sleep(0.02)
            t2 = asyncio.create_task(
                client.call(
                    "Ты уверен?",
                    messages=[{"role": "user", "content": "review-B context"}],
                )
            )
            await asyncio.sleep(0.02)
            release.set()
            r1, r2 = await asyncio.gather(t1, t2)

        assert len(post_calls) == 2
        assert r1.text == "answer-A"
        assert r2.text == "answer-B"

    async def test_different_model_not_deduped(self) -> None:
        """A3: разные модели у клиентов → разные dedup-ключи, отдельные запросы."""
        mock_response = httpx.Response(200, json={"output_text": "ok"})
        release = asyncio.Event()
        post_calls = 0

        async def slow_post(url: str, **kwargs: object) -> httpx.Response:
            nonlocal post_calls
            post_calls += 1
            await release.wait()
            return mock_response

        def make_client(model: str) -> ResponsesClient:
            with patch("grok_critic.api_client.config") as mock_cfg:
                mock_cfg.base_url = "https://polza.ai/api/v1"
                mock_cfg.api_key = SecretStr("test-key")
                mock_cfg.model = model
                mock_cfg.timeout_seconds = 30
                mock_cfg.timeout_low = 90
                mock_cfg.timeout_mid = 150
                mock_cfg.max_retries = 2
                mock_cfg.retry_backoff_base = 2.0
                mock_cfg.retry_deadline_seconds = 0.0
                mock_cfg.daily_budget_rub = 0.0
                mock_cfg.max_concurrent_requests = 4
                return ResponsesClient()

        client_a = make_client("model-a")
        client_b = make_client("model-b")

        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = slow_post
            mock_gc.return_value = mock_httpx
            t1 = asyncio.create_task(client_a.call("same", system_prompt="s"))
            await asyncio.sleep(0.02)
            t2 = asyncio.create_task(client_b.call("same", system_prompt="s"))
            await asyncio.sleep(0.02)
            release.set()
            r1, r2 = await asyncio.gather(t1, t2)

        assert post_calls == 2
        assert r1.success and r2.success


# END_BLOCK_INFLIGHT_DEDUP


# START_BLOCK_INFLIGHT_CONCURRENCY
def _patch_runtime_config(**overrides: object):
    """patch config на ВСЁ время теста (call() читает config в рантайме, не только в __init__)."""
    patcher = patch("grok_critic.api_client.config")
    mock_cfg = patcher.start()
    values: dict[str, object] = {
        "base_url": "https://polza.ai/api/v1",
        "api_key": SecretStr("test-key"),
        "model": "x-ai/grok-4.20-multi-agent",
        "timeout_seconds": 30,
        "timeout_low": 90,
        "timeout_mid": 150,
        "max_retries": 2,
        "retry_backoff_base": 2.0,
        "retry_deadline_seconds": 0.0,
        "daily_budget_rub": 0.0,
        "max_concurrent_requests": 2,
    }
    values.update(overrides)
    for name, value in values.items():
        setattr(mock_cfg, name, value)
    return patcher


def _body_prompt(kwargs: dict[str, object]) -> str:
    body = kwargs.get("json")
    assert isinstance(body, dict)
    return str(body["input"][-1]["content"])


class TestInFlightConcurrency:
    """DEDUP-CANCEL / DEDUP-SEM / BUDGET-SOFT: инварианты общей in-flight задачи."""

    async def test_creator_cancel_does_not_duplicate_request(self) -> None:
        """I2: отмена создателя не удаляет запись — повторный вызов присоединяется."""
        patcher = _patch_runtime_config()
        try:
            client = ResponsesClient()
            started = asyncio.Event()
            release = asyncio.Event()
            post_calls = 0

            async def slow_post(url: str, **kwargs: object) -> httpx.Response:
                nonlocal post_calls
                post_calls += 1
                started.set()
                await release.wait()
                return httpx.Response(200, json={"output_text": "ok"})

            with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
                mock_httpx = AsyncMock()
                mock_httpx.post = slow_post
                mock_gc.return_value = mock_httpx

                creator = asyncio.create_task(client.call("same"))
                await asyncio.wait_for(started.wait(), 1)
                creator.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await creator

                key = client._dedup_key("same", 4, None, None)
                assert key in api_mod._inflight

                second = asyncio.create_task(client.call("same"))
                await asyncio.sleep(0.01)
                release.set()
                result = await asyncio.wait_for(second, 1)
                await asyncio.sleep(0)

            assert post_calls == 1
            assert result.text == "ok"
            assert key not in api_mod._inflight
        finally:
            patcher.stop()

    async def test_all_waiters_cancelled_request_completes(self) -> None:
        """I2/I3/I4/I6: все ожидающие отменены → запрос завершается, запись удалена,
        статистика учтена один раз, слот semaphore освобождён."""
        patcher = _patch_runtime_config(max_concurrent_requests=1)
        try:
            client = ResponsesClient()
            started = asyncio.Event()
            release = asyncio.Event()
            prompts: list[str] = []

            async def post(url: str, **kwargs: object) -> httpx.Response:
                prompt = _body_prompt(kwargs)
                prompts.append(prompt)
                if prompt == "same":
                    started.set()
                    await release.wait()
                return httpx.Response(200, json={"output_text": f"ok-{prompt}"})

            with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
                mock_httpx = AsyncMock()
                mock_httpx.post = post
                mock_gc.return_value = mock_httpx

                waiters = [asyncio.create_task(client.call("same")) for _ in range(3)]
                await asyncio.wait_for(started.wait(), 1)
                key = client._dedup_key("same", 4, None, None)
                shared = api_mod._inflight[key]
                for w in waiters:
                    w.cancel()
                results = await asyncio.gather(*waiters, return_exceptions=True)
                assert all(isinstance(r, asyncio.CancelledError) for r in results)
                assert not shared.done()
                assert api_mod._inflight.get(key) is shared

                # I4: слот всё ещё занят летящим запросом — другой ключ не стартует.
                other = asyncio.create_task(client.call("other"))
                await asyncio.sleep(0.02)
                assert prompts == ["same"]

                release.set()
                shared_result = await asyncio.wait_for(shared, 1)
                other_result = await asyncio.wait_for(other, 1)
                await asyncio.sleep(0)

            assert shared_result.text == "ok-same"
            assert other_result.text == "ok-other"
            assert prompts == ["same", "other"]
            assert key not in api_mod._inflight
            assert get_usage_stats()["calls"] == 2  # по одному на каждый реальный запрос
        finally:
            patcher.stop()

    async def test_joiners_do_not_hold_semaphore_slot(self) -> None:
        """I4: владелец + 3 joiner при limit=2 → запрос другого ключа стартует до
        завершения владельца."""
        patcher = _patch_runtime_config(max_concurrent_requests=2)
        try:
            client = ResponsesClient()
            started = asyncio.Event()
            release = asyncio.Event()
            prompts: list[str] = []

            async def post(url: str, **kwargs: object) -> httpx.Response:
                prompt = _body_prompt(kwargs)
                prompts.append(prompt)
                if prompt == "slow":
                    started.set()
                    await release.wait()
                return httpx.Response(200, json={"output_text": f"ok-{prompt}"})

            with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
                mock_httpx = AsyncMock()
                mock_httpx.post = post
                mock_gc.return_value = mock_httpx

                owner = asyncio.create_task(client.call("slow"))
                await asyncio.wait_for(started.wait(), 1)
                joiners = [asyncio.create_task(client.call("slow")) for _ in range(3)]
                await asyncio.sleep(0.01)

                other_result = await asyncio.wait_for(client.call("other"), 1)
                assert not owner.done()
                assert prompts == ["slow", "other"]

                release.set()
                results = await asyncio.wait_for(asyncio.gather(owner, *joiners), 1)

            assert other_result.text == "ok-other"
            assert all(r.text == "ok-slow" for r in results)
            assert prompts.count("slow") == 1
        finally:
            patcher.stop()

    async def test_joiner_queued_for_slot_does_not_hold_it(self) -> None:
        """I4 (DEDUP-SEM): владелец и joiner одного ключа пришли, когда все слоты заняты.
        После освобождения слотов joiner не должен занять второй слот своим ожиданием —
        запрос другого ключа стартует, пока общий запрос ещё летит."""
        patcher = _patch_runtime_config(max_concurrent_requests=2)
        try:
            client = ResponsesClient()
            gates = {name: asyncio.Event() for name in ("a", "b", "slow")}
            started = {name: asyncio.Event() for name in ("a", "b", "slow", "other")}
            prompts: list[str] = []

            async def post(url: str, **kwargs: object) -> httpx.Response:
                prompt = _body_prompt(kwargs)
                prompts.append(prompt)
                started[prompt].set()
                if prompt in gates:
                    await gates[prompt].wait()
                return httpx.Response(200, json={"output_text": f"ok-{prompt}"})

            with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
                mock_httpx = AsyncMock()
                mock_httpx.post = post
                mock_gc.return_value = mock_httpx

                blocker_a = asyncio.create_task(client.call("a"))
                blocker_b = asyncio.create_task(client.call("b"))
                await asyncio.wait_for(started["a"].wait(), 1)
                await asyncio.wait_for(started["b"].wait(), 1)

                owner = asyncio.create_task(client.call("slow"))
                joiner = asyncio.create_task(client.call("slow"))
                await asyncio.sleep(0.01)

                gates["a"].set()
                await asyncio.wait_for(blocker_a, 1)
                await asyncio.wait_for(started["slow"].wait(), 1)
                gates["b"].set()
                await asyncio.wait_for(blocker_b, 1)

                other_result = await asyncio.wait_for(client.call("other"), 1)
                assert not owner.done() and not joiner.done()

                gates["slow"].set()
                owner_result, joiner_result = await asyncio.wait_for(
                    asyncio.gather(owner, joiner), 1
                )

            assert other_result.text == "ok-other"
            assert owner_result.text == joiner_result.text == "ok-slow"
            assert prompts.count("slow") == 1
        finally:
            patcher.stop()

    async def test_joiner_not_rejected_by_budget_new_request_rejected(self) -> None:
        """I5: бюджет исчерпан во время полёта → joiner получает результат,
        новый запрос (другой ключ) отклоняется без post."""
        patcher = _patch_runtime_config(daily_budget_rub=10.0)
        try:
            client = ResponsesClient()
            started = asyncio.Event()
            release = asyncio.Event()
            prompts: list[str] = []

            async def post(url: str, **kwargs: object) -> httpx.Response:
                prompts.append(_body_prompt(kwargs))
                started.set()
                await release.wait()
                return httpx.Response(200, json={"output_text": "ok"})

            with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
                mock_httpx = AsyncMock()
                mock_httpx.post = post
                mock_gc.return_value = mock_httpx

                owner = asyncio.create_task(client.call("same"))
                await asyncio.wait_for(started.wait(), 1)
                # Имитация: бюджет исчерпан, пока первый запрос летит.
                api_mod._usage_stats["cost_rub"] = 50.0

                joiner = asyncio.create_task(client.call("same"))
                rejected = await asyncio.wait_for(client.call("fresh"), 1)
                assert not rejected.success
                assert "Превышен дневной бюджет: 50,00 ₽ из 10,00 ₽" in rejected.error
                assert "POLZA_DAILY_BUDGET_RUB" in rejected.error
                assert prompts == ["same"]

                release.set()
                owner_result, joiner_result = await asyncio.wait_for(
                    asyncio.gather(owner, joiner), 1
                )

            assert owner_result.text == "ok"
            assert joiner_result.text == "ok"
            assert joiner_result.success
            assert prompts == ["same"]
        finally:
            patcher.stop()

    async def test_exception_reaches_all_waiters_and_entry_removed(self) -> None:
        """I6: исключение общей задачи получают все ожидающие; запись удалена."""
        patcher = _patch_runtime_config()
        try:
            client = ResponsesClient()
            started = asyncio.Event()
            release = asyncio.Event()
            post_calls = 0

            async def failing_post(url: str, **kwargs: object) -> httpx.Response:
                nonlocal post_calls
                post_calls += 1
                started.set()
                await release.wait()
                raise RuntimeError("boom")

            with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
                mock_httpx = AsyncMock()
                mock_httpx.post = failing_post
                mock_gc.return_value = mock_httpx

                owner = asyncio.create_task(client.call("same"))
                await asyncio.wait_for(started.wait(), 1)
                joiners = [asyncio.create_task(client.call("same")) for _ in range(2)]
                await asyncio.sleep(0.01)
                release.set()
                results = await asyncio.wait_for(
                    asyncio.gather(owner, *joiners, return_exceptions=True), 1
                )
                await asyncio.sleep(0)

            assert post_calls == 1
            assert len(results) == 3
            assert all(isinstance(r, RuntimeError) for r in results)
            assert api_mod._inflight == {}
            stats = get_usage_stats()
            assert stats["calls"] == 0
            assert stats["errors"] == 0
        finally:
            patcher.stop()

    async def test_done_callback_does_not_remove_newer_task(self) -> None:
        """I3: done-callback устаревшей задачи не удаляет более новую запись с тем же ключом."""

        async def value() -> CritiqueResult:
            return CritiqueResult(text="x", model="m", agent_count=4, effort="low")

        release = asyncio.Event()

        async def pending() -> CritiqueResult:
            await release.wait()
            return CritiqueResult(text="y", model="m", agent_count=4, effort="low")

        old = asyncio.create_task(value())
        await old
        new = asyncio.create_task(pending())
        api_mod._inflight["k"] = new
        api_mod._inflight_discard("k", old)
        assert api_mod._inflight.get("k") is new
        release.set()
        await new
        api_mod._inflight_discard("k", new)
        assert "k" not in api_mod._inflight


# END_BLOCK_INFLIGHT_CONCURRENCY


# START_BLOCK_BUDGET_GUARD
class TestBudgetGuard:
    """FEAT-BUDGET: превышение дневного лимита — отказ ДО платного вызова."""

    async def test_budget_exceeded_no_api_call(self) -> None:
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.api_key = SecretStr("test-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.timeout_seconds = 30
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.max_concurrent_requests = 2
            mock_cfg.daily_budget_rub = 500.0

            api_mod._usage_stats["date"] = datetime.date.today().isoformat()
            api_mod._usage_stats["cost_rub"] = 1234.567  # уже потрачено больше лимита

            client = ResponsesClient()
            with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
                mock_httpx = AsyncMock()
                mock_gc.return_value = mock_httpx
                result = await client.call("test")
                assert not result.success
                assert result.error == (
                    "Превышен дневной бюджет: 1 234,57 ₽ из 500,00 ₽. "
                    "Увеличьте POLZA_DAILY_BUDGET_RUB или дождитесь следующего дня."
                )
                assert "$" not in result.error
                mock_httpx.post.assert_not_called()

    async def test_budget_below_limit_allows_call(self) -> None:
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.api_key = SecretStr("test-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.timeout_seconds = 30
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.max_concurrent_requests = 2
            mock_cfg.daily_budget_rub = 500.0

            api_mod._usage_stats["date"] = datetime.date.today().isoformat()
            api_mod._usage_stats["cost_rub"] = 499.99

            client = ResponsesClient()
            mock_response = httpx.Response(200, json={"output_text": "ok"})
            with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
                mock_httpx = AsyncMock()
                mock_httpx.post = AsyncMock(return_value=mock_response)
                mock_gc.return_value = mock_httpx
                result = await client.call("test")
                assert result.success
                mock_httpx.post.assert_awaited_once()

    async def test_budget_disabled_by_default(self) -> None:
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.api_key = SecretStr("test-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.timeout_seconds = 30
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.max_concurrent_requests = 2
            mock_cfg.daily_budget_rub = 0.0  # выключен

            api_mod._usage_stats["date"] = datetime.date.today().isoformat()
            api_mod._usage_stats["cost_rub"] = 100_000.0  # много потрачено, но лимита нет

            client = ResponsesClient()
            mock_response = httpx.Response(200, json={"output_text": "ok"})
            with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
                mock_httpx = AsyncMock()
                mock_httpx.post = AsyncMock(return_value=mock_response)
                mock_gc.return_value = mock_httpx
                result = await client.call("test")
                assert result.success


# END_BLOCK_BUDGET_GUARD


# START_BLOCK_USAGE_STATS
class TestUsageStats:
    """FEAT-BUDGET: суточная статистика вызовов и стоимости."""

    @pytest.fixture()
    def client(self) -> ResponsesClient:
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.api_key = SecretStr("test-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.timeout_seconds = 30
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.daily_budget_rub = 0.0
            mock_cfg.max_concurrent_requests = 2
            return ResponsesClient()

    async def test_success_recorded(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(
            200,
            json={
                "output_text": "ok",
                "usage": {
                    "input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500, "cost_rub": 1.25,
                },
            },
        )
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            await client.call("test")

        stats = get_usage_stats()
        assert stats["calls"] == 1
        assert stats["errors"] == 0
        assert stats["cost_rub"] == pytest.approx(1.25)
        assert "cost_usd" not in stats

    async def test_error_recorded(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(500, text="boom")
        with (
            patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc,
            patch("grok_critic.api_client.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            await client.call("test")

        stats = get_usage_stats()
        assert stats["calls"] == 0
        assert stats["errors"] == 1

    async def test_estimated_cost_recorded(self, client: ResponsesClient, monkeypatch) -> None:
        """PRICING-RUB: оценка по тарифу тоже учитывается в суточной cost_rub."""
        monkeypatch.setattr(api_mod, "get_model_pricing", AsyncMock(return_value=GROK_PRICING))
        mock_response = httpx.Response(
            200,
            json={"output_text": "ok", "usage": {"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1_000_000}},
        )
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            await client.call("test")
        assert get_usage_stats()["cost_rub"] == pytest.approx(147.35)

    def test_date_in_dd_mm_yyyy(self) -> None:
        stats = get_usage_stats()
        assert stats["date"] == datetime.date.today().strftime("%d.%m.%Y")
        assert set(stats) == {"date", "calls", "errors", "cost_rub"}

    def test_rollover_on_date_change(self) -> None:
        api_mod._usage_stats.update({"date": "2000-01-01", "calls": 7, "errors": 3, "cost_rub": 99.0})
        stats = get_usage_stats()
        assert stats["calls"] == 0
        assert stats["errors"] == 0
        assert stats["cost_rub"] == 0.0
        assert stats["date"] == datetime.date.today().strftime("%d.%m.%Y")
        # внутренний ключ сброса — ISO, наружу не утекает
        assert api_mod._usage_stats["date"] == datetime.date.today().isoformat()

    def test_same_day_keeps_counters(self) -> None:
        api_mod._usage_stats.update({"calls": 2, "cost_rub": 10.5})
        stats = get_usage_stats()
        assert stats["calls"] == 2
        assert stats["cost_rub"] == 10.5


# END_BLOCK_USAGE_STATS


# START_BLOCK_FORMAT_RUB
class TestFormatRub:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0,00 ₽"),
            (53.67, "53,67 ₽"),
            (23.576, "23,58 ₽"),
            (128760.16, "128 760,16 ₽"),
            (1234567.891, "1 234 567,89 ₽"),
        ],
    )
    def test_format(self, value: float, expected: str) -> None:
        assert api_mod.format_rub(value) == expected


# END_BLOCK_FORMAT_RUB
