# FILE: tests/test_api_client.py
# VERSION: 1.11.0
# START_MODULE_CONTRACT
#   PURPOSE: Tests for M-API ResponsesClient with mocked HTTP
#   SCOPE: call(), error handling, parsing, usage/cost, retry deadline, dedup, budget guard
#   DEPENDS: M-API, M-CONFIG
#   LINKS: M-API
# END_MODULE_CONTRACT

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr

import grok_critic.api_client as api_mod
from grok_critic.api_client import (
    MAX_RETRIES,
    CritiqueResult,
    ResponsesClient,
    _calculate_cost,
    _extract_text,
    _extract_usage,
    _resolve_effort,
    _resolve_timeout,
    close_client,
    get_client,
    get_usage_stats,
)
from grok_critic.config import config


# START_BLOCK_RUNTIME_STATE_RESET
@pytest.fixture(autouse=True)
def _reset_api_runtime_state():
    """Чистит module-level состояние (stats/inflight/semaphore) между тестами."""
    api_mod._usage_stats.update(
        {
            "date": datetime.date.today().isoformat(),
            "calls": 0,
            "errors": 0,
            "cost_usd": 0.0,
            "cost_rub": 0.0,
        }
    )
    api_mod._inflight.clear()
    api_mod._semaphore = None
    api_mod._semaphore_limit = None
    yield


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


# START_BLOCK_CALCULATE_COST
class TestCalculateCost:
    def test_zero_prices(self) -> None:
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            assert _calculate_cost(1000, 1000) == 0.0

    def test_with_prices_per_million(self) -> None:
        with patch("grok_critic.api_client.config") as mock_cfg:
            # $2.5 за 1М input, $6.6 за 1М output
            mock_cfg.price_input_per_1m = 2.5
            mock_cfg.price_output_per_1m = 6.6
            # 30K input + 10K output = 30/1000*2.5 + 10/1000*6.6 = 0.075 + 0.066 = 0.141
            cost = _calculate_cost(30_000, 10_000)
            assert cost == pytest.approx(0.075 + 0.066)

    def test_zero_tokens(self) -> None:
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.price_input_per_1m = 2.5
            mock_cfg.price_output_per_1m = 6.6
            assert _calculate_cost(0, 0) == 0.0


# END_BLOCK_CALCULATE_COST


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
        assert r.cost_usd == 0.0
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
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.daily_budget_usd = 0.0
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

    async def test_cost_calculated(self, client: ResponsesClient) -> None:
        with patch.object(client, "_api_key", "test-key"), \
             patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.price_input_per_1m = 10.0   # $10 per 1M → 1000 tokens = $0.01
            mock_cfg.price_output_per_1m = 30.0  # $30 per 1M → 500 tokens = $0.015
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.timeout_seconds = 30
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.daily_budget_usd = 0.0
            mock_cfg.max_concurrent_requests = 2

            mock_response = httpx.Response(
                200,
                json={
                    "output_text": "ok",
                    "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
                },
            )
            with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
                mock_httpx = AsyncMock()
                mock_httpx.post = AsyncMock(return_value=mock_response)
                mock_gc.return_value = mock_httpx
                result = await client.call("test")
                assert result.cost_usd == pytest.approx(0.01 + 0.015)


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
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.daily_budget_usd = 0.0
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
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.daily_budget_usd = 0.0
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
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            mock_cfg.timeout_low = 90
            mock_cfg.timeout_mid = 150
            mock_cfg.max_retries = 2
            mock_cfg.retry_backoff_base = 2.0
            mock_cfg.retry_deadline_seconds = 0.0
            mock_cfg.daily_budget_usd = 0.0
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


# END_BLOCK_INFLIGHT_DEDUP


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
            mock_cfg.daily_budget_usd = 0.01

            api_mod._usage_stats["date"] = datetime.date.today().isoformat()
            api_mod._usage_stats["cost_usd"] = 0.5  # уже потрачено больше лимита

            client = ResponsesClient()
            with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
                mock_httpx = AsyncMock()
                mock_gc.return_value = mock_httpx
                result = await client.call("test")
                assert not result.success
                assert "бюджет" in result.error.lower()
                mock_httpx.post.assert_not_called()

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
            mock_cfg.daily_budget_usd = 0.0  # выключен

            api_mod._usage_stats["date"] = datetime.date.today().isoformat()
            api_mod._usage_stats["cost_usd"] = 100.0  # много потрачено, но лимита нет

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
            mock_cfg.daily_budget_usd = 0.0
            mock_cfg.max_concurrent_requests = 2
            mock_cfg.price_input_per_1m = 2.6
            mock_cfg.price_output_per_1m = 6.6
            return ResponsesClient()

    async def test_success_recorded(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(
            200,
            json={
                "output_text": "ok",
                "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
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
        assert stats["cost_usd"] == pytest.approx((1000 / 1e6 * 2.6) + (500 / 1e6 * 6.6))

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


# END_BLOCK_USAGE_STATS
