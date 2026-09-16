# FILE: tests/test_migrate_env.py
# VERSION: 1.12.0
# START_MODULE_CONTRACT
#   PURPOSE: Tests for scripts/migrate_env_1_12.py — миграция .env на 1.12.0
#   SCOPE: паритет с python-dotenv (многострочные значения, CR/CRLF, ключи в кавычках, BOM, не-ASCII),
#          удаление устаревших ключей; комментарии: в .env не удаляются, в шаблоне — только описания цен;
#          бюджет в ₽ для шаблона; пост-условие по значениям (окружение, ${...}, регистр pydantic, raw);
#          сохранение байтов/метаданных (DACL при создании, атрибуты, права), атомарная запись, сбои и
#          повторы, цели по умолчанию, exit codes, отсутствие утечки значений, дифференциальный fuzz
#   DEPENDS: scripts/migrate_env_1_12.py, python-dotenv
#   LINKS: M-CONFIG
# END_MODULE_CONTRACT

from __future__ import annotations

import importlib.util
import io
import os
import random
import stat
import sys
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_env_1_12.py"
_spec = importlib.util.spec_from_file_location("migrate_env_1_12", _SCRIPT)
assert _spec is not None and _spec.loader is not None
migrate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = migrate  # dataclasses ищут модуль в sys.modules
_spec.loader.exec_module(migrate)

SECRET = "sk-FAKE-SECRET-VALUE-123456"
BOM = "﻿"


def _block(newline: str = "\n", *, commented: bool = False, indent: str = "", export: str = "") -> str:
    key_line = ("# " if commented else f"{indent}{export}") + "POLZA_DAILY_BUDGET_RUB=0"
    return newline.join((*(indent + line for line in migrate.BUDGET_RUB_DESCRIPTION), key_line)) + newline


def _mig(text: str, *, template: bool = False):
    return migrate.migrate_text(text, template=template)


def _app_values(text: str) -> dict:
    """Независимо от скрипта: как читает приложение — open(encoding='utf-8') + dotenv_values; BOM-ключи не видны."""
    stream = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")
    return {k: v for k, v in dotenv_values(stream=stream).items() if not k.startswith(BOM)}


def _pydantic_view(values: dict) -> dict:
    """Как pydantic-settings (case_sensitive=False): ключи в нижнем регистре, побеждает последний."""
    return {k.lower(): v for k, v in values.items()}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in [*migrate.DEPRECATED_KEYS, "POLZA_DAILY_BUDGET_RUB", "POLZA_ENV_FILE"]:
        monkeypatch.delenv(key, raising=False)


