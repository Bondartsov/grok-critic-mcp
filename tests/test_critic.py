# FILE: tests/test_critic.py
# VERSION: 1.11.2
# START_MODULE_CONTRACT
#   PURPOSE: Tests for M-CRITIC prompt building, review logic, followup, health_check
#   SCOPE: _build_user_prompt, general_review, followup (+review_id), ReviewStore,
#          injection guard, JSON mode, health_check
#   DEPENDS: M-CRITIC, M-CONFIG, M-API
#   LINKS: M-CRITIC
# END_MODULE_CONTRACT

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from grok_critic.api_client import CritiqueResult
from grok_critic.config import config
from grok_critic.critic import (
    ARCHITECTURE_SYSTEM_PROMPT,
    CRITIC_SYSTEM_PROMPT,
    FOLLOWUP_SYSTEM_PROMPT,
    INJECTION_GUARD,
    JSON_OUTPUT_INSTRUCTION,
    SECURITY_SYSTEM_PROMPT,
    ReviewStore,
    _build_user_prompt,
    _code_fence,
    do_architecture_review,
    do_security_audit,
    followup,
    general_review,
    health_check,
    review_store,
)


# START_BLOCK_STORE_ISOLATION
@pytest.fixture(autouse=True)
def _isolated_review_store(tmp_path, monkeypatch):
    """FEAT-CLI: store дисковый (директория per-id файлов) — изолируем в tmp_path."""
    monkeypatch.setattr(review_store, "_dir", tmp_path / "reviews")
    yield


# END_BLOCK_STORE_ISOLATION


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

    def test_fence_survives_backticks_in_content(self) -> None:
        """SEC-INJECTION: контент с ``` не должен ломать markdown-ограду промпта."""
        content = "пример\n```\nзловредный блок\n```\nконец"
        result = _build_user_prompt(content)
        # Ограда длиннее самого длинного забора в контенте (4+ backticks)
        assert "````\n" in result
        fence_count = result.count("````")
        assert fence_count == 2  # открывающая и закрывающая

    def test_code_fence_helper(self) -> None:
        assert _code_fence("plain code") == "```"
        assert _code_fence("has ``` inside") == "````"
        assert _code_fence("````` extreme") == "``````"


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


