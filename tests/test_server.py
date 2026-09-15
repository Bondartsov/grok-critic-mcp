# FILE: tests/test_server.py
# VERSION: 1.11.0
# START_MODULE_CONTRACT
#   PURPOSE: Tests for M-SERVER MCP tool registration and invocation
#   SCOPE: Registration, params, delegation, sandbox denylist, file_path opt-in,
#          heartbeat/elapsed, JSON mode, reload/restart/self_update
#   DEPENDS: M-SERVER, M-CRITIC
#   LINKS: M-SERVER
# END_MODULE_CONTRACT

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from pydantic import SecretStr

from grok_critic.api_client import CritiqueResult
from grok_critic.config import config
from grok_critic.server import (
    _heartbeat,
    _parse_json_loose,
    _read_file_content,
    _validate_agent_count,
    architecture_review,
    check_health,
    critic_followup,
    critic_review,
    reload_config_tool,
    restart_server,
    security_audit,
    self_update,
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
        assert "self_update" in tool_names
        assert len(tools) == 8

    def test_critic_review_tool_has_params(self) -> None:
        tools = server._tool_manager.list_tools()
        critic_tool = next(t for t in tools if t.name == "critic_review")
        schema = critic_tool.parameters
        properties = schema.get("properties", {})
        assert "content" in properties
        assert "context" in properties
        assert "agent_count" in properties
        assert "focus_areas" in properties
        assert "output_format" in properties  # FEAT-JSON
        assert "content" in schema.get("required", [])

    def test_critic_followup_tool_has_params(self) -> None:
        """FEAT-FOLLOWUP-ID: required только question; review_id доступен."""
        tools = server._tool_manager.list_tools()
        followup_tool = next(t for t in tools if t.name == "critic_followup")
        schema = followup_tool.parameters
        properties = schema.get("properties", {})
        assert "previous_review" in properties
        assert "question" in properties
        assert "agent_count" in properties
        assert "review_id" in properties
        assert schema.get("required", []) == ["question"]


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
            "grok_critic.server.general_review",
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
        with patch("grok_critic.server.general_review", new=mock):
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
        with patch("grok_critic.server.general_review", new=mock):
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
            "grok_critic.server.general_review",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await critic_review(content="code")
            assert "Metadata" in result
            assert "agents=16" in result
            assert "input=1 240" in result
            assert "output=870" in result
            assert "rev_test1234" in result
            assert "$0.0124" in result

    async def test_elapsed_in_metadata(self) -> None:
        """FEAT-PROGRESS: в metadata присутствует время выполнения."""
        mock_result = CritiqueResult(
            text="ok", model="m", agent_count=16, effort="high", review_id="rev_el1"
        )
        with patch("grok_critic.server.general_review", new_callable=AsyncMock, return_value=mock_result):
            await asyncio.sleep(0.01)
            result = await critic_review(content="code")
            assert "⏱ Elapsed:" in result

    async def test_error_response(self) -> None:
        mock_result = CritiqueResult(
            text="",
            model="m",
            agent_count=4,
            effort="low",
            error="API key invalid",
        )
        with patch(
            "grok_critic.server.general_review",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await critic_review(content="code")
            assert "❌ Error:" in result
            assert "API key invalid" in result

    async def test_json_output_normalized(self) -> None:
        """FEAT-JSON: валидный JSON от модели нормализуется к чистому pretty-JSON."""
        raw = 'Вот разбор:\n```json\n{"summary": "s", "findings": []}\n```'
        mock_result = CritiqueResult(
            text=raw, model="m", agent_count=16, effort="high", review_id="rev_js1"
        )
        with patch("grok_critic.server.general_review", new_callable=AsyncMock, return_value=mock_result):
            result = await critic_review(content="code", output_format="json")
        # до metadata-footer идёт чистый JSON
        body = result.split("\n---")[0]
        parsed = json.loads(body)
        assert parsed["summary"] == "s"

    async def test_json_output_fallback_when_not_json(self) -> None:
        mock_result = CritiqueResult(
            text="модель ответила текстом", model="m", agent_count=4, effort="low", review_id="rev_js2"
        )
        with patch("grok_critic.server.general_review", new_callable=AsyncMock, return_value=mock_result):
            result = await critic_review(content="code", output_format="json")
            assert "⚠️ Модель вернула не-JSON" in result
            assert "модель ответила текстом" in result


# END_BLOCK_CRITIC_REVIEW_TOOL


# START_BLOCK_PARSE_JSON_LOOSE
class TestParseJsonLoose:
    def test_plain_json(self) -> None:
        assert _parse_json_loose('{"a": 1}') == {"a": 1}

    def test_json_code_fence(self) -> None:
        text = "```json\n{\"a\": 2}\n```"
        assert _parse_json_loose(text) == {"a": 2}

    def test_json_embedded_in_text(self) -> None:
        text = "Вывод: {\"a\": 3} конец"
        assert _parse_json_loose(text) == {"a": 3}

    def test_not_json_returns_none(self) -> None:
        assert _parse_json_loose("совсем не json") is None

    def test_non_dict_json_returns_none(self) -> None:
        assert _parse_json_loose("[1, 2, 3]") is None


# END_BLOCK_PARSE_JSON_LOOSE


# START_BLOCK_HEARTBEAT_TESTS
class TestHeartbeat:
    async def test_heartbeat_sends_info_until_stopped(self) -> None:
        """FEAT-PROGRESS: heartbeat шлёт уведомления, пока не установлен stop."""
        ctx = AsyncMock()
        stop = asyncio.Event()
        task = asyncio.create_task(_heartbeat(ctx, "critic_review", stop, interval=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await task
        assert ctx.info.await_count >= 1

    async def test_heartbeat_silent_after_immediate_stop(self) -> None:
        ctx = AsyncMock()
        stop = asyncio.Event()
        stop.set()  # ревью завершилось мгновенно
        await _heartbeat(ctx, "t", stop, interval=0.01)
        assert ctx.info.await_count == 0

    async def test_decorator_without_ctx_no_crash(self) -> None:
        """Прямые вызовы без ctx (как в тестах/клиентах без progress) работают."""
        mock_result = CritiqueResult(text="ok", model="m", agent_count=4, effort="low", review_id="rev_hb")
        with patch("grok_critic.server.general_review", new_callable=AsyncMock, return_value=mock_result):
            result = await critic_review(content="x")
            assert "ok" in result


# END_BLOCK_HEARTBEAT_TESTS


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

    async def test_followup_with_review_id(self) -> None:
        """FEAT-FOLLOWUP-ID: review_id пробрасывается в critic.followup."""
        mock = AsyncMock(return_value=CritiqueResult(
            text="answer", model="m", agent_count=16, effort="high", review_id="rev_new"
        ))
        with patch("grok_critic.server.followup", new=mock):
            await critic_followup(question="why?", review_id="rev_orig123")
            kwargs = mock.call_args.kwargs
            assert kwargs["review_id"] == "rev_orig123"
            assert kwargs["question"] == "why?"


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

    async def test_usage_today_rendered(self) -> None:
        """FEAT-BUDGET: суточная статистика видна в выводе health_check."""
        with patch(
            "grok_critic.server.health_check",
            new_callable=AsyncMock,
            return_value={
                "status": "ok",
                "model": "m",
                "base_url": "https://polza.ai/api/v1",
                "issues": [],
                "usage_today": {"calls": 3, "errors": 1, "cost_usd": 0.1234, "cost_rub": 45.6},
            },
        ):
            result = await check_health()
            assert "Today: 3 calls" in result
            assert "$0.1234" in result
            assert "45.60 ₽" in result

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
                "api_key": SecretStr("pza_testkey123"),
                "base_url": "https://polza.ai/api/v1",
                "model": "x-ai/grok-4.20-multi-agent",
                "agent_count": 16,
                "timeout_seconds": 300,
                "log_level": "WARNING",
                "price_input_per_1m": 2.6,
                "price_output_per_1m": 6.6,
                "allow_file_path": False,
                "daily_budget_usd": 5.0,
                "max_concurrent_requests": 2,
                "retry_deadline_seconds": 0.0,
            })(),
        ), patch("grok_critic.server.close_client", new_callable=AsyncMock):
            result = await reload_config_tool()
            assert "✅ Config reloaded" in result
            assert "grok-4.20-multi-agent" in result
            assert "$2.6" in result
            assert "$6.6" in result
            assert "y123" in result  # masked key last 4 chars
            assert "daily_budget_usd: $5.0" in result
            assert "(auto = timeout_seconds)" in result

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


# START_BLOCK_SELF_UPDATE_TOOL
class TestSelfUpdateTool:
    async def test_disabled_by_default(self) -> None:
        with patch("grok_critic.server.config") as mock_cfg:
            mock_cfg.allow_self_update = False
            result = await self_update()
            assert "disabled" in result

    async def test_already_up_to_date(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Already up to date.", b""))
        mock_proc.returncode = 0

        with patch("grok_critic.server.config") as mock_cfg, \
             patch("grok_critic.server.asyncio.create_subprocess_exec", return_value=mock_proc):
            mock_cfg.allow_self_update = True
            result = await self_update()
            assert "Already up to date" in result

    async def test_git_pull_fails(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"fatal: not a git repository"))
        mock_proc.returncode = 128

        with patch("grok_critic.server.config") as mock_cfg, \
             patch("grok_critic.server.asyncio.create_subprocess_exec", return_value=mock_proc):
            mock_cfg.allow_self_update = True
            result = await self_update()
            assert "❌ git pull failed" in result
            assert "128" in result

    async def test_full_update_flow(self) -> None:
        git_proc = AsyncMock()
        git_proc.communicate = AsyncMock(return_value=(
            b"Updating abdc10d..de15bb0\nFast-forward\n src/server.py | 5 +++--\n 1 file changed",
            b"",
        ))
        git_proc.returncode = 0

        pip_proc = AsyncMock()
        pip_proc.communicate = AsyncMock(return_value=(
            b"Successfully installed grok-critic-mcp-1.10.0",
            b"",
        ))
        pip_proc.returncode = 0

        call_count = 0

        async def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return git_proc if call_count == 1 else pip_proc

        with patch("grok_critic.server.config") as mock_cfg, \
             patch("grok_critic.server.asyncio.create_subprocess_exec", side_effect=mock_subprocess), \
             patch("grok_critic.server.close_client", new_callable=AsyncMock), \
             patch("grok_critic.server.os._exit") as mock_exit:
            mock_cfg.allow_self_update = True
            await self_update()
            mock_exit.assert_called_once_with(0)


# END_BLOCK_SELF_UPDATE_TOOL


# START_BLOCK_VALIDATE_AGENT_COUNT
class TestValidateAgentCount:
    def test_none_passes_through(self) -> None:
        assert _validate_agent_count(None) is None

    def test_zero_clamps_to_one(self) -> None:
        assert _validate_agent_count(0) == 1

    def test_negative_clamps_to_one(self) -> None:
        assert _validate_agent_count(-5) == 1

    def test_over_64_clamps_to_64(self) -> None:
        assert _validate_agent_count(100) == 64

    def test_valid_values_pass(self) -> None:
        assert _validate_agent_count(4) == 4
        assert _validate_agent_count(16) == 16
        assert _validate_agent_count(64) == 64

    def test_boundary_values(self) -> None:
        assert _validate_agent_count(1) == 1
        assert _validate_agent_count(64) == 64


# END_BLOCK_VALIDATE_AGENT_COUNT


# START_BLOCK_READ_FILE_CONTENT
class TestReadFileContent:
    def test_nonexistent_file(self) -> None:
        content, err = _read_file_content("/nonexistent/path/file.py")
        assert content == ""
        assert "not found" in err.lower() or "not a file" in err.lower()

    def test_empty_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "allowed_read_dirs", str(tmp_path))
        f = tmp_path / "empty.py"
        f.write_text("")
        content, err = _read_file_content(str(f))
        assert content == ""
        assert "empty" in err.lower()

    def test_valid_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(config, "allowed_read_dirs", str(tmp_path))
        f = tmp_path / "code.py"
        f.write_text("def hello(): pass", encoding="utf-8")
        content, err = _read_file_content(str(f))
        assert err is None
        assert "hello" in content

    def test_directory_path(self, tmp_path) -> None:
        content, err = _read_file_content(str(tmp_path))
        assert content == ""
        assert "not a file" in err.lower()

    def test_outside_allowed_dirs_denied(self, tmp_path) -> None:
        """SEC-01: файл вне cwd и POLZA_ALLOWED_READ_DIRS отклоняется."""
        f = tmp_path / "code.py"
        f.write_text("x = 1", encoding="utf-8")
        content, err = _read_file_content(str(f))
        assert content == ""
        assert "denied" in err.lower()

    def test_path_traversal_denied(self, tmp_path, monkeypatch) -> None:
        """SEC-01: ../ из разрешённой директории наружу отклоняется."""
        secret_dir = tmp_path / "secret"
        secret_dir.mkdir()
        (secret_dir / "passwords.txt").write_text("hunter2", encoding="utf-8")
        allowed = tmp_path / "project"
        allowed.mkdir()
        monkeypatch.setattr(config, "allowed_read_dirs", str(allowed))
        content, err = _read_file_content(str(allowed / ".." / "secret" / "passwords.txt"))
        assert content == ""
        assert "denied" in err.lower()

    def test_sensitive_name_denied_inside_allowed(self, tmp_path, monkeypatch) -> None:
        """SEC-01: .env/id_rsa блокируются даже внутри разрешённой директории."""
        monkeypatch.setattr(config, "allowed_read_dirs", str(tmp_path))
        for name in (".env", "id_rsa", "credentials.json"):
            f = tmp_path / name
            f.write_text("SECRET=1", encoding="utf-8")
            content, err = _read_file_content(str(f))
            assert content == "", f"{name} should be blocked"
            assert "sensitive" in err.lower()

    def test_sensitive_suffix_denied_inside_allowed(self, tmp_path, monkeypatch) -> None:
        """SEC-01: *.pem/*.key блокируются даже внутри разрешённой директории."""
        monkeypatch.setattr(config, "allowed_read_dirs", str(tmp_path))
        for name in ("server.pem", "private.key"):
            f = tmp_path / name
            f.write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
            content, err = _read_file_content(str(f))
            assert content == "", f"{name} should be blocked"
            assert "sensitive" in err.lower()

    def test_sensitive_glob_variants_denied(self, tmp_path, monkeypatch) -> None:
        """SEC-02: точечный denylist обходился вариантами имён — глобы их ловят."""
        monkeypatch.setattr(config, "allowed_read_dirs", str(tmp_path))
        blocked = (
            ".env.local",              # вариант .env
            ".env.production",
            "credentials.prod.json",   # вариант credentials.json
            "my-credentials.txt",
            "id_rsa.pub",              # публичный ключ той же пары
            "backup_id_rsa_old",
            ".git-credentials",
            ".netrc",
            ".htpasswd",
            "service.pfx",
            "vault.kdbx",
        )
        for name in blocked:
            f = tmp_path / name
            f.write_text("SECRET=1", encoding="utf-8")
            content, err = _read_file_content(str(f))
            assert content == "", f"{name} should be blocked by glob denylist"
            assert "sensitive" in err.lower(), f"{name}: wrong error"

    def test_git_config_denied(self, tmp_path, monkeypatch) -> None:
        """SEC-02: .git/config содержит токены remote-URL — заблокирован."""
        monkeypatch.setattr(config, "allowed_read_dirs", str(tmp_path))
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        cfg = git_dir / "config"
        cfg.write_text("[remote \"origin\"]\n url = https://token@github.com/x/y.git", encoding="utf-8")
        content, err = _read_file_content(str(cfg))
        assert content == ""
        assert "sensitive" in err.lower()

    def test_case_insensitive_denylist(self, tmp_path, monkeypatch) -> None:
        """SEC-02: регистр имени не обходит denylist (SECRET.PEM)."""
        monkeypatch.setattr(config, "allowed_read_dirs", str(tmp_path))
        f = tmp_path / "SECRET.PEM"
        f.write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")
        content, err = _read_file_content(str(f))
        assert content == ""
        assert "sensitive" in err.lower()

    def test_normal_files_not_blocked_by_globs(self, tmp_path, monkeypatch) -> None:
        """SEC-02: легитимные имена, похожие на секреты, НЕ блокируются лишний раз."""
        monkeypatch.setattr(config, "allowed_read_dirs", str(tmp_path))
        ok_names = ("keyboard.py", "monkey.py", "secrets.py", "config.yaml", ".github-workflow.yml")
        for name in ok_names:
            f = tmp_path / name
            f.write_text("# ok", encoding="utf-8")
            content, err = _read_file_content(str(f))
            assert err is None, f"{name} should be readable"
            assert "# ok" in content

    def test_multiple_allowed_dirs(self, tmp_path, monkeypatch) -> None:
        """Несколько директорий через os.pathsep — обе доступны."""
        import os

        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "a.py").write_text("a = 1", encoding="utf-8")
        (dir_b / "b.py").write_text("b = 2", encoding="utf-8")
        monkeypatch.setattr(config, "allowed_read_dirs", f"{dir_a}{os.pathsep}{dir_b}")
        content_a, err_a = _read_file_content(str(dir_a / "a.py"))
        content_b, err_b = _read_file_content(str(dir_b / "b.py"))
        assert err_a is None and "a = 1" in content_a
        assert err_b is None and "b = 2" in content_b