# START_BLOCK_PARSER_PARITY
class TestParserParity:
    @pytest.mark.parametrize(
        "text",
        [
            "A=1\r\nB='x\ny'\rC\n\n# c\n  D = 2 # note\n\t\n",
            '\nPEM="line1\n\nline3"\nPOLZA_DAILY_BUDGET_USD=5',
            "A=1\r\nBROKEN: x\r\nB=2\r\n",
            f"{BOM}# c\nA=1\n",
            "",
            "   ",
        ],
    )
    def test_units_restore_text_bytewise(self, text) -> None:
        units = migrate._parse_units(text)
        assert "".join(u.gap + u.body for u in units) == text

    def test_lone_cr_line_endings(self) -> None:
        new, _ = _mig(f"POLZA_DAILY_BUDGET_USD=5\rPOLZA_API_KEY={SECRET}\n")
        assert new == f"POLZA_API_KEY={SECRET}\n"
        new, _ = _mig(f"POLZA_API_KEY={SECRET}\rPOLZA_DAILY_BUDGET_USD=5\r")
        assert new == f"POLZA_API_KEY={SECRET}\r"

    @pytest.mark.parametrize(
        "text",
        [
            f'POLZA_API_KEY={SECRET}\nPEM="line1\nPOLZA_DAILY_BUDGET_USD=5\nline3"\n',
            "A=1\nPEM='x\n# note about price\nPOLZA_DAILY_BUDGET_USD=5\ny'\n",
            f'NOTE="text\n\nPOLZA_DAILY_BUDGET_USD=5"\nPOLZA_API_KEY={SECRET}\n',
            'NOTE="x\n\nPOLZA_DAILY_BUDGET_USD=1\n\ny"\nA=1\n',
        ],
    )
    def test_deprecated_looking_lines_inside_quoted_value_untouched(self, text) -> None:
        new, report = _mig(text)
        assert new == text
        assert not report.changed and not report.refused

    @pytest.mark.parametrize(
        "text",
        [
            f"POLZA_DAILY_BUDGET_USD='5\n'\nPOLZA_API_KEY={SECRET}\nFOO='bar'\n",
            f'POLZA_PRICE_INPUT_PER_1M="1\nPOLZA_MODEL=evil"\nPOLZA_API_KEY={SECRET}\n',
            f'POLZA_DAILY_BUDGET_USD="5\nPOLZA_API_KEY={SECRET}\nPOLZA_MODEL=x-ai/grok"\n',
        ],
    )
    def test_deprecated_key_with_multiline_value_refused(self, text) -> None:
        """Внутри многострочного значения могут быть данные пользователя — молча не удаляем."""
        new, report = _mig(text)
        assert new == text
        assert "1–2" in report.refused or "1–3" in report.refused
        assert SECRET not in report.refused

    def test_mixed_newlines_and_values_preserved_bytewise(self) -> None:
        new, _ = _mig(f'POLZA_API_KEY={SECRET}\r\nPEM="a\nb"\r\nPOLZA_DAILY_BUDGET_USD=5\r\n')
        assert new == f'POLZA_API_KEY={SECRET}\r\nPEM="a\nb"\r\n'
        new, _ = _mig(f"POLZA_API_KEY={SECRET}\nA=1\r\nPOLZA_DAILY_BUDGET_USD=5\nB=2\n")
        assert new == f"POLZA_API_KEY={SECRET}\nA=1\r\nB=2\n"

    def test_crlf_after_unparseable_line_not_split(self) -> None:
        text = (
            f"POLZA_API_KEY={SECRET}\r\nPOLZA_MODEL: grok\r\n# POLZA_PRICE_INPUT_PER_1M: price per 1M tokens\r\n"
            "POLZA_PRICE_INPUT_PER_1M=0.2\r\n\r\nPOLZA_TIMEOUT_SECONDS=180\r\n"
        )
        new, report = _mig(text)
        assert new == (
            f"POLZA_API_KEY={SECRET}\r\nPOLZA_MODEL: grok\r\n# POLZA_PRICE_INPUT_PER_1M: price per 1M tokens\r\n"
            "\r\nPOLZA_TIMEOUT_SECONDS=180\r\n"
        )
        assert report.removed_keys == [("POLZA_PRICE_INPUT_PER_1M", 4)]
        assert report.review_lines_before == [3]
        new, report = _mig(f"POLZA_API_KEY={SECRET}\r\nPOLZA_MODEL: grok\r\nPOLZA_DAILY_BUDGET_USD=5\r\n")
        assert new == f"POLZA_API_KEY={SECRET}\r\nPOLZA_MODEL: grok\r\n"
        assert report.removed_keys == [("POLZA_DAILY_BUDGET_USD", 3)]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("'POLZA_PRICE_INPUT_PER_1M'=1\nA=1\n", "A=1\n"),
            ("POLZA_DAILY_BUDGET_USD\nA=1\n", "A=1\n"),
            ("export POLZA_DAILY_BUDGET_USD\nA=1\n", "A=1\n"),
            ("\tpolza_daily_budget_usd\n", ""),
            ("export polza_price_output_per_1m = 6.0 # old\nA=1\n", "A=1\n"),
        ],
    )
    def test_quoted_bare_exported_lowercase_keys(self, text, expected) -> None:
        new, report = _mig(text)
        assert new == expected
        assert report.changed

    @pytest.mark.parametrize("key", ["POLZA_PRICE_ıNPUT_PER_1M", "POLZA_DAILY_BUDGET_UſD"])
    def test_non_ascii_lookalike_keys_untouched(self, key) -> None:
        text = f"POLZA_API_KEY={SECRET}\n{key}=keep-me\n"
        new, report = _mig(text)
        assert new == text and not report.changed

    def test_error_line_that_looks_deprecated_is_left_for_review(self) -> None:
        text = 'A=1\nPOLZA_DAILY_BUDGET_USD="unterminated\n'
        new, report = _mig(text)
        assert new == text
        assert not report.changed
        assert report.review_lines_before == [2]


class TestBom:
    """BOM не снимается: приложение читает его как часть первого ключа — и скрипт тоже."""

    def test_bom_line_never_removed_but_flagged(self) -> None:
        new, report = _mig(f"{BOM}# POLZA_DAILY_BUDGET_USD=5\nPOLZA_API_KEY={SECRET}\nPOLZA_PRICE_INPUT_PER_1M=1\n")
        assert new == f"{BOM}# POLZA_DAILY_BUDGET_USD=5\nPOLZA_API_KEY={SECRET}\n"
        assert report.review_lines_before == [1]

    def test_bom_glued_deprecated_key_left_alone(self) -> None:
        text = f"{BOM}POLZA_DAILY_BUDGET_USD=5\nPOLZA_API_KEY={SECRET}\n"
        new, report = _mig(text)
        assert new == text and not report.changed
        assert report.review_lines_before == [1]

    @pytest.mark.parametrize("first", ["POLZA_AGENT_COUNT=999", "POLZA_MODEL=other/model"])
    def test_ignored_first_key_stays_ignored(self, first) -> None:
        new, _ = _mig(f"{BOM}{first}\nPOLZA_API_KEY={SECRET}\nPOLZA_DAILY_BUDGET_USD=5\n")
        assert new == f"{BOM}{first}\nPOLZA_API_KEY={SECRET}\n"
        assert first.split("=")[0] not in _app_values(new)

    def test_case_duplicates_keep_pydantic_winner(self) -> None:
        text = f"{BOM}POLZA_API_KEY=sk-FAKE-A\npolza_api_key=sk-FAKE-B\nPOLZA_API_KEY=sk-FAKE-C\nPOLZA_DAILY_BUDGET_USD=5\n"
        new, report = _mig(text)
        assert not report.refused
        expected = {k: v for k, v in _app_values(text).items() if not migrate.is_deprecated_name(k)}
        assert _pydantic_view(_app_values(new)) == _pydantic_view(expected)

    def test_kept_when_nothing_changes(self) -> None:
        new, report = _mig(f"{BOM}A=1\n")
        assert new == f"{BOM}A=1\n" and not report.changed


# END_BLOCK_PARSER_PARITY


