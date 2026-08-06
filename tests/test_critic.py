# FILE: tests/test_critic.py
# VERSION: 1.9.0
# START_MODULE_CONTRACT
#   PURPOSE: Tests for M-CRITIC prompt building, review logic, followup, health_check
#   SCOPE: Test _build_user_prompt, structured_review, followup, health_check
#   DEPENDS: M-CRITIC, M-CONFIG, M-API
#   LINKS: M-CRITIC
# END_MODULE_CONTRACT

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from grok_critic.api_client import CritiqueResult
from grok_critic.config import config
from grok_critic.critic import (
    ARCHITECTURE_SYSTEM_PROMPT,
    CRITIC_SYSTEM_PROMPT,
    FOLLOWUP_SYSTEM_PROMPT,
    SECURITY_SYSTEM_PROMPT,
    _build_user_prompt,
    do_architecture_review,
    do_security_audit,
    followup,
    health_check,
    structured_review,
)


# START_BLOCK_PROMPT_BUILDING
class TestBuildUserPrompt:
    def test_content_only(self) -> None:
        result = _build_user_prompt("print('hello')")
        assert "print('hello')" in result
        assert "Контекст" not in result
        assert "Фокус внимания" not in result

    def test_with_context(self) -> None:
        result = _build_user_prompt("code", context="FastAPI project")
        assert "FastAPI project" in result
        assert "Контекст" in result

    def test_with_focus_areas(self) -> None:
        result = _build_user_prompt("code", focus_areas=["security", "performance"])
        assert "security" in result
        assert "performance" in result
        assert "Фокус внимания" in result

    def test_with_all_params(self) -> None:
        result = _build_user_prompt(
            "def foo(): pass",
            context="Utility module",
            focus_areas=["SOLID", "DRY"],
        )
        assert "Utility module" in result
        assert "SOLID" in result
        assert "def foo(): pass" in result


# END_BLOCK_PROMPT_BUILDING


# START_BLOCK_SYSTEM_PROMPT
class TestSystemPrompt:
    def test_contains_sections(self) -> None:
        assert "Логические ошибки" in CRITIC_SYSTEM_PROMPT
        assert "SOLID" in CRITIC_SYSTEM_PROMPT
        assert "DRY" in CRITIC_SYSTEM_PROMPT
        assert "KISS" in CRITIC_SYSTEM_PROMPT
        assert "Производительность" in CRITIC_SYSTEM_PROMPT
        assert "Безопасность" in CRITIC_SYSTEM_PROMPT

    def test_instructs_russian(self) -> None:
        assert "русском" in CRITIC_SYSTEM_PROMPT.lower()


# END_BLOCK_SYSTEM_PROMPT


