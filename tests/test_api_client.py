# FILE: tests/test_api_client.py
# VERSION: 1.1.0
# START_MODULE_CONTRACT
#   PURPOSE: Tests for M-API ResponsesClient with mocked HTTP
#   SCOPE: Test call(), error handling, response parsing, CritiqueResult, usage, cost
#   DEPENDS: M-API, M-CONFIG
#   LINKS: M-API
# END_MODULE_CONTRACT

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from grok_critic.api_client import (
    CritiqueResult,
    ResponsesClient,
    _calculate_cost,
    _extract_text,
    _extract_usage,
    _resolve_effort,
    get_client,
)


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
        inp, out, total, cost_rub, cached = _extract_usage(payload)
        assert inp == 100
        assert out == 50
        assert total == 150
        assert cost_rub is None
        assert cached == 0

    def test_with_cost_rub(self) -> None:
        payload = {"usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost_rub": 1.23}}
        inp, out, total, cost_rub, cached = _extract_usage(payload)
        assert inp == 100
        assert cost_rub == 1.23

    def test_with_cost_alias(self) -> None:
        payload = {"usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost": 2.50}}
        inp, out, total, cost_rub, cached = _extract_usage(payload)
        assert cost_rub == 2.50

    def test_missing_usage(self) -> None:
        inp, out, total, cost_rub, cached = _extract_usage({})
        assert inp == 0
        assert out == 0
        assert total == 0
        assert cost_rub is None
        assert cached == 0

    def test_partial_usage(self) -> None:
        payload = {"usage": {"input_tokens": 200}}
        inp, out, total, cost_rub, cached = _extract_usage(payload)
        assert inp == 200
        assert out == 0
        assert total == 0
        assert cost_rub is None

    def test_with_cached_tokens(self) -> None:
        payload = {"usage": {
            "input_tokens": 2000, "output_tokens": 500, "total_tokens": 2500,
            "prompt_tokens_details": {"cached_tokens": 1800},
        }}
        inp, out, total, cost_rub, cached = _extract_usage(payload)
        assert inp == 2000
        assert cached == 1800

    def test_with_input_tokens_details(self) -> None:
        payload = {"usage": {
            "input_tokens": 2000, "output_tokens": 500, "total_tokens": 2500,
            "input_tokens_details": {"cached_tokens": 1500},
        }}
        inp, out, total, cost_rub, cached = _extract_usage(payload)
        assert cached == 1500


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
        import grok_critic.api_client as mod

        mod._client = None
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.timeout_seconds = 30
            client = await get_client()
            assert isinstance(client, httpx.AsyncClient)
            await client.aclose()
            mod._client = None

    async def test_reuses_client(self) -> None:
        import grok_critic.api_client as mod

        mod._client = None
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.timeout_seconds = 30
            c1 = await get_client()
            c2 = await get_client()
            assert c1 is c2
            await c1.aclose()
            mod._client = None


# END_BLOCK_PERSISTENT_CLIENT


# START_BLOCK_CLIENT_CALL
class TestResponsesClientCall:
    @pytest.fixture()
    def client(self) -> ResponsesClient:
        with patch("grok_critic.api_client.config") as mock_cfg:
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.api_key = "test-key"
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.timeout_seconds = 30
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
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
            assert "Auth error" in result.error
            assert "Unauthorized" in result.error

    async def test_429_error(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(429, text="Too Many Requests")
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Rate limited" in result.error

    async def test_500_error(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(500, text="Internal Server Error")
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "Server error" in result.error

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
            assert "Payment required" in result.error
            assert "Недостаточно средств" in result.error

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
            assert "Provider down" in result.error
            assert "xAI provider unavailable" in result.error

    async def test_503_no_providers(self, client: ResponsesClient) -> None:
        mock_response = httpx.Response(503, text="Service Unavailable")
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(return_value=mock_response)
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "No providers" in result.error

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
        with patch("grok_critic.api_client.get_client", new_callable=AsyncMock) as mock_gc:
            mock_httpx = AsyncMock()
            mock_httpx.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_gc.return_value = mock_httpx
            result = await client.call("test")
            assert not result.success
            assert "timed out" in result.error.lower()

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
            assert "Invalid JSON" in result.error

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
            mock_cfg.price_output_per_1m = 30.0   # $30 per 1M → 500 tokens = $0.015
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.timeout_seconds = 30

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