# START_BLOCK_GUARD
class TestPostConditionGuard:
    @pytest.mark.parametrize(
        "reference", ["${POLZA_DAILY_BUDGET_USD}", "${polza_daily_budget_usd:-5}", "${ POLZA_PRICE_INPUT_PER_1M}"],
    )
    def test_reference_to_deprecated_key_refused(self, reference) -> None:
        text = f"X=1\nPOLZA_DAILY_BUDGET_USD=5\nPOLZA_PRICE_INPUT_PER_1M=5\nA=budget-{reference}\n"
        new, report = _mig(text)
        assert new == text
        assert "4" in report.refused and "budget-" not in report.refused

    def test_script_environment_does_not_mask_lost_interpolation(self, monkeypatch) -> None:
        monkeypatch.setenv("POLZA_DAILY_BUDGET_USD", "5")
        text = f"POLZA_API_KEY={SECRET}\nPOLZA_DAILY_BUDGET_USD=5\nPOLZA_TIMEOUT_SECONDS=${{POLZA_DAILY_BUDGET_USD}}00\n"
        new, report = _mig(text)
        assert new == text and report.refused
        assert os.environ["POLZA_DAILY_BUDGET_USD"] == "5"  # окружение восстановлено

    def test_guard_blocks_any_unexpected_change(self, monkeypatch) -> None:
        original = migrate._mark_descriptions

        def buggy(units, paragraphs, report, *, template) -> None:
            original(units, paragraphs, report, template=template)
            next(u for u in units if u.kind == migrate.CONTENT).removed = True  # «баг» эвристики

        monkeypatch.setattr(migrate, "_mark_descriptions", buggy)
        text = f"POLZA_API_KEY={SECRET}\nPOLZA_DAILY_BUDGET_USD=5\n"
        new, report = _mig(text)
        assert new == text
        assert report.refused and SECRET not in report.refused

    def test_guard_checks_case_insensitive_view(self, monkeypatch) -> None:
        """Перестановка регистр-дублей не видна в dict ==, но меняет значение для pydantic — отказ."""
        original = migrate._render

        def reorder(units, paragraphs, report) -> str:
            text = original(units, paragraphs, report)
            lines = text.splitlines(keepends=True)
            return "".join([lines[1], lines[0], *lines[2:]])

        monkeypatch.setattr(migrate, "_render", reorder)
        new, report = _mig("POLZA_MODEL=a\npolza_model=b\nPOLZA_DAILY_BUDGET_USD=5\n")
        assert report.refused and new.startswith("POLZA_MODEL=a")


# END_BLOCK_GUARD


