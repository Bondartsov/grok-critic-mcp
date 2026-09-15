# FILE: tests/test_cli.py
# VERSION: 1.11.1
# START_MODULE_CONTRACT
#   PURPOSE: Tests for M-CLI terminal interface (serve/health/doctor/review/followup/logs/config)
#   SCOPE: argparse wiring, exit codes, mocked API calls, store interaction, output formats
#   DEPENDS: M-CLI, M-CRITIC, M-CONFIG
#   LINKS: M-CLI
# END_MODULE_CONTRACT

from __future__ import annotations

import argparse
import io
import json
from unittest.mock import AsyncMock, patch

import pytest

import grok_critic.cli as cli
from grok_critic.api_client import CritiqueResult
from grok_critic.critic import review_store


# START_BLOCK_HELPERS
def _ok_result(**overrides) -> CritiqueResult:
    defaults = dict(
        text="review text",
        model="m",
        agent_count=4,
        effort="low",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cost_usd=0.001,
        cost_rub=0.1,
        review_id="rev_cli0001",
    )
    defaults.update(overrides)
    return CritiqueResult(**defaults)


def _args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Дисковый store (директория per-id файлов) изолируем в tmp_path."""
    monkeypatch.setattr(review_store, "_dir", tmp_path / "reviews")
    yield


# END_BLOCK_HELPERS


# START_BLOCK_HEALTH_TESTS
class TestHealthCommand:
    def test_no_ping_ok(self, capsys) -> None:
        rc = cli.cmd_health(_args(ping=False, json=False))
        assert rc == cli.EXIT_OK
        assert "Config OK" in capsys.readouterr().out

    def test_no_ping_store_broken(self, tmp_path, monkeypatch, capsys) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(review_store, "_dir", blocker / "db" / "reviews")
        rc = cli.cmd_health(_args(ping=False, json=False))
        assert rc == cli.EXIT_ERR
        assert "Store" in capsys.readouterr().err

    def test_ping_json(self, capsys) -> None:
        fake = {
            "status": "ok",
            "model": "m",
            "base_url": "https://x",
            "issues": [],
            "balance_rub": 100.5,
            "usage_today": {"calls": 2, "errors": 0, "cost_usd": 0.1, "cost_rub": 3.0},
        }
        with patch("grok_critic.cli.health_check", new_callable=AsyncMock, return_value=fake):
            rc = cli.cmd_health(_args(ping=True, json=True))
        assert rc == cli.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["balance_rub"] == 100.5
        assert payload["usage_today"]["calls"] == 2

    def test_ping_degraded(self, capsys) -> None:
        fake = {
            "status": "degraded",
            "model": "m",
            "base_url": "https://x",
            "issues": ["Balance API returned 500"],
            "usage_today": {"calls": 0, "errors": 1, "cost_usd": 0.0, "cost_rub": 0.0},
        }
        with patch("grok_critic.cli.health_check", new_callable=AsyncMock, return_value=fake):
            rc = cli.cmd_health(_args(ping=True, json=False))
        assert rc == cli.EXIT_WARN
        assert "Issue: Balance API returned 500" in capsys.readouterr().out


# END_BLOCK_HEALTH_TESTS


# START_BLOCK_REVIEW_TESTS
class TestReviewCommand:
    def test_stdin_review(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("def add(a, b):\n    return a - b\n"))
        with patch("grok_critic.cli.general_review", new_callable=AsyncMock, return_value=_ok_result()):
            rc = cli.cmd_review(_args(path="-", context="ctx", agents=4, focus="security", json=False, json_output=False))
        assert rc == cli.EXIT_OK
        assert "review text" in capsys.readouterr().out

    def test_review_passes_parsed_params(self, monkeypatch) -> None:
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("code"))
        mock = AsyncMock(return_value=_ok_result())
        with patch("grok_critic.cli.general_review", new=mock):
            cli.cmd_review(_args(path="-", context="FastAPI", agents=4, focus=" a , b ", json=False, json_output=False))
        kwargs = mock.call_args.kwargs
        assert kwargs["focus_areas"] == ["a", "b"]
        assert kwargs["context"] == "FastAPI"
        assert kwargs["agent_count"] == 4

    def test_review_clamps_agents(self, monkeypatch) -> None:
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("code"))
        mock = AsyncMock(return_value=_ok_result())
        with patch("grok_critic.cli.general_review", new=mock):
            cli.cmd_review(_args(path="-", context=None, agents=100, focus=None, json=False, json_output=False))
        assert mock.call_args.kwargs["agent_count"] == 64

    def test_review_file_not_found(self, tmp_path, capsys) -> None:
        rc = cli.cmd_review(_args(path=str(tmp_path / "nope.py"), context=None, agents=4, focus=None, json=False, json_output=False))
        assert rc == cli.EXIT_ERR
        assert "не найден" in capsys.readouterr().err

    def test_review_reads_file_and_warns_on_sensitive(self, tmp_path, monkeypatch, capsys) -> None:
        f = tmp_path / ".env.local"
        f.write_text("TOKEN=x", encoding="utf-8")
        mock = AsyncMock(return_value=_ok_result())
        with patch("grok_critic.cli.general_review", new=mock):
            rc = cli.cmd_review(_args(path=str(f), context=None, agents=4, focus=None, json=False, json_output=False))
        assert rc == cli.EXIT_OK
        assert "секретов" in capsys.readouterr().err
        assert mock.call_args.kwargs["content"] == "TOKEN=x"

    def test_review_error_result(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("code"))
        with patch("grok_critic.cli.general_review", new_callable=AsyncMock, return_value=_ok_result(error="Превышен дневной бюджет")):
            rc = cli.cmd_review(_args(path="-", context=None, agents=4, focus=None, json=False, json_output=False))
        assert rc == cli.EXIT_ERR
        assert "бюджет" in capsys.readouterr().err

    def test_review_json_payload(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("code"))
        with patch("grok_critic.cli.general_review", new_callable=AsyncMock, return_value=_ok_result()):
            rc = cli.cmd_review(_args(path="-", context=None, agents=4, focus=None, json=True, json_output=False))
        assert rc == cli.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is True
        assert payload["review_id"] == "rev_cli0001"
        assert payload["total_tokens"] == 150


# END_BLOCK_REVIEW_TESTS


# START_BLOCK_FOLLOWUP_TESTS
class TestFollowupCommand:
    def test_requires_source(self, capsys) -> None:
        rc = cli.cmd_followup(_args(question="why?", review_id=None, from_source=None, agents=None, json=False))
        assert rc == cli.EXIT_ERR
        assert "review-id" in capsys.readouterr().err

    def test_rejects_both_sources(self, tmp_path, capsys) -> None:
        src = tmp_path / "rev.txt"
        src.write_text("review", encoding="utf-8")
        rc = cli.cmd_followup(_args(question="why?", review_id="rev_1", from_source=str(src), agents=None, json=False))
        assert rc == cli.EXIT_ERR
        assert "одно" in capsys.readouterr().err

    def test_by_review_id(self, capsys) -> None:
        review_store.save("rev_seed1", [{"role": "user", "content": "q"}], "past answer")
        mock = AsyncMock(return_value=_ok_result(text="clarified", review_id="rev_new1"))
        with patch("grok_critic.cli.critic_followup_fn", new=mock):
            rc = cli.cmd_followup(_args(question="подробнее?", review_id="rev_seed1", from_source=None, agents=4, json=False))
        assert rc == cli.EXIT_OK
        kwargs = mock.call_args.kwargs
        assert kwargs["review_id"] == "rev_seed1"
        assert kwargs["question"] == "подробнее?"
        assert "clarified" in capsys.readouterr().out

    def test_by_unknown_review_id(self, capsys) -> None:
        rc = cli.cmd_followup(_args(question="why?", review_id="rev_ghost", from_source=None, agents=None, json=False))
        assert rc == cli.EXIT_ERR
        assert "не найден" in capsys.readouterr().err

    def test_from_file(self, tmp_path, capsys) -> None:
        src = tmp_path / "review.md"
        src.write_text("Previous full review", encoding="utf-8")
        mock = AsyncMock(return_value=_ok_result())
        with patch("grok_critic.cli.critic_followup_fn", new=mock):
            rc = cli.cmd_followup(_args(question="why?", review_id=None, from_source=str(src), agents=None, json=False))
        assert rc == cli.EXIT_OK
        assert mock.call_args.kwargs["previous_review"] == "Previous full review"

    def test_error_result(self, tmp_path, capsys) -> None:
        src = tmp_path / "review.md"
        src.write_text("review", encoding="utf-8")
        with patch(
            "grok_critic.cli.critic_followup_fn",
            new_callable=AsyncMock,
            return_value=_ok_result(text="", error="Пустой вопрос"),
        ):
            rc = cli.cmd_followup(_args(question="  ", review_id=None, from_source=str(src), agents=None, json=False))
        assert rc == cli.EXIT_ERR


# END_BLOCK_FOLLOWUP_TESTS


# START_BLOCK_LOGS_CONFIG_DOCTOR
class TestLogsCommand:
    def test_no_log_file_warns(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.config, "log_file", "")
        rc = cli.cmd_logs(_args(tail=50))
        assert rc == cli.EXIT_WARN

    def test_tail_last_lines(self, tmp_path, monkeypatch, capsys) -> None:
        log = tmp_path / "gc.log"
        log.write_text("\n".join(f"line{i}" for i in range(10)), encoding="utf-8")
        monkeypatch.setattr(cli.config, "log_file", str(log))
        rc = cli.cmd_logs(_args(tail=3))
        out = capsys.readouterr().out
        assert rc == cli.EXIT_OK
        assert out.splitlines() == ["line7", "line8", "line9"]

    def test_missing_file(self, tmp_path, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.config, "log_file", str(tmp_path / "absent.log"))
        rc = cli.cmd_logs(_args(tail=5))
        assert rc == cli.EXIT_ERR


class TestConfigCommand:
    def test_masks_key(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.config, "api_key", __import__("pydantic").SecretStr("pza_supersecretkey"))
        rc = cli.cmd_config(_args(json=False))
        out = capsys.readouterr().out
        assert rc == cli.EXIT_OK
        assert "pza_supersecretkey" not in out
        assert "tkey" in out  # последние 4 символа видимы
        assert "store_path" in out

    def test_json_payload(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(cli.config, "api_key", __import__("pydantic").SecretStr("pza_k1"))
        rc = cli.cmd_config(_args(json=True))
        payload = json.loads(capsys.readouterr().out)
        assert rc == cli.EXIT_OK
        assert payload["model"]
        assert "entries" not in payload  # это конфиг, не дамп store


class TestDoctorCommand:
    def test_doctor_prints_checklist(self, capsys) -> None:
        rc = cli.cmd_doctor(_args())
        out = capsys.readouterr().out
        assert rc in (cli.EXIT_OK, cli.EXIT_ERR)  # сеть может быть недоступна в песочнице
        assert "grok-critic doctor:" in out
        assert "POLZA_API_KEY" in out
        assert "Store диалогов" in out
        assert "новая сессия" in out  # подсказка про кэш схемы MCP


class TestParser:
    def test_default_is_serve(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_review_accepts_dash_stdin(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["review", "-", "--agents", "4"])
        assert args.path == "-"
        assert args.agents == 4

    def test_followup_positional_question(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["followup", "почему так?", "--review-id", "rev_x"])
        assert args.question == "почему так?"
        assert args.review_id == "rev_x"


# END_BLOCK_LOGS_CONFIG_DOCTOR