# END_BLOCK_READ_FILE_CONTENT


# START_BLOCK_DECORATOR_TESTS
class TestDecoratorBehavior:
    async def test_decorator_catches_exception(self) -> None:
        """Decorator wraps unexpected exceptions into user-friendly error strings."""
        with patch(
            "grok_critic.server.general_review",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected boom"),
        ):
            result = await critic_review(content="code")
            assert "❌" in result
            assert "unexpected boom" in result

    async def test_decorator_clamps_agent_count(self) -> None:
        """Decorator validates and clamps agent_count before passing to critic."""
        mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=1, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.server.general_review", new=mock):
            await critic_review(content="code", agent_count=100)
            assert mock.call_args.kwargs["agent_count"] == 64

    async def test_decorator_clamps_negative_agent_count(self) -> None:
        mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=1, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.server.general_review", new=mock):
            await critic_review(content="code", agent_count=-1)
            assert mock.call_args.kwargs["agent_count"] == 1


# END_BLOCK_DECORATOR_TESTS


# START_BLOCK_ARCHITECTURE_SECURITY_TOOLS
class TestArchitectureReviewTool:
    async def test_basic_call(self) -> None:
        mock_result = CritiqueResult(
            text="Architecture looks solid",
            model="x-ai/grok-4.20-multi-agent",
            agent_count=16,
            effort="high",
            input_tokens=200,
            output_tokens=100,
            total_tokens=300,
            cost_usd=0.01,
            review_id="rev_arch1",
        )
        with patch(
            "grok_critic.server.do_architecture_review",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await architecture_review(content="microservices diagram")
            assert "solid" in result
            assert "rev_arch1" in result


class TestSecurityAuditTool:
    async def test_basic_call(self) -> None:
        mock_result = CritiqueResult(
            text="Found 2 SQL injection vulnerabilities",
            model="x-ai/grok-4.20-multi-agent",
            agent_count=16,
            effort="high",
            input_tokens=150,
            output_tokens=80,
            total_tokens=230,
            cost_usd=0.008,
            review_id="rev_sec1",
        )
        with patch(
            "grok_critic.server.do_security_audit",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await security_audit(content="SELECT * FROM users WHERE id=")
            assert "SQL injection" in result
            assert "rev_sec1" in result


# END_BLOCK_ARCHITECTURE_SECURITY_TOOLS


# START_BLOCK_FILE_PATH_INTEGRATION
class TestFilePathIntegration:
    """BUG-01 / TEST-06 / SEC-03: file_path через декоратор — интеграционные сценарии."""

    async def test_review_with_file_path(self, tmp_path, monkeypatch) -> None:
        """Валидный файл в разрешённой директории читается и уходит как content (при включённом opt-in)."""
        monkeypatch.setattr(config, "allow_file_path", True)
        monkeypatch.setattr(config, "allowed_read_dirs", str(tmp_path))
        f = tmp_path / "code.py"
        f.write_text("def hello(): pass", encoding="utf-8")
        mock = AsyncMock(return_value=CritiqueResult(
            text="review ok", model="m", agent_count=4, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.server.general_review", new=mock):
            result = await critic_review(file_path=str(f))
            assert "review ok" in result
            assert mock.call_args.kwargs["content"] == "def hello(): pass"
            assert mock.call_args.kwargs["context"] == f"File: {f}"

    async def test_file_path_disabled_by_default(self, tmp_path, monkeypatch) -> None:
        """SEC-03: по умолчанию file_path отключён — ошибка с подсказкой, API не вызывается."""
        monkeypatch.setattr(config, "allow_file_path", False)  # дефолт, явно для теста
        monkeypatch.delenv("POLZA_ALLOW_FILE_PATH", raising=False)
        f = tmp_path / "code.py"
        f.write_text("x = 1", encoding="utf-8")
        mock = AsyncMock()
        with patch("grok_critic.server.general_review", new=mock):
            result = await critic_review(file_path=str(f))
            assert "❌" in result
            assert "POLZA_ALLOW_FILE_PATH" in result
            mock.assert_not_called()

    async def test_review_file_path_outside_sandbox(self, tmp_path, monkeypatch) -> None:
        """Файл вне sandbox при включённом file_path — ошибка, general_review НЕ вызывается."""
        monkeypatch.setattr(config, "allow_file_path", True)
        f = tmp_path / "code.py"
        f.write_text("x = 1", encoding="utf-8")
        mock = AsyncMock()
        with patch("grok_critic.server.general_review", new=mock):
            result = await critic_review(file_path=str(f))
            assert "❌" in result
            assert "denied" in result.lower()
            mock.assert_not_called()

    async def test_sensitive_file_via_decorator_denied(self, tmp_path, monkeypatch) -> None:
        """SEC-02: даже при включённом file_path секрет не уходит в API."""
        monkeypatch.setattr(config, "allow_file_path", True)
        monkeypatch.setattr(config, "allowed_read_dirs", str(tmp_path))
        env_local = tmp_path / ".env.local"
        env_local.write_text("API_TOKEN=supersecret", encoding="utf-8")
        mock = AsyncMock()
        with patch("grok_critic.server.general_review", new=mock):
            result = await critic_review(file_path=str(env_local))
            assert "❌" in result
            assert "sensitive" in result.lower()
            mock.assert_not_called()

    async def test_followup_rejects_file_path(self, monkeypatch) -> None:
        """BUG-01: file_path у critic_followup — внятная ошибка, не TypeError."""
        monkeypatch.setattr(config, "allow_file_path", True)
        result = await critic_followup(
            previous_review="review", question="why?", file_path="/any/file.py"
        )
        assert "❌" in result
        assert "does not support file_path" in result


# END_BLOCK_FILE_PATH_INTEGRATION