# START_BLOCK_DESCRIPTIONS
class TestDescriptions:
    _PRICES = (
        f"POLZA_API_KEY={SECRET}\n\n# Цены за 1M токенов (USD)\nPOLZA_PRICE_INPUT_PER_1M=2.0\n"
        "POLZA_PRICE_OUTPUT_PER_1M=6.0\n\nPOLZA_MODEL=x-ai/grok-4.20-multi-agent\n"
    )

    def test_real_env_keeps_description_for_review(self) -> None:
        new, report = _mig(self._PRICES)
        assert new == f"POLZA_API_KEY={SECRET}\n\n# Цены за 1M токенов (USD)\n\nPOLZA_MODEL=x-ai/grok-4.20-multi-agent\n"
        assert report.removed_keys == [("POLZA_PRICE_INPUT_PER_1M", 4), ("POLZA_PRICE_OUTPUT_PER_1M", 5)]
        assert report.removed_comment_lines == []
        assert report.review_lines_before == [3] and report.review_lines_after == [3]

    def test_template_removes_price_description(self) -> None:
        new, report = _mig(self._PRICES, template=True)
        assert new == f"POLZA_API_KEY={SECRET}\n\nPOLZA_MODEL=x-ai/grok-4.20-multi-agent\n\n" + _block()
        assert report.removed_comment_lines == [3]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("POLZA_DAILY_BUDGET_USD=5\n\nPOLZA_MODEL=m\n", "POLZA_MODEL=m\n"),
            ("POLZA_MODEL=m\n\nPOLZA_DAILY_BUDGET_USD=5\n", "POLZA_MODEL=m\n"),
            ("POLZA_DAILY_BUDGET_USD=5\n", ""),
            ("A=1\n\n\nPOLZA_DAILY_BUDGET_USD=5\n\n\nB=2\n", "A=1\n\n\nB=2\n"),
            ("\nPOLZA_DAILY_BUDGET_USD=5\n\nB=2\n", "\nB=2\n"),
            ("A=1\nPOLZA_DAILY_BUDGET_USD=1", "A=1"),
        ],
        ids=["first", "last", "only", "multi-blank", "leading-blank", "no-trailing-newline"],
    )
    def test_paragraph_separators(self, text, expected) -> None:
        new, _ = _mig(text)
        assert new == expected

    _LAYOUT = (
        "# Модель и цены\nPOLZA_PRICE_INPUT_PER_1M=2.0\nPOLZA_MODEL=m\n# цена выхода\n"
        "POLZA_PRICE_OUTPUT_PER_1M=6.0\n# число агентов\nPOLZA_AGENT_COUNT=4\n"
    )

    def test_real_env_layout(self) -> None:
        new, report = _mig(self._LAYOUT)
        assert new == "# Модель и цены\nPOLZA_MODEL=m\n# цена выхода\n# число агентов\nPOLZA_AGENT_COUNT=4\n"
        assert report.removed_comment_lines == []
        assert report.review_lines_before == [1, 4]
        assert report.review_lines_after == [1, 3]

    def test_template_layout_removes_description_when_next_key_has_its_own(self) -> None:
        new, report = _mig(self._LAYOUT, template=True)
        assert new == "# Модель и цены\nPOLZA_MODEL=m\n# число агентов\nPOLZA_AGENT_COUNT=4\n\n" + _block()
        assert report.removed_comment_lines == [4]
        assert report.review_lines_before == [1]

    @pytest.mark.parametrize("template", [False, True])
    def test_comment_shared_across_commented_key_kept(self, template) -> None:
        text = (
            f"POLZA_API_KEY={SECRET}\n\n# Limits: daily budget and retry count\nPOLZA_DAILY_BUDGET_USD=5\n"
            "# POLZA_MAX_RETRIES=2\nPOLZA_MAX_RETRIES=3\n"
        )
        new, report = _mig(text, template=template)
        rub = _block() if template else ""  # в шаблоне бюджет в ₽ встаёт на место долларового
        assert new == (
            f"POLZA_API_KEY={SECRET}\n\n# Limits: daily budget and retry count\n{rub}# POLZA_MAX_RETRIES=2\n"
            "POLZA_MAX_RETRIES=3\n"
        )
        assert report.removed_comment_lines == []

    @pytest.mark.parametrize("template", [False, True])
    def test_personal_notes_above_deprecated_key_never_deleted(self, template) -> None:
        notes = "# previous token (rotated 01.09.2026): sk-FAKE-OLD-123\n# db password: pa$$w0rd-FAKE\n"
        text = f"POLZA_API_KEY=sk-FAKE-new\nPOLZA_MODEL=x-ai/grok\n{notes}POLZA_DAILY_BUDGET_USD=5\n"
        new, report = _mig(text, template=template)
        assert notes in new
        assert report.removed_comment_lines == []

    def test_template_unrelated_note_kept_price_line_removed(self) -> None:
        text = f"POLZA_API_KEY={SECRET}\n\n# backup key: sk-FAKE-OLD\n# цена за 1M токенов\nPOLZA_DAILY_BUDGET_USD=5\n\nB=2\n"
        new, report = _mig(text, template=True)
        assert new == f"POLZA_API_KEY={SECRET}\n\n# backup key: sk-FAKE-OLD\n{_block()}\nB=2\n"
        assert report.removed_comment_lines == [4]

    def test_commented_live_key_is_not_a_description(self) -> None:
        new, _ = _mig("# POLZA_LOG_FILE=app.log\nPOLZA_DAILY_BUDGET_USD=5\n", template=True)
        assert new == "# POLZA_LOG_FILE=app.log\n" + _block()

    def test_no_deprecated_keys_no_change(self) -> None:
        text = f"POLZA_API_KEY={SECRET}\n\n# комментарий\nPOLZA_MODEL=m\n"
        new, report = _mig(text)
        assert new == text and not report.changed

    def test_stale_comment_elsewhere_reported_not_removed(self) -> None:
        text = "POLZA_MODEL=m\n\n# Стоимость ~$0.25 за вызов\nPOLZA_AGENT_COUNT=4\n"
        new, report = _mig(text)
        assert new == text and not report.changed
        assert report.review_lines_before == [3]

    def test_review_line_numbers_shift_after_migration(self) -> None:
        new, report = _mig("POLZA_DAILY_BUDGET_USD=5\n\n# осталось упоминание USD\nPOLZA_MODEL=m\n")
        assert new == "# осталось упоминание USD\nPOLZA_MODEL=m\n"
        assert report.review_lines_before == [3]
        assert report.review_lines_after == [1]

    def test_alternating_keys_and_comments_is_linear(self) -> None:
        text = "POLZA_PRICE_INPUT_PER_1M=1\n# note\n" * 8000
        started = time.perf_counter()
        _, report = _mig(text, template=True)
        assert len(report.removed_keys) == 8000
        assert time.perf_counter() - started < 15


# END_BLOCK_DESCRIPTIONS


# START_BLOCK_TEMPLATE
class TestTemplateBudget:
    def test_usd_paragraph_replaced_in_place(self) -> None:
        text = (
            "POLZA_MODEL=m\n\n# Дневной бюджет в USD (0 = без лимита)\nPOLZA_DAILY_BUDGET_USD=0\n\n"
            "POLZA_LOG_LEVEL=WARNING\n"
        )
        new, report = _mig(text, template=True)
        assert new == f"POLZA_MODEL=m\n\n{_block()}\nPOLZA_LOG_LEVEL=WARNING\n"
        assert report.added_budget_rub == "active"
        assert _app_values(new)["POLZA_DAILY_BUDGET_RUB"] == "0"

    @pytest.mark.parametrize("newline", ["\r\n", "\r"])
    def test_block_uses_file_newline_style(self, newline) -> None:
        new, _ = _mig(f"POLZA_MODEL=m{newline}POLZA_DAILY_BUDGET_USD=0{newline}", template=True)
        assert new == f"POLZA_MODEL=m{newline}" + _block(newline)

    def test_block_keeps_indent_and_export(self) -> None:
        text = "export POLZA_API_KEY=\n    export POLZA_DAILY_BUDGET_USD=5\nexport POLZA_MODEL=x\n"
        new, _ = _mig(text, template=True)
        assert new == "export POLZA_API_KEY=\n" + _block(indent="    ", export="export ") + "export POLZA_MODEL=x\n"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("POLZA_MODEL=m\n", "POLZA_MODEL=m\n\n"), ("POLZA_MODEL=m", "POLZA_MODEL=m\n\n"), ("", ""), ("A=1\n\n", "A=1\n\n")],
    )
    def test_appended_when_no_usd_budget(self, text, expected) -> None:
        new, report = _mig(text, template=True)
        assert new == expected + _block()
        assert report.added_budget_rub == "active"

    def test_commented_usd_becomes_commented_rub(self) -> None:
        new, report = _mig("A=1\n# POLZA_DAILY_BUDGET_USD=0\n", template=True)
        assert new == "A=1\n" + _block(commented=True)
        assert report.added_budget_rub == "commented"
        assert "POLZA_DAILY_BUDGET_RUB" not in _app_values(new)

    @pytest.mark.parametrize(
        "rub_line",
        ["# POLZA_DAILY_BUDGET_RUB=100", "'POLZA_DAILY_BUDGET_RUB'=500", "POLZA_DAILY_BUDGET_RUB", "polza_daily_budget_rub=7"],
    )
    def test_not_added_when_rub_key_present(self, rub_line) -> None:
        new, report = _mig(f"{rub_line}\nPOLZA_DAILY_BUDGET_USD=5\n", template=True)
        assert new == f"{rub_line}\n"
        assert not report.added_budget_rub

    def test_real_env_never_gets_rub_key(self) -> None:
        new, report = _mig("POLZA_DAILY_BUDGET_USD=5\n\nPOLZA_MODEL=m\n")
        assert new == "POLZA_MODEL=m\n" and not report.added_budget_rub

    @pytest.mark.parametrize("template", [False, True])
    def test_idempotent(self, template) -> None:
        text = "POLZA_MODEL=m\n\n# бюджет USD\nPOLZA_DAILY_BUDGET_USD=0\nPOLZA_PRICE_INPUT_PER_1M=1\n"
        first, _ = _mig(text, template=template)
        second, report = _mig(first, template=template)
        assert second == first
        assert not report.changed