# START_BLOCK_INJECTION_GUARD
class TestInjectionGuard:
    """SEC-INJECTION: guard добавляется к каждому system-промпту."""

    async def test_general_review_appends_guard(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=4, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await general_review("code")
        system_msg = call_mock.call_args.kwargs["messages"][0]
        assert INJECTION_GUARD in system_msg["content"]
        assert system_msg["content"].startswith(CRITIC_SYSTEM_PROMPT)

    async def test_architecture_review_appends_guard(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=4, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await do_architecture_review("diagram")
        system_msg = call_mock.call_args.kwargs["messages"][0]
        assert INJECTION_GUARD in system_msg["content"]
        assert system_msg["content"].startswith(ARCHITECTURE_SYSTEM_PROMPT)

    async def test_followup_appends_guard(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="answer", model="m", agent_count=4, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await followup("prev review", "question")
        system_msg = call_mock.call_args.kwargs["messages"][0]
        assert INJECTION_GUARD in system_msg["content"]
        assert system_msg["content"].startswith(FOLLOWUP_SYSTEM_PROMPT)


# END_BLOCK_INJECTION_GUARD


# START_BLOCK_GENERAL_REVIEW
class TestGeneralReview:
    async def test_empty_content_returns_error(self) -> None:
        result = await general_review("")
        assert not result.success
        assert "Пустой контент" in result.error

    async def test_whitespace_only_returns_error(self) -> None:
        result = await general_review("   \n\t  ")
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
            result = await general_review("def foo(): pass")
            assert result.success is True
            assert result.text == "Found 3 issues"

    async def test_passes_focus_areas(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=8, effort="medium", review_id="rev_123"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await general_review(
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
            result = await general_review("code")
            assert isinstance(result, CritiqueResult)
            assert result.input_tokens == 100
            assert result.review_id == "rev_abc123"

    async def test_success_populates_review_store(self) -> None:
        mock_result = CritiqueResult(
            text="review body", model="m", agent_count=4, effort="low", review_id="rev_store1"
        )
        with patch("grok_critic.critic.ResponsesClient.call", new_callable=AsyncMock, return_value=mock_result):
            await general_review("some code")
        conversation = review_store.load("rev_store1")
        assert conversation is not None
        roles = [m["role"] for m in conversation]
        assert roles == ["system", "user", "assistant"]

    async def test_json_mode_adds_instruction(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=4, effort="low", review_id="rev_j"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await general_review("code", output_format="json")
        system_msg = call_mock.call_args.kwargs["messages"][0]["content"]
        assert JSON_OUTPUT_INSTRUCTION in system_msg

    async def test_text_mode_no_json_instruction(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="ok", model="m", agent_count=4, effort="low", review_id="rev_t"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await general_review("code", output_format="text")
        system_msg = call_mock.call_args.kwargs["messages"][0]["content"]
        assert JSON_OUTPUT_INSTRUCTION not in system_msg

    async def test_unknown_output_format_rejected(self) -> None:
        result = await general_review("code", output_format="xml")
        assert not result.success
        assert "output_format" in result.error


# END_BLOCK_GENERAL_REVIEW


# START_BLOCK_REVIEW_STORE
class TestReviewStore:
    """ReviewStore — пер-файловый (db/reviews/rev_*.json), без общих мутируемых файлов."""

    def test_save_and_load(self, tmp_path) -> None:
        store = ReviewStore(max_entries=3, path=tmp_path / "reviews")
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        store.save("rev_1", messages, "assistant answer")
        loaded = store.load("rev_1")
        assert loaded is not None
        assert len(loaded) == 3
        assert loaded[-1] == {"role": "assistant", "content": "assistant answer"}

    def test_one_file_per_review_id(self, tmp_path) -> None:
        """Каждый review_id — отдельный файл: нет общего мутируемого JSON (анти-race)."""
        store = ReviewStore(path=tmp_path / "reviews")
        store.save("rev_a1", [{"role": "user", "content": "1"}], "a1")
        store.save("rev_b2", [{"role": "user", "content": "2"}], "b2")
        files = sorted(p.name for p in (tmp_path / "reviews").glob("rev_*.json"))
        assert files == ["rev_a1.json", "rev_b2.json"]

    def test_load_missing_returns_none(self, tmp_path) -> None:
        store = ReviewStore(path=tmp_path / "reviews")
        assert store.load("rev_missing") is None

    def test_empty_answer_not_saved(self, tmp_path) -> None:
        store = ReviewStore(path=tmp_path / "reviews")
        store.save("rev_1", [{"role": "user", "content": "u"}], "   ")
        assert store.load("rev_1") is None

    def test_prune_oldest_beyond_limit(self, tmp_path) -> None:
        store = ReviewStore(max_entries=2, path=tmp_path / "reviews")
        import time as _t

        for i in range(3):
            store.save(f"rev_{i}", [{"role": "user", "content": str(i)}], f"a{i}")
            _t.sleep(0.01)  # различные ts для детерминированного prune
        assert store.load("rev_0") is None      # самый старый вытеснен
        assert store.load("rev_1") is not None
        assert store.load("rev_2") is not None

    def test_touch_refreshes_recency(self, tmp_path) -> None:
        store = ReviewStore(max_entries=2, path=tmp_path / "reviews")
        store.save("rev_a", [], "a")
        store.save("rev_b", [], "b")
        store.load("rev_a")                      # rev_a снова свежая
        store.save("rev_c", [], "c")             # вытеснится rev_b
        assert store.load("rev_a") is not None
        assert store.load("rev_b") is None

    def test_persists_across_instances(self, tmp_path) -> None:
        """FEAT-CLI: review_id переживает 'рестарт' (новый инстанс/процесс)."""
        path = tmp_path / "db" / "reviews"
        store1 = ReviewStore(path=path)
        store1.save("rev_persist", [{"role": "user", "content": "q"}], "answer")
        store2 = ReviewStore(path=path)
        loaded = store2.load("rev_persist")
        assert loaded is not None
        assert loaded[-1]["content"] == "answer"

    def test_two_writers_do_not_lose_entries(self, tmp_path) -> None:
        """REGRESSION rev_4e2fb8bca326: два процесса пишут разные записи —
        пер-файловый layout исключает last-writer-wins потерю чужой записи."""
        path = tmp_path / "reviews"
        writer_a = ReviewStore(path=path)
        writer_b = ReviewStore(path=path)
        writer_a.save("rev_from_a", [{"role": "user", "content": "a"}], "from a")
        writer_b.save("rev_from_b", [{"role": "user", "content": "b"}], "from b")
        # writer_a больше НЕ перезаписывает файл целиком — запись B на месте
        assert writer_a.load("rev_from_b") is not None
        assert writer_b.load("rev_from_a") is not None

    def test_corrupted_entry_tolerated(self, tmp_path) -> None:
        """Битый файл одной записи не роняет store и не мешает другим записям."""
        path = tmp_path / "reviews"
        store = ReviewStore(path=path)
        store.save("rev_ok", [{"role": "user", "content": "u"}], "a")
        (path / "rev_bad.json").write_text("{broken", encoding="utf-8")
        fresh = ReviewStore(path=path)
        assert fresh.load("rev_bad") is None
        assert fresh.load("rev_ok") is not None

    @pytest.mark.skipif(os.name != "posix", reason="NEW-SEC-store-perms: права POSIX-only")
    def test_store_dir_and_file_perms_posix(self, tmp_path) -> None:
        """NEW-SEC-store-perms: каталог store — 0700, записанный файл — 0600."""
        path = tmp_path / "reviews"
        store = ReviewStore(path=path)
        store.save("rev_perm", [{"role": "user", "content": "u"}], "a")
        dir_mode = path.stat().st_mode & 0o777
        file_mode = (path / "rev_perm.json").stat().st_mode & 0o777
        assert dir_mode == 0o700
        assert file_mode == 0o600

    def test_save_and_load_works_on_current_platform(self, tmp_path) -> None:
        """NEW-SEC-store-perms: chmod (POSIX) не ломает обычную запись/чтение ни на одной платформе."""
        store = ReviewStore(path=tmp_path / "reviews")
        store.save("rev_cross_platform", [{"role": "user", "content": "u"}], "answer")
        loaded = store.load("rev_cross_platform")
        assert loaded is not None
        assert loaded[-1]["content"] == "answer"

    def test_ttl_expiry(self, tmp_path) -> None:
        """Записи старше 24ч удаляются при обращении/чистке."""
        import time as _time

        from grok_critic.critic import STORE_TTL_SECONDS

        path = tmp_path / "reviews"
        store = ReviewStore(path=path)
        store.save("rev_old", [{"role": "user", "content": "u"}], "a")
        store.save("rev_fresh", [{"role": "user", "content": "u"}], "b")
        # состарим rev_old прямо в файле
        old_path = path / "rev_old.json"
        entry = json.loads(old_path.read_text(encoding="utf-8"))
        entry["ts"] = _time.time() - STORE_TTL_SECONDS - 10
        old_path.write_text(json.dumps(entry), encoding="utf-8")
        fresh = ReviewStore(path=path)
        assert fresh.load("rev_old") is None
        assert fresh.load("rev_fresh") is not None

    def test_tmp_file_opened_with_excl_and_0600(self, tmp_path) -> None:
        """STORE-TMP-0600: на POSIX tmp создаётся через os.open(O_EXCL, 0o600) —
        без промежуточного write_text() + chmod() (окно с широкими правами по umask).
        Мокаем os.name, чтобы тест был воспроизводим независимо от платформы CI."""
        path = tmp_path / "reviews"
        store = ReviewStore(path=path)
        real_open = os.open
        opened: list[tuple[str, int, int]] = []

        def fake_open(file, flags, mode=0o777):
            opened.append((file, flags, mode))
            return real_open(file, flags, mode)

        with patch("grok_critic.critic.os.name", "posix"), \
                patch("grok_critic.critic.os.chmod"), \
                patch("grok_critic.critic.os.open", side_effect=fake_open):
            store.save("rev_excl", [{"role": "user", "content": "u"}], "a")

        assert len(opened) == 1
        _file, flags, mode = opened[0]
        assert flags & os.O_EXCL
        assert flags & os.O_CREAT
        assert mode == 0o600
        # финальный файл всё равно на месте и читаем
        assert store.load("rev_excl") is not None

    def test_two_writers_same_review_id_unique_tmp_names(self, tmp_path) -> None:
        """STORE-TMP-UNIQUE: два «писателя» (разные процессы/инстансы) сохраняют
        ОДИН и тот же review_id подряд — уникальные tmp-имена не сталкиваются,
        итоговый JSON корректен и содержит последнюю запись."""
        path = tmp_path / "reviews"
        writer_a = ReviewStore(path=path)
        writer_b = ReviewStore(path=path)

        writer_a.save("rev_shared", [{"role": "user", "content": "q1"}], "answer from a")
        writer_b.save("rev_shared", [{"role": "user", "content": "q2"}], "answer from b")

        # никаких осиротевших .tmp-файлов не осталось
        assert list(path.glob("*.tmp")) == []
        loaded = writer_a.load("rev_shared")
        assert loaded is not None
        assert loaded[-1] == {"role": "assistant", "content": "answer from b"}

    def test_tmp_name_unique_per_call(self, tmp_path) -> None:
        """Прямая проверка уникальности имени tmp-файла между двумя вызовами
        _atomic_write для одного и того же целевого path (без реальной гонки потоков)."""
        path = tmp_path / "reviews"
        store = ReviewStore(path=path)
        target = path / "rev_x.json"
        seen: list[str] = []
        real_replace = os.replace

        def fake_replace(src, dst):
            seen.append(str(src))
            real_replace(src, dst)

        with patch("grok_critic.critic.os.replace", side_effect=fake_replace):
            store._atomic_write(target, json.dumps({"ts": 1, "messages": []}))
            store._atomic_write(target, json.dumps({"ts": 2, "messages": []}))

        assert len(seen) == 2
        assert seen[0] != seen[1]

    def test_orphaned_tmp_file_does_not_break_load_or_prune(self, tmp_path) -> None:
        """Осиротевший .tmp-файл (например, после аварийного завершения писателя
        между os.open и os.replace) не мешает load() существующих записей и не
        считается записью в _prune (glob только rev_*.json)."""
        path = tmp_path / "reviews"
        store = ReviewStore(max_entries=5, path=path)
        store.save("rev_ok", [{"role": "user", "content": "u"}], "a")

        orphan = path / ".rev_ok.json.99999.deadbeef.tmp"
        orphan.write_text("{not valid json at all", encoding="utf-8")

        assert store.load("rev_ok") is not None
        store._prune()  # не должен упасть и не должен тронуть orphan/rev_ok
        assert orphan.exists()
        assert store.load("rev_ok") is not None


# END_BLOCK_REVIEW_STORE


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

    async def test_both_sources_rejected(self) -> None:
        result = await followup(previous_review="text", question="why?", review_id="rev_x")
        assert not result.success
        assert "что-то одно" in result.error

    async def test_prompt_includes_review_and_question(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="answer", model="m", agent_count=16, effort="high", review_id="rev_1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await followup("Previous review", "Explain more")
            prompt = call_mock.call_args.kwargs["prompt"]
            assert "Previous review" in prompt
            assert "Explain more" in prompt

    async def test_custom_agent_count(self) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="answer", model="m", agent_count=4, effort="low", review_id="rev_1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await followup("review", "question", agent_count=4)
            assert call_mock.call_args.kwargs.get("agent_count") == 4


# END_BLOCK_FOLLOWUP


# START_BLOCK_FOLLOWUP_BY_ID
class TestFollowupById:
    """FEAT-FOLLOWUP-ID: followup по review_id без передачи полного текста ревью."""

    @pytest.fixture()
    def seeded_store(self) -> str:
        conversation = [
            {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
            {"role": "user", "content": "original code prompt"},
        ]
        review_store.save("rev_seed123", conversation, "original critique answer")
        yield "rev_seed123"
        (review_store._dir / "rev_seed123.json").unlink(missing_ok=True)

    async def test_followup_by_review_id_builds_dialogue(self, seeded_store) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="deep answer", model="m", agent_count=16, effort="high", review_id="rev_new1"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            result = await followup(question="А что если event sourcing?", review_id=seeded_store)
            assert result.success
            messages = call_mock.call_args.kwargs["messages"]
            roles = [m["role"] for m in messages]
            assert roles == ["system", "system", "user", "assistant", "user"]
            # оригинальный диалог восстановлен
            assert "original code prompt" in messages[2]["content"]
            assert "original critique answer" in messages[3]["content"]
            # новый вопрос добавлен последним
            assert "event sourcing" in messages[4]["content"]
            assert messages[0]["content"].startswith(FOLLOWUP_SYSTEM_PROMPT)

    async def test_followup_by_unknown_review_id(self) -> None:
        """A4: сообщение про TTL/лимит store, а не про 'теряется при рестарте'
        (store теперь дисковый и переживает рестарт — см. test_persists_across_instances)."""
        result = await followup(question="why?", review_id="rev_nope")
        assert not result.success
        assert "не найден" in result.error
        assert "previous_review" in result.error  # подсказка о fallback
        assert "TTL" in result.error
        assert "рестарте" not in result.error

    async def test_followup_by_id_result_also_stored(self, seeded_store) -> None:
        call_mock = AsyncMock(return_value=CritiqueResult(
            text="chain answer", model="m", agent_count=4, effort="low", review_id="rev_chain9"
        ))
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            await followup(question="ещё вопрос", review_id=seeded_store)
        chained = review_store.load("rev_chain9")
        assert chained is not None
        assert chained[-1]["content"] == "chain answer"


# END_BLOCK_FOLLOWUP_BY_ID


# START_BLOCK_HEALTH_CHECK
class TestHealthCheck:
    @pytest.fixture(autouse=True)
    def _reset_balance_cache(self):
        """A2/TEST-05: TTL-кэш баланса (60с) не должен маскировать ошибки между тестами."""
        import grok_critic.critic as critic_module

        critic_module._balance_cache = None
        yield
        critic_module._balance_cache = None

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

    async def test_usage_today_present(self, mock_balance) -> None:
        """FEAT-BUDGET: health_check отдаёт суточную статистику."""
        with patch("grok_critic.critic.config") as mock_cfg:
            mock_cfg.api_key = SecretStr("valid-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            result = await health_check()
            assert "usage_today" in result
            assert set(result["usage_today"].keys()) == {"calls", "errors", "cost_usd", "cost_rub"}

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

    # -- A2/TEST-05: status пересчитывается ПОСЛЕ блока Balance API -----------

    async def test_degraded_when_balance_api_returns_500(self) -> None:
        """A2: без бага status оставался бы 'ok', хотя Balance API вернул 500."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("grok_critic.critic.httpx.AsyncClient", return_value=mock_client), patch(
            "grok_critic.critic.config"
        ) as mock_cfg:
            mock_cfg.api_key = SecretStr("valid-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            result = await health_check()
            assert result["status"] == "degraded"
            assert any("Balance API" in issue for issue in result["issues"])

    async def test_degraded_when_balance_api_connect_error(self) -> None:
        """A2/TEST-05: транспортная ошибка (httpx.ConnectError) тоже должна давать degraded."""
        import httpx

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("grok_critic.critic.httpx.AsyncClient", return_value=mock_client), patch(
            "grok_critic.critic.config"
        ) as mock_cfg:
            mock_cfg.api_key = SecretStr("valid-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            result = await health_check()
            assert result["status"] == "degraded"
            assert any("Balance API" in issue for issue in result["issues"])

    async def test_ok_when_balance_api_succeeds(self, mock_balance) -> None:
        """A2: успешный ответ Balance API не должен деградировать статус."""
        with patch("grok_critic.critic.config") as mock_cfg:
            mock_cfg.api_key = SecretStr("valid-key")
            mock_cfg.model = "x-ai/grok-4.20-multi-agent"
            mock_cfg.base_url = "https://polza.ai/api/v1"
            mock_cfg.price_input_per_1m = 0.0
            mock_cfg.price_output_per_1m = 0.0
            result = await health_check()
            assert result["status"] == "ok"
            assert result["issues"] == []


# END_BLOCK_HEALTH_CHECK


# START_BLOCK_SIZE_GUARD
class TestContentSizeGuard:
    """TEST-03 / REL-03: единый cost-guard MAX_CONTENT_CHARS для всех путей ревью."""

    async def test_general_review_oversized(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "max_content_chars", 100)
        call_mock = AsyncMock()
        with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
            result = await general_review("x" * 101)
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

    async def test_followup_by_id_guards_only_question(self, monkeypatch) -> None:
        """FEAT-FOLLOWUP-ID: при review_id лимит применяется только к новому вопросу."""
        monkeypatch.setattr(config, "max_content_chars", 100)
        conversation = [
            {"role": "user", "content": "x" * 300},  # оригинал больше лимита — уже оплачен
        ]
        review_store.save("rev_big1", conversation, "y" * 300)
        try:
            call_mock = AsyncMock(return_value=CritiqueResult(
                text="ok", model="m", agent_count=4, effort="low", review_id="rev_big2"
            ))
            with patch("grok_critic.critic.ResponsesClient.call", new=call_mock):
                result = await followup(question="короткий вопрос", review_id="rev_big1")
                assert result.success
                call_mock.assert_called_once()
        finally:
            (review_store._dir / "rev_big1.json").unlink(missing_ok=True)

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
            assert kwargs["messages"][0]["content"].startswith(ARCHITECTURE_SYSTEM_PROMPT)
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
            assert kwargs["messages"][0]["content"].startswith(SECURITY_SYSTEM_PROMPT)
            assert "security" in kwargs["prompt"]
            assert "vulnerabilities" in kwargs["prompt"]

    async def test_architecture_review_empty_content(self) -> None:
        result = await do_architecture_review("   ")
        assert not result.success
        assert "архитектурного ревью" in result.error


# END_BLOCK_SPECIALIZED_REVIEW_TESTS
