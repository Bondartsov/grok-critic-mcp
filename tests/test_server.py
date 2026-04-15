# FILE: tests/test_server.py
# VERSION: 1.1.0
# START_MODULE_CONTRACT
#   PURPOSE: Tests for M-SERVER MCP tool registration and invocation
#   SCOPE: Verify tools are registered, parameters work, calls delegate correctly
#   DEPENDS: M-SERVER, M-CRITIC
#   LINKS: M-SERVER
# END_MODULE_CONTRACT

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from grok_critic.api_client import CritiqueResult
from grok_critic.server import (
    check_health,
    critic_followup,
    critic_review,
    reload_config_tool,
    restart_server,
    server,
)


# START_BLOCK_TOOL_REGISTRATION
class TestToolRegistration:
    def test_server_has_tools(self) -> None:
        tools = server._tool_manager.list_tools()
        tool_names = {t.name for t in tools}
        assert "critic_review" in tool_names
        assert "check_health" in tool_names
        assert "critic_followup" in tool_names
        assert "reload_config_tool" in tool_names
        assert "restart_server" in tool_names

    def test_critic_review_tool_has_params(self) -> None:
        tools = server._tool_manager.list_tools()
        critic_tool = next(t for t in tools if t.name == "critic_review")
        schema = critic_tool.parameters
        properties = schema.get("properties", {})
        assert "content" in properties
        assert "context" in properties
        assert "agent_count" in properties
        assert "focus_areas" in properties
        assert "content" in schema.get("required", [])

    def test_critic_followup_tool_has_params(self) -> None:
        tools = server._tool_manager.list_tools()
        followup_tool = next(t for t in tools if t.name == "critic_followup")
        schema = followup_tool.parameters
        properties = schema.get("properties", {})
        assert "previous_review" in properties
        assert "question" in properties
        assert "agent_count" in properties
        assert "previous_review" in schema.get("required", [])
        assert "question" in schema.get("required", [])


# END_BLOCK_TOOL_REGISTRATION