# END_BLOCK_TEMPLATE


# START_BLOCK_DIFFERENTIAL_FUZZ
_FRAGMENTS = [
    "POLZA_API_KEY=sk-FAKE-{i}",
    "POLZA_MODEL=m{i}",
    "polza_model=lower{i}",
    "export POLZA_AGENT_COUNT={i}",
    "'QUOTED_KEY_{i}'=v{i}",
    "BARE_{i}",
    'MULTI_{i}="multi\nline {i}"',
    "TRAP_{i}='x\n\n# inner price USD\nPOLZA_DAILY_BUDGET_USD=9\ny'",
    "INLINE_{i}=1 # note",
    "BROKEN_{i}: value",
    "POLZA_PRICE_INPUT_PER_1M={i}",
    "# POLZA_PRICE_OUTPUT_PER_1M={i}",
    "export polza_daily_budget_usd = {i}",
    "'POLZA_DAILY_BUDGET_USD'={i}",
    "POLZA_PRICE_OUTPUT_PER_1M",
    "# цена за 1M токенов",
    "# backup note {i}",
    "# USD budget",
    "",
    "   ",
    "\t",
]


class TestDifferentialFuzz:
    def test_values_match_dotenv_and_second_run_is_noop(self) -> None:
        rng = random.Random(20260916)
        for case in range(600):
            parts = [rng.choice(_FRAGMENTS).format(i=n) for n in range(rng.randint(0, 12))]
            text = "".join(part + rng.choice(["\n", "\r\n", "\r"]) for part in parts)
            if rng.random() < 0.2:
                text = text.rstrip("\r\n")
            if rng.random() < 0.15:
                text = BOM + text
            template = rng.random() < 0.5
            new, report = _mig(text, template=template)
            assert not report.refused, f"case {case}: {report.refused}"
            assert new.startswith(BOM) == text.startswith(BOM), f"case {case}"
            expected = {k: v for k, v in _app_values(text).items() if not migrate.is_deprecated_name(k)}
            if report.added_budget_rub == "active":
                expected["POLZA_DAILY_BUDGET_RUB"] = "0"
            after = _app_values(new)
            assert after == expected, f"case {case}"
            assert _pydantic_view(after) == _pydantic_view(expected), f"case {case}"
            again, second = _mig(new, template=template)
            assert again == new and not second.changed, f"case {case}"

    def test_large_file_is_fast_enough(self) -> None:
        text = "# USD\n" * 40_000 + "POLZA_API_KEY=x\nPOLZA_DAILY_BUDGET_USD=1\n"
        started = time.perf_counter()
        _, report = _mig(text)
        assert report.changed
        assert time.perf_counter() - started < 20


# END_BLOCK_DIFFERENTIAL_FUZZ


# START_BLOCK_WINDOWS_SECURITY
def _win_api():
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    return ctypes, wintypes, advapi32, kernel32


def _get_dacl_sddl(path) -> str:
    ctypes, wintypes, advapi32, kernel32 = _win_api()
    get_info = advapi32.GetNamedSecurityInfoW
    get_info.argtypes = (
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    )
    get_info.restype = wintypes.DWORD
    to_string = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    to_string.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p)
    to_string.restype = wintypes.BOOL
    dacl, descriptor, text = ctypes.c_void_p(), ctypes.c_void_p(), wintypes.LPWSTR()
    assert get_info(str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)) == 0
    assert to_string(descriptor, 1, 4, ctypes.byref(text), None)
    value = text.value
    kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    kernel32.LocalFree(descriptor)
    return value


def _set_protected_dacl(path, sddl: str) -> None:
    ctypes, wintypes, advapi32, kernel32 = _win_api()
    from_string = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    from_string.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    from_string.restype = wintypes.BOOL
    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL))
    get_dacl.restype = wintypes.BOOL
    set_info = advapi32.SetNamedSecurityInfoW
    set_info.argtypes = (
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    )
    set_info.restype = wintypes.DWORD
    descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    present, defaulted = wintypes.BOOL(), wintypes.BOOL()
    assert from_string(sddl, 1, ctypes.byref(descriptor), None)
    try:
        assert get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted))
        assert set_info(str(path), 1, 0x4 | 0x80000000, None, None, dacl, None) == 0
    finally:
        kernel32.LocalFree(descriptor)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL/атрибуты")