# START_BLOCK_STRUCTURED_REVIEW
class TestStructuredReview:
    async def test_empty_content_returns_error(self) -> None:
        result = await structured_review("")
        assert not result.success
        assert "Пустой контент" in result.error

    async def test_whitespace_only_returns_error(self) -> None:
        result = await structured_review("   \n\t  ")
        assert not result.success
        assert "Пустой контент" in result.error

    async def test_delegates_to_api_client(self) -> None:
        mock_result = CritiqueResult(
            text="Found 3 issues",
            model="x-ai/grok-4.20-multi-agent",
            agent_count=16,
            effort="high",
            review_id="rev_abc123",
        )
        with patch(
            "grok_critic.critic.ResponsesClient.call",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await structured_review("def foo(): pass")
            assert result.success is True
            assert result.text == "Found 3 issues"

    async def test_passes_focus_areas(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=8, effort="medium", review_id="rev_123"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await structured_review(
                "code",
                focus_areas=["security"],
                agent_count=8,
            )
            call_args = call_mock.call_args
            assert "security" in call_args.kwargs.get("prompt", call_args[1].get("prompt", ""))

    async def test_returns_critique_result(self) -> None:
        mock_result = CritiqueResult(
            text="review",
            model="m",
            agent_count=4,
            effort="low",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_usd=0.001,
            review_id="rev_abc123",
        )
        with patch("grok_critic.critic.ResponsesClient.call", new_callable=AsyncMock, return_value=mock_result):
            result = await structured_review("code")
            assert isinstance(result, CritiqueResult)
            assert result.input_tokens == 100
            assert result.review_id == "rev_abc123"


# END_BLOCK_STRUCTURED_REVIEW


# START_BLOCK_FOLLOWUP
class TestFollowup:
    async def test_basic_followup(self) -> None:
        mock_result = CritiqueResult(
            text="Here is the clarification",
            model="m",
            agent_count=16,
            effort="high",
            review_id="rev_followup1",
        )
        with patch("grok_critic.critic.ResponsesClient.call", new_callable=AsyncMock, return_value=mock_result):
            result = await followup("Previous review text", "What about security?")
            assert result.success is True
            assert result.text == "Here is the clarification"

    async def test_empty_previous_review(self) -> None:
        result = await followup("", "question")
        assert not result.success
        assert "Пустой" in result.error

    async def test_empty_question(self) -> None:
        result = await followup("review text", "  ")
        assert not result.success
        assert "Пустой" in result.error

    async def test_prompt_includes_review_and_question(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="answer", model="m", agent_count=16, effort="high", review_id="rev_1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await followup("Previous review", "Explain more")
            prompt = call_mock.call_args.kwargs["prompt"]
            assert "Previous review" in prompt
            assert "Explain more" in prompt

    async def test_uses_followup_system_prompt(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="answer", model="m", agent_count=16, effort="high", review_id="rev_1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await followup("review", "question")
            sys_prompt = call_mock.call_args.kwargs.get("system_prompt", "")
            assert sys_prompt == FOLLOWUP_SYSTEM_PROMPT

    async def test_custom_agent_count(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="answer", model="m", agent_count=4, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await followup("review", "question", agent_count=4)
            assert call_mock.call_args.kwargs.get("agent_count") == 4


# END_BLOCK_FOLLOWUP


# START_BLOCK_HEALTH_CHECK
class TestHealthCheck:
    @pytest.fixture()
    def mock_balance(self):
        """Mock balance API to return a fake balance."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"amount": "1250.50"}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        # Make async with ... as client: return mock_client itself
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("grok_critic.critic.httpx.AsyncClient", return_value=mock_client):
            yield mock_client

    async def test_healthy_when_key_set(self, mock_balance) -> None:
        with patch("grok_critic.critic.config") as mock_cfg:
            mock_cfg.api_key = SecretStr("valid-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            result = await health_check()
            assert result["status"] == "ok"
            assert result["issues"] == []
            assert result["balance_rub"] == 1250.50

    async def test_degraded_when_no_key(self) -> None:
        with patch("grok_critic.critic.config") as mock_cfg:
            mock_cfg.api_key = SecretStr("")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            result = await health_check()
            assert result["status"] == "degraded"
            assert any("POLZA_API_KEY" in issue for issue in result["issues"])

    async def test_pricing_info_when_set(self, mock_balance) -> None:
        with patch("grok_critic.critic.config") as mock_cfg:
            mock_cfg.api_key = SecretStr("valid-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.price_input_per_1m = 2.6
            mock_cfg.price_output_per_1m = 6.6
            result = await health_check()
            assert "pricing" in result
            assert result["pricing"]["input_per_1m"] == 2.6
            assert result["pricing"]["output_per_1m"] == 6.6

    async def test_no_pricing_when_zero(self, mock_balance) -> None:
        with patch("grok_critic.critic.config") as mock_cfg:
            mock_cfg.api_key = SecretStr("valid-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            result = await health_check()
            assert "pricing" not in result


# END_BLOCK_HEALTH_CHECK


# START_BLOCK_SIZE_GUARD
class TestContentSizeGuard:
    """TEST-03 / REL-03: единый cost-guard MAX_CONTENT_CHARS для всех путей ревью."""

    async def test_structured_review_oversized(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "max_content_chars", 100)
        call_mock = AsyncMock()
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            result = await structured_review("x" * 101)
            assert not result.success
            assert "слишком большой" in result.error
            call_mock.assert_not_called()

    async def test_followup_oversized(self, monkeypatch) -> None:
        """REL-03: followup раньше обходил cost-guard — гигантский previous_review уходил в платный API."""
        monkeypatch.setattr(config, "max_content_chars", 100)
        call_mock = AsyncMock()
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            result = await followup(previous_review="x" * 90, question="y" * 20)
            assert not result.success
            assert "слишком большой" in result.error
            call_mock.assert_not_called()

    async def test_followup_within_limit(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "max_content_chars", 1000)
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="answer", model="m", agent_count=4, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            result = await followup(previous_review="review text", question="why?")
            assert result.success
            call_mock.assert_called_once()


# END_BLOCK_SIZE_GUARD


# START_BLOCK_SPECIALIZED_REVIEW_TESTS
class TestSpecializedReviews:
    """TEST-04: реальные do_architecture_review / do_security_audit — правильные system-промпты и focus_areas."""

    async def test_architecture_review_uses_arch_prompt(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=4, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            result = await do_architecture_review("monolith with layers", agent_count=4)
            assert result.success
            kwargs = call_mock.call_args.kwargs
            assert kwargs["system_prompt"] == ARCHITECTURE_SYSTEM_PROMPT
            assert "architecture" in kwargs["prompt"]
            assert "scalability" in kwargs["prompt"]

    async def test_security_audit_uses_security_prompt(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=4, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            result = await do_security_audit("app.get('/u/<id>')", agent_count=4)
            assert result.success
            kwargs = call_mock.call_args.kwargs
            assert kwargs["system_prompt"] == SECURITY_SYSTEM_PROMPT
            assert "security" in kwargs["prompt"]
            assert "vulnerabilities" in kwargs["prompt"]

    async def test_architecture_review_empty_content(self) -> None:
        result = await do_architecture_review("   ")
        assert not result.success
        assert "архитектурного ревью" in result.error


# END_BLOCK_SPECIALIZED_REVIEW_TESTS