# START_BLOCK_CRITIC_REVIEW_TOOL
class TestCriticReviewTool:
    async def test_basic_call(self) -> None:
        mock_result = CritiqueResult(
            text="Review: looks good",
            model="x-ai/grok-4.20-multi-agent",
            agent_count=16,
            effort="high",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_usd=0.0,
            review_id="rev_abc123",
        )
        with patch(
            "grok_critic.server.structured_review",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await critic_review(content="def foo(): pass")
            assert "looks good" in result
            assert "rev_abc123" in result

    async def test_passes_all_params(self) -> None:
        mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=8, effort="medium", review_id="rev_1"
        ))
        with patch("grok_critic.server.structured_review", new=mock):
            await critic_review(
                content="code",
                context="FastAPI",
                agent_count=8,
                focus_areas="security,performance",
            )
            call_kwargs = mock.call_args.kwargs
            assert call_kwargs["content"] == "code"
            assert call_kwargs["context"] == "FastAPI"
            assert call_kwargs["agent_count"] == 8
            assert call_kwargs["focus_areas"] == ["security", "performance"]

    async def test_focus_areas_parsing(self) -> None:
        mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=16, effort="high", review_id="rev_1"
        ))
        with patch("grok_critic.server.structured_review", new=mock):
            await critic_review(content="code", focus_areas="  a , b , c  ")
            assert mock.call_args.kwargs["focus_areas"] == ["a", "b", "c"]

    async def test_metadata_in_response(self) -> None:
        mock_result = CritiqueResult(
            text="Nice code",
            model="x-ai/grok-4.20-multi-agent",
            agent_count=16,
            effort="high",
            input_tokens=1240,
            output_tokens=870,
            total_tokens=2110,
            cost_usd=0.0124,
            review_id="rev_test1234",
        )
        with patch(
            "grok_critic.server.structured_review",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await critic_review(content="code")
            assert "Metadata" in result
            assert "agents=16" in result
            assert "input=1\u2009240" in result
            assert "output=870" in result
            assert "rev_test1234" in result
            assert "$0.0124" in result

    async def test_error_response(self) -> None:
        mock_result = CritiqueResult(
            text="",
            model="m",
            agent_count=4,
            effort="low",
            error="API key invalid",
        )
        with patch(
            "grok_critic.server.structured_review",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await critic_review(content="code")
            assert "❌ Error:" in result
            assert "API key invalid" in result


# END_BLOCK_CRITIC_REVIEW_TOOL


# START_BLOCK_CRITIC_FOLLOWUP_TOOL
class TestCriticFollowupTool:
    async def test_basic_followup(self) -> None:
        mock_result = CritiqueResult(
            text="Here is the clarification",
            model="x-ai/grok-4.20-multi-agent",
            agent_count=16,
            effort="high",
            input_tokens=80,
            output_tokens=40,
            total_tokens=120,
            cost_usd=0.0,
            review_id="rev_follow1",
        )
        with patch(
            "grok_critic.server.followup",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await critic_followup(
                previous_review="Found 3 issues",
                question="What about security?",
            )
            assert "clarification" in result
            assert "rev_follow1" in result

    async def test_followup_error(self) -> None:
        mock_result = CritiqueResult(
            text="",
            model="m",
            agent_count=4,
            effort="low",
            error="Empty review",
        )
        with patch(
            "grok_critic.server.followup",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await critic_followup(
                previous_review="",
                question="What?",
            )
            assert "❌ Error:" in result

    async def test_followup_with_agent_count(self) -> None:
        mock = AsyncMock(return_value=CritiqueResult(
            text="answer", model="m", agent_count=4, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.server.followup", new=mock):
            await critic_followup(
                previous_review="review text",
                question="explain",
                agent_count=4,
            )
            assert mock.call_args.kwargs["agent_count"] == 4


# END_BLOCK_CRITIC_FOLLOWUP_TOOL


# START_BLOCK_HEALTH_CHECK_TOOL
class TestHealthCheckTool:
    async def test_returns_status(self) -> None:
        with patch(
            "grok_critic.server.health_check",
            new_callable=AsyncMock,
            return_value={
                "status": "ok",
                "model": "x-ai/grok-4.20-multi-agent",
                "base_url": "https://polza.ai/api/v1",
                "issues": [],
            },
        ):
            result = await check_health()
            assert "ok" in result
            assert "grok-4.20-multi-agent" in result

    async def test_with_pricing_info(self) -> None:
        with patch(
            "grok_critic.server.health_check",
            new_callable=AsyncMock,
            return_value={
                "status": "ok",
                "model": "x-ai/grok-4.20-multi-agent",
                "base_url": "https://polza.ai/api/v1",
                "issues": [],
                "pricing": {"input_per_1m": 2.6, "output_per_1m": 6.6},
            },
        ):
            result = await check_health()
            assert "Pricing" in result
            assert "2.6" in result
            assert "6.6" in result
            assert "/1M" in result


# END_BLOCK_HEALTH_CHECK_TOOL


# START_BLOCK_RELOAD_CONFIG_TOOL
class TestReloadConfigTool:
    async def test_successful_reload(self) -> None:
        with patch(
            "grok_critic.server.reload_config",
            return_value=type("Cfg", (), {
                "api_key": "pza_testkey123",
                "base_url": "https://polza.ai/api/v1",
                "model": "x-ai/grok-4.20-multi-agent",
                "agent_count": 16,
                "timeout_seconds": 300,
                "log_level": "WARNING",
                "price_input_per_1m": 2.6,
                "price_output_per_1m": 6.6,
            })(),
        ), patch("grok_critic.server.close_client", new_callable=AsyncMock):
            result = await reload_config_tool()
            assert "✅ Config reloaded" in result
            assert "grok-4.20-multi-agent" in result
            assert "$2.6" in result
            assert "$6.6" in result
            assert "y123" in result  # masked key last 4 chars

    async def test_reload_failure(self) -> None:
        with patch(
            "grok_critic.server.reload_config",
            side_effect=RuntimeError("missing .env"),
        ):
            result = await reload_config_tool()
            assert "❌ Reload failed" in result
            assert "missing .env" in result


# END_BLOCK_RELOAD_CONFIG_TOOL


# START_BLOCK_RESTART_SERVER_TOOL
class TestRestartServerTool:
    async def test_restart_calls_exit(self) -> None:
        with patch("grok_critic.server.close_client", new_callable=AsyncMock) as mock_close, \
             patch("grok_critic.server.os._exit") as mock_exit:
            await restart_server(reason="agent requested")
            mock_close.assert_awaited_once()
            mock_exit.assert_called_once_with(0)

    async def test_restart_default_reason(self) -> None:
        with patch("grok_critic.server.close_client", new_callable=AsyncMock), \
             patch("grok_critic.server.os._exit"):
            await restart_server()
            # No assertion needed — just verify it doesn't crash


# END_BLOCK_RESTART_SERVER_TOOL