class TestWindowsMetadata:
    SDDL = "D:P(A;;FA;;;OW)"

    def test_temp_created_with_target_dacl_and_exclusive_access(self, tmp_path: Path) -> None:
        target = tmp_path / ".env"
        target.write_bytes(b"A=1\n")
        _set_protected_dacl(target, self.SDDL)
        fd, tmp_name = migrate._create_private_temp(target)
        try:
            # ACE и защита от наследования (P) совпадают; флаг AI (auto-inherited) — служебный, не сравниваем.
            assert _get_dacl_sddl(tmp_name).replace("AI(", "(", 1) == _get_dacl_sddl(target).replace("AI(", "(", 1)
            assert _get_dacl_sddl(tmp_name).startswith("D:P")
            with pytest.raises(PermissionError):
                open(tmp_name, "rb").close()  # пока пишем — никто не откроет даже на чтение
        finally:
            os.close(fd)
            os.unlink(tmp_name)

    def test_apply_keeps_protected_dacl_and_hidden_attribute(self, tmp_path: Path) -> None:
        import ctypes

        target = tmp_path / ".env"
        target.write_bytes(b"A=1\nPOLZA_DAILY_BUDGET_USD=5\n")
        _set_protected_dacl(target, self.SDDL)
        assert ctypes.windll.kernel32.SetFileAttributesW(str(target), stat.FILE_ATTRIBUTE_HIDDEN)
        before = _get_dacl_sddl(target)
        assert migrate.main(["--apply", str(target)]) == 0
        assert target.read_bytes() == b"A=1\n"
        assert _get_dacl_sddl(target) == before
        assert os.stat(target).st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN


# END_BLOCK_WINDOWS_SECURITY


# START_BLOCK_FILE_AND_CLI
class _Sharing(OSError):
    winerror = 32


class TestFile:
    def test_bom_crlf_file_keeps_bom(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_bytes(b"\xef\xbb\xbfPOLZA_API_KEY=x\r\nPOLZA_PRICE_INPUT_PER_1M=2\r\n")
        assert migrate.main(["--apply", str(path)]) == 0
        assert path.read_bytes() == b"\xef\xbb\xbfPOLZA_API_KEY=x\r\n"

    def test_unchanged_file_not_rewritten(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_bytes(b"A=1\r\nB=2\n")
        before = path.stat().st_mtime_ns
        assert migrate.main(["--apply", str(path)]) == 0
        assert path.read_bytes() == b"A=1\r\nB=2\n"
        assert path.stat().st_mtime_ns == before

    def test_no_temp_files_left(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_bytes(b"A=1\nPOLZA_DAILY_BUDGET_USD=1\n")
        assert migrate.main(["--apply", str(path)]) == 0
        assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]

    def test_stale_temp_reported_by_name_even_if_target_missing(self, tmp_path: Path, capsys) -> None:
        (tmp_path / ".env.abcd1234.migrate-tmp").write_bytes(f"POLZA_API_KEY={SECRET}\n".encode())
        (tmp_path / ".env.example.wxyz9876.migrate-tmp").write_bytes(b"A=1\n")
        assert migrate.main([str(tmp_path / ".env")]) == 2  # явный путь, файла нет
        output = capsys.readouterr().out
        assert ".env.abcd1234.migrate-tmp" in output and SECRET not in output
        assert ".env.example.wxyz9876.migrate-tmp" not in output  # чужой временный файл не приписан .env

    @pytest.mark.skipif(sys.platform != "win32", reason="регистронезависимая ФС Windows")
    def test_stale_temp_found_when_path_spelled_in_other_case(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "case1.envfile").write_bytes(b"A=1\n")
        (tmp_path / "case1.envfile.abcd1234.migrate-tmp").write_bytes(f"POLZA_API_KEY={SECRET}\n".encode())
        assert migrate.main([str(tmp_path / "CASE1.ENVFILE")]) == 0
        output = capsys.readouterr().out
        assert "case1.envfile.abcd1234.migrate-tmp" in output and SECRET not in output

    def test_write_failure_with_vanished_target_discards_partial_temp(self, tmp_path: Path, monkeypatch, capsys) -> None:
        path = tmp_path / ".env"
        path.write_bytes(f"POLZA_API_KEY={SECRET}\nPOLZA_DAILY_BUDGET_USD=5\n".encode())
        real_fsync = os.fsync

        def vanish_then_fail(fd) -> None:
            real_fsync(fd)
            os.unlink(path)
            raise OSError(28, "no space")

        monkeypatch.setattr(migrate.os, "fsync", vanish_then_fail)
        assert migrate.main(["--apply", str(path)]) == 2
        monkeypatch.undo()
        output = capsys.readouterr().out
        assert "INCOMPLETE" not in output and "VANISHED" in output
        assert list(tmp_path.iterdir()) == []  # недописанный временный файл не выдаётся за новое содержимое

    def test_read_only_file_refused_without_leftovers(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        original = f"POLZA_API_KEY={SECRET}\nPOLZA_DAILY_BUDGET_USD=5\n".encode()
        path.write_bytes(original)
        os.chmod(path, stat.S_IREAD)
        try:
            if os.access(path, os.W_OK):
                pytest.skip("read-only не действует (root)")
            assert migrate.main(["--apply", str(path)]) == 2
            assert path.read_bytes() == original
            assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]
        finally:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)

    def test_replace_failure_leaves_file_and_no_temp(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / ".env"
        original = f"POLZA_API_KEY={SECRET}\nPOLZA_DAILY_BUDGET_USD=5\n".encode()
        path.write_bytes(original)

        def failing_replace(target, replacement) -> None:
            raise PermissionError(13, f"denied {SECRET}")

        monkeypatch.setattr(migrate, "_replace_file", failing_replace)
        assert migrate.main(["--apply", str(path)]) == 2
        assert path.read_bytes() == original
        assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]

    def test_transient_sharing_violation_is_retried(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / ".env"
        path.write_bytes(b"A=1\nPOLZA_DAILY_BUDGET_USD=5\n")
        real_replace = migrate._replace_file
        failures = iter([True, True, False])

        def flaky_replace(target, replacement) -> None:
            if next(failures):
                raise _Sharing(13, "busy")
            real_replace(target, replacement)

        monkeypatch.setattr(migrate, "_replace_file", flaky_replace)
        monkeypatch.setattr(migrate, "_RETRY_DELAY_SECONDS", 0)
        assert migrate.main(["--apply", str(path)]) == 0
        assert path.read_bytes() == b"A=1\n"
        assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]

    def test_write_failure_leaves_no_temp(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / ".env"
        path.write_bytes(b"A=1\nPOLZA_DAILY_BUDGET_USD=5\n")

        def failing_open(*args, **kwargs):
            raise OSError(28, "no space")

        monkeypatch.setattr(migrate, "open", failing_open, raising=False)
        assert migrate.main(["--apply", str(path)]) == 2
        assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]

    def test_partial_replace_keeps_temp_and_reports_incomplete(self, tmp_path: Path, monkeypatch, capsys) -> None:
        path = tmp_path / ".env"
        path.write_bytes(f"POLZA_API_KEY={SECRET}\nPOLZA_DAILY_BUDGET_USD=5\n".encode())

        def vanished_target(target, replacement) -> None:
            os.unlink(target)  # как ReplaceFileW 1176: заменяемый файл уже удалён, замена не завершена
            raise OSError(5, "partial")

        monkeypatch.setattr(migrate, "_replace_file", vanished_target)
        assert migrate.main(["--apply", str(path)]) == 2
        output = capsys.readouterr().out
        leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(migrate.TEMP_SUFFIX)]
        assert len(leftovers) == 1 and leftovers[0].name in output
        assert "INCOMPLETE" in output and "не изменён" not in output
        assert leftovers[0].read_bytes() == f"POLZA_API_KEY={SECRET}\n".encode()
        assert SECRET not in output

    def test_interrupt_during_partial_replace_is_not_swallowed(self, tmp_path: Path, monkeypatch, capsys) -> None:
        path = tmp_path / ".env"
        path.write_bytes(b"A=1\nPOLZA_DAILY_BUDGET_USD=5\n")

        def interrupted(target, replacement) -> None:
            os.unlink(target)
            raise KeyboardInterrupt

        monkeypatch.setattr(migrate, "_replace_file", interrupted)
        with pytest.raises(KeyboardInterrupt):
            migrate.main(["--apply", str(path)])
        leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(migrate.TEMP_SUFFIX)]
        assert len(leftovers) == 1 and leftovers[0].name in capsys.readouterr().out

    def test_undeletable_temp_is_reported_by_path(self, tmp_path: Path, monkeypatch, capsys) -> None:
        path = tmp_path / ".env"
        path.write_bytes(f"POLZA_API_KEY={SECRET}\nPOLZA_DAILY_BUDGET_USD=5\n".encode())

        def failing_replace(target, replacement) -> None:
            raise OSError(5, "io")

        def locked_unlink(name) -> None:
            raise PermissionError(13, "locked")

        real_unlink = os.unlink
        monkeypatch.setattr(migrate, "_replace_file", failing_replace)
        monkeypatch.setattr(migrate.os, "unlink", locked_unlink)
        assert migrate.main(["--apply", str(path)]) == 2
        monkeypatch.undo()
        output = capsys.readouterr().out
        leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(migrate.TEMP_SUFFIX)]
        assert len(leftovers) == 1 and leftovers[0].name in output
        assert SECRET not in output
        real_unlink(leftovers[0])

    def test_interrupt_during_cleanup_still_reports_temp_path(self, tmp_path: Path, monkeypatch, capsys) -> None:
        path = tmp_path / ".env"
        path.write_bytes(b"A=1\nPOLZA_DAILY_BUDGET_USD=5\n")

        def failing_replace(target, replacement) -> None:
            raise OSError(5, "io")

        def interrupted_unlink(name) -> None:
            raise KeyboardInterrupt

        real_unlink = os.unlink
        monkeypatch.setattr(migrate, "_replace_file", failing_replace)
        monkeypatch.setattr(migrate.os, "unlink", interrupted_unlink)
        with pytest.raises(KeyboardInterrupt):
            migrate.main(["--apply", str(path)])
        monkeypatch.undo()
        leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(migrate.TEMP_SUFFIX)]
        assert len(leftovers) == 1 and leftovers[0].name in capsys.readouterr().out
        real_unlink(leftovers[0])

    def test_hardlinked_file_refused(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_bytes(b"A=1\nPOLZA_DAILY_BUDGET_USD=5\n")
        try:
            os.link(path, tmp_path / "twin.env")
        except OSError:
            pytest.skip("жёсткие ссылки не поддерживаются")
        assert migrate.main(["--apply", str(path)]) == 2
        assert path.read_bytes() == b"A=1\nPOLZA_DAILY_BUDGET_USD=5\n"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-права")
    def test_posix_mode_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_bytes(b"A=1\nPOLZA_DAILY_BUDGET_USD=5\n")
        os.chmod(path, 0o600)
        assert migrate.main(["--apply", str(path)]) == 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestCli:
    def test_dry_run_does_not_write_and_returns_1(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        original = b"A=1\nPOLZA_PRICE_OUTPUT_PER_1M=6\n"
        path.write_bytes(original)
        assert migrate.main([str(path)]) == 1
        assert path.read_bytes() == original
        assert migrate.main(["--apply", str(path)]) == 0
        assert migrate.main([str(path)]) == 0

    def test_template_detected_by_suffix(self, tmp_path: Path) -> None:
        path = tmp_path / ".env.example"
        path.write_bytes("POLZA_MODEL=m\n\n# бюджет USD\nPOLZA_DAILY_BUDGET_USD=0\n".encode())
        assert migrate.main(["--apply", str(path)]) == 0
        text = path.read_bytes().decode("utf-8")
        assert "POLZA_DAILY_BUDGET_RUB=0" in text and "USD" not in text

    def test_explicit_missing_path_or_directory_is_error(self, tmp_path: Path) -> None:
        assert migrate.main(["--apply", str(tmp_path / "nope.env")]) == 2
        assert migrate.main(["--apply", str(tmp_path)]) == 2

    def test_default_targets_are_logged(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setattr(migrate, "REPO_ROOT", tmp_path)
        monkeypatch.chdir(tmp_path)
        assert migrate.main(["--apply"]) == 0
        output = capsys.readouterr().out
        assert "TARGETS" in output and str(tmp_path / ".env") in output

    def test_polza_env_file_missing_is_error(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(migrate, "REPO_ROOT", tmp_path)
        monkeypatch.setenv("POLZA_ENV_FILE", str(tmp_path / "absent.env"))
        assert migrate.main(["--apply"]) == 2

    def test_refusal_returns_2_and_keeps_file(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        original = b"POLZA_DAILY_BUDGET_USD=5\nA=${POLZA_DAILY_BUDGET_USD}\n"
        path.write_bytes(original)
        assert migrate.main(["--apply", str(path)]) == 2
        assert path.read_bytes() == original

    def test_invalid_utf8_returns_2_and_file_untouched(self, tmp_path: Path, capsys) -> None:
        path = tmp_path / ".env"
        original = b"POLZA_PRICE_INPUT_PER_1M=1\nPOLZA_API_KEY=\xff\xfe" + SECRET.encode()
        path.write_bytes(original)
        assert migrate.main(["--apply", str(path)]) == 2
        assert path.read_bytes() == original
        captured = capsys.readouterr()
        assert SECRET not in captured.out + captured.err

    def test_output_never_contains_values_or_line_text(self, tmp_path: Path, capsys) -> None:
        env = tmp_path / ".env"
        env.write_bytes(
            (
                f"POLZA_API_KEY={SECRET}\n"
                "# секретный комментарий про цены USD\n"
                "POLZA_PRICE_INPUT_PER_1M=987.654321\n"
                "POLZA_MODEL=m\n"
                "\n"
                "# уникальный-текст-описания бюджета\n"
                "POLZA_DAILY_BUDGET_USD=123.456789\n"
            ).encode()
        )
        refused = tmp_path / "refused.env"
        refused.write_bytes(f"POLZA_DAILY_BUDGET_USD=555.777\nA={SECRET}-${{POLZA_DAILY_BUDGET_USD}}\n".encode())
        multiline = tmp_path / "multi.env"
        multiline.write_bytes(f'POLZA_DAILY_BUDGET_USD="1\nPOLZA_API_KEY={SECRET}"\n'.encode())
        assert migrate.main([str(env)]) == 1
        assert migrate.main(["--apply", str(env)]) == 0
        assert migrate.main(["--apply", str(refused)]) == 2
        assert migrate.main(["--apply", str(multiline)]) == 2
        captured = capsys.readouterr()
        output = captured.out + captured.err
        for leaked in (SECRET, "987.654321", "123.456789", "555.777", "секретный комментарий", "уникальный-текст"):
            assert leaked not in output
        assert "POLZA_PRICE_INPUT_PER_1M" in output
        assert _app_values(env.read_bytes().decode("utf-8"))["POLZA_API_KEY"] == SECRET

    def test_default_targets_follow_server_resolution(self, tmp_path: Path, monkeypatch) -> None:
        repo, work, home = tmp_path / "repo", tmp_path / "work", tmp_path / "home"
        for directory in (repo, work, home):
            directory.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("POLZA_ENV_FILE", "~/grok.env")
        assert migrate.default_targets(repo, work) == [(home / "grok.env", True), (repo / ".env.example", False)]
        monkeypatch.delenv("POLZA_ENV_FILE")
        # в cwd нет .env — сервер (и скрипт) берёт <repo>/.env
        assert migrate.default_targets(repo, work) == [(repo / ".env", False), (repo / ".env.example", False)]
        (work / ".env").write_bytes(b"A=1\n")
        # в cwd есть .env — только он, <repo>/.env не трогаем
        assert migrate.default_targets(repo, work) == [(work / ".env", False), (repo / ".env.example", False)]


# END_BLOCK_FILE_AND_CLI
