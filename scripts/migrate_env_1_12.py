#!/usr/bin/env python
# FILE: scripts/migrate_env_1_12.py
# VERSION: 1.12.0
# START_MODULE_CONTRACT
#   PURPOSE: Миграция .env / .env.example на grok-critic 1.12.0 без вывода содержимого файлов
#   SCOPE: Удалить POLZA_PRICE_INPUT_PER_1M / POLZA_PRICE_OUTPUT_PER_1M / POLZA_DAILY_BUDGET_USD
#          (активные, закомментированные, в кавычках, без значения); в шаблоне (*.example / *.sample /
#          *.template) — ещё и их описания, плюс POLZA_DAILY_BUDGET_RUB=0. Разбор — тем же парсером
#          python-dotenv, что у приложения; запись — только если значения остальных ключей, как их
#          прочитает приложение, не меняются.
#   DEPENDS: python-dotenv
#   LINKS: M-CONFIG
# END_MODULE_CONTRACT
"""Миграция .env на grok-critic 1.12.0 — без вывода содержимого файлов.

Запуск:
    python scripts/migrate_env_1_12.py                 # dry-run: что будет изменено
    python scripts/migrate_env_1_12.py --apply         # записать изменения
    python scripts/migrate_env_1_12.py --apply PATH…   # явные файлы (рекомендуется)

По умолчанию — ровно тот .env, который прочитает сервер, запущенный из текущей директории
(POLZA_ENV_FILE с раскрытием ~ → ./.env → <repo>/.env), плюс шаблон <repo>/.env.example.
Выбранные цели всегда печатаются.

Гарантии:
- вывод содержит ТОЛЬКО имена устаревших ключей, счётчики, номера строк и пути — значения и текст
  строк не печатаются никогда (файл может содержать POLZA_API_KEY);
- файл разбирается парсером python-dotenv, как в приложении (многострочные значения в кавычках,
  ключи в кавычках, одиночный CR, BOM как часть первого ключа); неизменённые участки — побайтно;
- в реальном .env комментарии не удаляются (только номера строк на проверку), в шаблоне удаляются
  описания цен/бюджета над удалёнными ключами;
- перед записью: значения, как их прочитает приложение (с интерполяцией без устаревших ключей в окружении,
  без интерполяции и в регистронезависимом виде pydantic-settings), совпадают с исходными за вычетом
  устаревших ключей — иначе отказ; ${…} на удаляемые ключи и многострочные значения устаревших ключей — отказ;
- запись атомарная; временный файл создаётся с правами цели (Windows: DACL цели при создании; POSIX: 0600),
  замена на Windows — ReplaceFileW (ACL, атрибуты, время создания сохраняются) с повтором при кратковременной
  блокировке; файлы только для чтения и с жёсткими ссылками не трогаются.

Exit codes: 0 — изменений нет или они записаны; 1 — dry-run нашёл изменения; 2 — ошибка или отказ.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import os
import re
import secrets
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import dotenv_values
    from dotenv.parser import parse_stream
except ImportError:  # pragma: no cover — зависимость grok-critic
    sys.stderr.write("python-dotenv не найден: выполните `python -m pip install -e .` в корне репозитория\n")
    raise SystemExit(2) from None

logger = logging.getLogger("grok-critic.migrate-env")

REPO_ROOT = Path(__file__).resolve().parent.parent

# START_BLOCK_RULES
DEPRECATED_KEYS: frozenset[str] = frozenset({
    "POLZA_PRICE_INPUT_PER_1M",
    "POLZA_PRICE_OUTPUT_PER_1M",
    "POLZA_DAILY_BUDGET_USD",
})
BUDGET_USD_KEY = "POLZA_DAILY_BUDGET_USD"
BUDGET_RUB_KEY = "POLZA_DAILY_BUDGET_RUB"
# Описание бюджета в ₽ для шаблона. В реальный .env не добавляется: 0 — и так значение по умолчанию.
BUDGET_RUB_DESCRIPTION: tuple[str, ...] = (
    "# Дневной лимит расходов в ₽ по фактической стоимости запросов (0 = без лимита).",
    "# Soft limit: проверяется до платного запроса. Стоимость токенов задавать не нужно —",
    "# тариф модели берётся из Polza.AI (GET /models/{model}).",
)
TEMPLATE_SUFFIXES: tuple[str, ...] = (".example", ".sample", ".template")
TEMP_SUFFIX = ".migrate-tmp"
BOM = "﻿"
# ReplaceFileW: замена не завершена, заменяемый файл мог уже исчезнуть — временный файл удалять нельзя.
_PARTIAL_REPLACE_WINERRORS = frozenset({1176, 1177})
# Кратковременная блокировка (антивирус, индексатор, синхронизация): доступ запрещён / файл занят.
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})
_RETRY_ATTEMPTS = 5
_RETRY_DELAY_SECONDS = 0.15

_DEPRECATED_ALTERNATION = "|".join(sorted(DEPRECATED_KEYS))
_NEWLINE_RE = re.compile(r"\r\n|\n|\r")
_LEAD_RE = re.compile(r"\s*")
_TRAILING_NEWLINE_RE = re.compile(r"(?:\r\n|\n|\r)\Z")
_ENDS_WITH_BLANK_LINE_RE = re.compile(r"(?:\r\n|\n|\r)[^\S\r\n]*(?:\r\n|\n|\r)\Z")
_ENTRY_PREFIX_RE = re.compile(r"^(?P<indent>[^\S\r\n]*)(?P<export>export[^\S\r\n]+)?")
# Закомментированный ключ: `# KEY=…`, `# export KEY=…`, `# 'KEY'=…`, а также голый `# POLZA_…`.
_COMMENTED_KEY_RE = re.compile(
    r"^[^\S\r\n]*#[^\S\r\n]*(?:export[^\S\r\n]+)?"
    r"(?:(?:'(?P<quoted>[^'\r\n]+)'|(?P<plain>[^=#\s']+))[^\S\r\n]*="
    r"|(?P<bare>POLZA_[A-Za-z0-9_]+)[^\S\r\n]*(?:\r\n|\n|\r)?\Z)",
    re.IGNORECASE,
)
# Текст, похожий на устаревший ключ, в строке, которую приложение не читает как этот ключ.
_LOOKS_DEPRECATED_RE = re.compile(
    r"^[^\S\r\n]*#?[^\S\r\n]*(?:export[^\S\r\n]+)?'?(?:" + _DEPRECATED_ALTERNATION + r")\b", re.IGNORECASE,
)
# Ссылка на удаляемый ключ в значении другого ключа: ${POLZA_DAILY_BUDGET_USD} / ${…:-default}.
_DEPRECATED_REFERENCE_RE = re.compile(r"\$\{[^\S\r\n]*(?:" + _DEPRECATED_ALTERNATION + r")\b", re.IGNORECASE)
# Шаблон: описание над удалённым ключом удаляется, только если оно явно про цены/бюджет.
_TEMPLATE_DESCRIPTION_RE = re.compile(
    r"USD|доллар|\bцен|\bprice|бюджет|budget|\bза\s*1\s*M\b|\bper\s*1\s*M\b", re.IGNORECASE,
)
# Комментарии рядом с удалёнными ключами / устаревшие упоминания — только на ручную проверку.
_REVIEW_WORDING_RE = re.compile(
    r"USD|\$|доллар|\bцен|price|стоимост|\bcost|бюджет|budget|лимит|\blimit|\b1\s*M\b|токен|token",
    re.IGNORECASE,
)
_STALE_COMMENT_RE = re.compile(r"USD|\$\s*\d|доллар|PRICE_(?:INPUT|OUTPUT)", re.IGNORECASE)
_TEMP_NAME_RE_TEMPLATE = r"{name}\.[A-Za-z0-9_]{{8}}" + re.escape(TEMP_SUFFIX)
USER_DATETIME_FORMAT = "%d.%m.%Y %H:%M:%S"

# Виды единиц разбора
DEPRECATED = "deprecated"  # активная запись с устаревшим ключом
COMMENTED_DEPRECATED = "commented-deprecated"  # `# POLZA_DAILY_BUDGET_USD=…`
COMMENT = "comment"  # комментарий (в т.ч. закомментированный НЕустаревший ключ — тогда key задан)
BLANK = "blank"  # хвостовые пробельные символы файла
CONTENT = "content"  # любая другая запись
ERROR = "error"  # строка, которую python-dotenv не разобрал (сохраняется как есть)
# END_BLOCK_RULES


# START_BLOCK_MODEL
def is_deprecated_name(key: str) -> bool:
    """Устаревший ключ так, как его сопоставит приложение: только ASCII ("ı".upper() == "I" — не совпадение)."""
    return key.isascii() and key.upper() in DEPRECATED_KEYS


def _normalized_key(key: str) -> str:
    return key.upper() if key.isascii() else key


class ReplaceIncompleteError(Exception):
    """Замена не завершена: исходного файла может не быть, новое содержимое — во временном файле."""

    def __init__(self, temp_path: str, winerror: int | None) -> None:
        super().__init__("replace incomplete")
        self.temp_path = temp_path
        self.winerror = winerror


@dataclass
class _Unit:
    """Запись python-dotenv, разделённая на пустые строки перед ней (gap) и саму запись (body)."""

    gap: str
    body: str
    kind: str
    key: str | None  # имя ключа (ASCII — в верхнем регистре) для записей и закомментированных ключей
    line: int  # номер первой строки body в исходном файле
    removed: bool = False
    replacement: str | None = None  # текст, которым заменяется удалённая запись (бюджет в ₽)
    review: bool = False


@dataclass
class MigrationReport:
    """Итог миграции файла — только имена устаревших ключей и номера строк, без текста строк."""

    removed_keys: list[tuple[str, int]] = field(default_factory=list)  # (ключ, номер строки до миграции)
    removed_comment_lines: list[int] = field(default_factory=list)  # номера строк до миграции
    added_budget_rub: str = ""  # "" | "active" | "commented"
    review_lines_before: list[int] = field(default_factory=list)
    review_lines_after: list[int] = field(default_factory=list)
    refused: str = ""  # причина отказа (без содержимого файла)
    applied: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.removed_keys or self.removed_comment_lines or self.added_budget_rub)


# END_BLOCK_MODEL


# START_BLOCK_PARSE
def _count_newlines(text: str) -> int:
    return len(_NEWLINE_RE.findall(text))


def _parse_units(text: str) -> list[_Unit]:
    """Разбор парсером python-dotenv. Склейка gap+body всех единиц == text побайтно."""
    raw: list[list] = []  # [original, key, error]
    for binding in parse_stream(io.StringIO(text, newline="")):
        original = binding.original.string
        # python-dotenv: у неразобранной строки `_rest_of_line` забирает только \r из \r\n — \n вернуть к ней.
        if raw and raw[-1][0].endswith("\r") and original.startswith("\n"):
            raw[-1][0] += "\n"
            original = original[1:]
        if original:
            raw.append([original, binding.key, binding.error])

    units: list[_Unit] = []
    line = 1  # свой счётчик: CRLF — одна строка (у python-dotenv разорванный CRLF считался бы дважды)
    for original, binding_key, error in raw:
        lead_match = _LEAD_RE.match(original)
        lead = lead_match.group() if lead_match else ""
        cut = max(lead.rfind("\n"), lead.rfind("\r")) + 1
        gap, body = original[:cut], original[cut:]
        body_line = line + _count_newlines(gap)
        line += _count_newlines(original)
        key: str | None = None
        if error:
            kind = ERROR
        elif binding_key is not None:
            key = _normalized_key(binding_key)
            kind = DEPRECATED if is_deprecated_name(binding_key) else CONTENT
        elif not body.strip():
            kind = BLANK
        else:
            match = _COMMENTED_KEY_RE.match(body)
            if match:
                key = _normalized_key(match.group("quoted") or match.group("plain") or match.group("bare"))
            kind = COMMENTED_DEPRECATED if key is not None and is_deprecated_name(key) else COMMENT
        units.append(_Unit(gap=gap, body=body, kind=kind, key=key, line=body_line))
    return units


def _paragraphs(units: Sequence[_Unit]) -> list[list[int]]:
    """Абзацы — записи между пустыми строками (новый абзац, если перед записью есть перевод строки)."""
    paragraphs: list[list[int]] = []
    for index, unit in enumerate(units):
        if not paragraphs or unit.gap:
            paragraphs.append([index])
        else:
            paragraphs[-1].append(index)
    return paragraphs


def _dominant_newline(text: str) -> str:
    crlf = text.count("\r\n")
    counts = {"\r\n": crlf, "\n": text.count("\n") - crlf, "\r": text.count("\r") - crlf}
    best = max(counts.values())
    return "\n" if best == 0 else next(nl for nl in ("\n", "\r\n", "\r") if counts[nl] == best)


@contextlib.contextmanager
def _without_deprecated_environ() -> Iterator[None]:
    """Интерполяция не должна брать устаревшие ключи из окружения скрипта: у сервера окружение другое."""
    saved = {name: os.environ[name] for name in list(os.environ) if is_deprecated_name(name)}
    for name in saved:
        del os.environ[name]
    try:
        yield
    finally:
        os.environ.update(saved)


def effective_values(text: str, *, interpolate: bool = True) -> dict[str, str | None]:
    """Значения так, как их прочитает приложение: open(encoding="utf-8") (BOM не снимается) + dotenv_values."""
    stream = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")  # newline=None, как open()
    dotenv_logger = logging.getLogger("dotenv.main")
    previous_level = dotenv_logger.level
    dotenv_logger.setLevel(logging.CRITICAL)  # предупреждения парсера — не в вывод скрипта
    try:
        with _without_deprecated_environ():
            return dict(dotenv_values(stream=stream, interpolate=interpolate))
    finally:
        dotenv_logger.setLevel(previous_level)


def _guard_views(values: dict[str, str | None]) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """(как есть; как у pydantic-settings — ключи в нижнем регистре, побеждает последний)."""
    return values, {key.lower(): value for key, value in values.items()}


# END_BLOCK_PARSE


# START_BLOCK_MIGRATE_TEXT
def _mark_descriptions(
    units: list[_Unit], paragraphs: list[list[int]], report: MigrationReport, *, template: bool,
) -> None:
    """Комментарии прямо над удаляемыми ключами.

    Реальный .env (секреты, истории нет): комментарии НЕ удаляются — про цены/токены/лимиты уходят на
    ручную проверку. Шаблон (история в git): удаляется описание явно про цены/бюджет, если оно не общее
    с живой записью ниже (живая запись раньше собственного описания) — иначе на проверку.
    """
    for paragraph in paragraphs:
        # За один обратный проход: для позиции p — есть ли дальше живая запись раньше простого комментария.
        live_before_comment = [False] * (len(paragraph) + 1)
        for position in range(len(paragraph) - 1, -1, -1):
            unit = units[paragraph[position]]
            if unit.kind in (CONTENT, ERROR):
                live_before_comment[position] = True
            elif unit.kind == COMMENT and unit.key is None:
                live_before_comment[position] = False
            else:
                live_before_comment[position] = live_before_comment[position + 1]
        position = 0
        while position < len(paragraph):
            if units[paragraph[position]].kind not in (DEPRECATED, COMMENTED_DEPRECATED):
                position += 1
                continue
            run_end = position
            while run_end < len(paragraph) and units[paragraph[run_end]].kind in (DEPRECATED, COMMENTED_DEPRECATED):
                run_end += 1
            shared = live_before_comment[run_end]
            above = position - 1
            while above >= 0:
                comment = units[paragraph[above]]
                if comment.kind != COMMENT or comment.key is not None or comment.removed:
                    break
                deletable = template and not shared and bool(_TEMPLATE_DESCRIPTION_RE.search(comment.body))
                if not deletable:
                    comment.review = comment.review or bool(_REVIEW_WORDING_RE.search(comment.body))
                    if template:
                        break
                    above -= 1
                    continue
                comment.removed = True
                report.removed_comment_lines.append(comment.line)
                above -= 1
            position = run_end


def _render(units: list[_Unit], paragraphs: list[list[int]], report: MigrationReport) -> str:
    """Собрать текст без удалённых записей; пустые строки-разделители не копятся и не теряются."""
    pieces: list[str] = []
    out_line = 1
    carry: str | None = None  # пустые строки начала файла, если первые абзацы удалены целиком
    emitted_any = False
    report.review_lines_before.clear()
    report.review_lines_after.clear()
    for paragraph in paragraphs:
        survivors = [units[i] for i in paragraph if not units[i].removed or units[i].replacement is not None]
        if not survivors:
            if not emitted_any and carry is None:
                carry = units[paragraph[0]].gap
            continue
        gap = units[paragraph[0]].gap if carry is None else carry
        carry = None
        for unit in survivors:
            body = unit.replacement if unit.replacement is not None else unit.body
            pieces.append(gap)
            out_line += _count_newlines(gap)
            gap = ""
            if unit.review and unit.replacement is None:
                report.review_lines_before.append(unit.line)
                report.review_lines_after.append(out_line)
            pieces.append(body)
            out_line += _count_newlines(body)
        emitted_any = True
    return "".join(pieces)


def migrate_text(text: str, *, template: bool) -> tuple[str, MigrationReport]:
    """Чистая миграция текста файла. Возвращает (новый текст, отчёт); без изменений/при отказе — исходный."""
    report = MigrationReport()
    units = _parse_units(text)  # BOM не снимается: приложение читает его как часть первого ключа
    if "".join(unit.gap + unit.body for unit in units) != text:
        report.refused = "парсер python-dotenv не восстановил файл побайтно — миграция небезопасна"
        return text, report

    multiline: list[str] = []
    for unit in units:
        if unit.kind in (DEPRECATED, COMMENTED_DEPRECATED):
            unit.removed = True
            report.removed_keys.append((str(unit.key), unit.line))
            extra_lines = len(_NEWLINE_RE.findall(unit.body.rstrip("\r\n")))
            if extra_lines:
                multiline.append(f"{unit.line}–{unit.line + extra_lines}")
        elif unit.kind == ERROR and _LOOKS_DEPRECATED_RE.search(unit.body):
            unit.review = True  # python-dotenv эту строку не читает — трогать не будем, но покажем
        elif unit.kind == CONTENT and unit.key and unit.key.startswith(BOM) and _LOOKS_DEPRECATED_RE.search(unit.body[1:]):
            unit.review = True  # ключ с приклеенным BOM приложение не видит — не трогаем, но покажем
        elif unit.kind == COMMENT and unit.key is None and _STALE_COMMENT_RE.search(unit.body):
            unit.review = True
    if multiline:
        report.refused = (
            "значение устаревшего ключа занимает несколько строк (строки " + ", ".join(multiline) + ") — "
            "внутри могут быть другие данные; поправьте вручную, файл не изменён"
        )
        return text, report

    paragraphs = _paragraphs(units)
    _mark_descriptions(units, paragraphs, report, template=template)

    newline = _dominant_newline(text)
    append_block = ""
    if template and not any(unit.key == BUDGET_RUB_KEY for unit in units):
        usd = [u for u in units if u.key == BUDGET_USD_KEY and u.kind in (DEPRECATED, COMMENTED_DEPRECATED)]
        commented = bool(usd) and all(u.kind == COMMENTED_DEPRECATED for u in usd)
        report.added_budget_rub = "commented" if commented else "active"
        prefix = _ENTRY_PREFIX_RE.match(usd[0].body) if usd and not commented else None
        indent = prefix.group("indent") if prefix else ""
        export = (prefix.group("export") or "") if prefix else ""
        key_line = ("# " if commented else f"{indent}{export}") + f"{BUDGET_RUB_KEY}=0"
        block = newline.join((*(indent + line for line in BUDGET_RUB_DESCRIPTION), key_line)) + newline
        if usd:
            usd[0].replacement = block
        else:
            append_block = block

    new_text = _render(units, paragraphs, report)
    if append_block:
        if new_text and not new_text.endswith(("\n", "\r")):
            new_text += newline
        if new_text.strip() and not _ENDS_WITH_BLANK_LINE_RE.search(new_text):
            new_text += newline
        new_text += append_block
    elif report.changed and text and not text.endswith(("\n", "\r")):
        new_text = _TRAILING_NEWLINE_RE.sub("", new_text, count=1)  # файл был без хвостового перевода строки

    if not report.changed:
        return text, report

    references = [u.line for u in units if not u.removed and u.kind in (CONTENT, ERROR)
                  and _DEPRECATED_REFERENCE_RE.search(u.body)]
    if references:
        report.refused = (
            "строки " + ", ".join(map(str, references)) + " ссылаются на удаляемые ключи через ${...} — "
            "поправьте их вручную, файл не изменён"
        )
        return text, report

    # Пост-условие: приложение увидит те же значения, кроме удалённых ключей (и добавленного ₽-бюджета).
    for interpolate in (True, False):
        before = {k: v for k, v in effective_values(text, interpolate=interpolate).items() if not is_deprecated_name(k)}
        if report.added_budget_rub == "active":
            before[BUDGET_RUB_KEY] = "0"
        after = effective_values(new_text, interpolate=interpolate)
        leftovers = sorted({k.upper() for k in after if is_deprecated_name(k)})
        if leftovers:
            report.refused = "после миграции остались бы ключи: " + ", ".join(leftovers)
            return text, report
        if _guard_views(after) != _guard_views(before):
            report.refused = "изменились бы значения других ключей — файл не изменён, поправьте вручную"
            return text, report
    return new_text, report


# END_BLOCK_MIGRATE_TEXT


# START_BLOCK_FILE_IO
def _retry_transient(action: Callable[[], None]) -> None:
    """Повторить действие при кратковременной блокировке файла на Windows (winerror 5/32/33)."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            action()
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in _TRANSIENT_WINERRORS or attempt == _RETRY_ATTEMPTS:
                raise
            logger.debug("[MigrateEnv][_retry_transient][RETRY] attempt=%d winerror=%s", attempt, exc.winerror)
            time.sleep(_RETRY_DELAY_SECONDS)


def _create_private_temp(target: Path) -> tuple[int, str]:
    """Пустой временный файл рядом с целью, недоступный посторонним с момента создания.

    POSIX: mkstemp (0600). Windows: CreateFileW с дескриптором безопасности цели (DACL и признак
    защиты от наследования) и без совместного доступа — прочитать данные раньше времени нельзя.
    """
    if sys.platform != "win32":
        return tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.", suffix=TEMP_SUFFIX)
    import ctypes
    import msvcrt
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class SecurityAttributes(ctypes.Structure):
        _fields_ = (("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL))

    get_info = advapi32.GetNamedSecurityInfoW
    get_info.argtypes = (
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    )
    get_info.restype = wintypes.DWORD
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    invalid_handle = ctypes.c_void_p(-1).value
    generic_write, create_new, file_attribute_normal = 0x40000000, 1, 0x80
    error_file_exists, error_already_exists = 80, 183

    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = get_info(str(target), 1, 0x4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if status:
        raise ctypes.WinError(status)
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        for _ in range(16):
            tmp_name = str(target.parent / f"{target.name}.{secrets.token_hex(4)}{TEMP_SUFFIX}")
            handle = create_file(tmp_name, generic_write, 0, ctypes.byref(attributes), create_new, file_attribute_normal, None)
            if handle is None or handle == invalid_handle:
                error = ctypes.get_last_error()
                if error in (error_file_exists, error_already_exists):
                    continue
                raise ctypes.WinError(error)
            try:
                return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY), tmp_name
            except BaseException:
                close_handle(handle)
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        raise FileExistsError("не удалось подобрать имя временного файла")
    finally:
        local_free(descriptor)


def _replace_file(target: Path, replacement: str) -> None:
    """Атомарно подменить target содержимым replacement, сохранив метаданные target."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        replace_file = kernel32.ReplaceFileW
        replace_file.argtypes = (
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID,
        )
        replace_file.restype = wintypes.BOOL
        replacefile_ignore_merge_errors = 0x00000002
        # ReplaceFileW переносит на новое содержимое ACL, атрибуты (hidden и т.п.) и время создания.
        if not replace_file(str(target), replacement, None, replacefile_ignore_merge_errors, None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(replacement, target)


def _discard_temp(tmp_name: str) -> None:
    def unlink() -> None:
        if os.path.lexists(tmp_name):
            os.chmod(tmp_name, stat.S_IREAD | stat.S_IWRITE)
            os.unlink(tmp_name)

    try:
        _retry_transient(unlink)
    except BaseException as exc:
        logger.error(
            "[MigrateEnv][_discard_temp][TEMP] не удалось удалить временный файл %s (%s) — удалите его "
            "вручную: он содержит данные исходного файла",
            tmp_name, type(exc).__name__,
        )
        if not isinstance(exc, OSError):
            raise


def _atomic_write(target: Path, data: bytes, original: os.stat_result) -> None:
    """Временный файл с правами цели (имя покрыто .gitignore: *.migrate-tmp) → данные → замена."""
    fd, tmp_name = _create_private_temp(target)
    replace_attempted = False  # до замены временный файл может быть недописан — «новым содержимым» он не считается
    try:
        try:
            handle = open(fd, "wb", closefd=True)
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if sys.platform != "win32":
            os.chmod(tmp_name, stat.S_IMODE(original.st_mode))
            with contextlib.suppress(OSError):
                os.chown(tmp_name, original.st_uid, original.st_gid)
        replace_attempted = True
        _retry_transient(lambda: _replace_file(target, tmp_name))
    except BaseException as exc:
        # Путь временного файла — первым делом: даже если дальше прилетит второй Ctrl+C, он уже в логе.
        logger.warning("[MigrateEnv][_atomic_write][TEMP] сбой записи %s, временный файл %s", target, tmp_name)
        winerror = getattr(exc, "winerror", None)
        target_missing = not target.exists()
        if replace_attempted and (winerror in _PARTIAL_REPLACE_WINERRORS or target_missing):
            if not isinstance(exc, Exception):
                raise  # Ctrl+C не глушим; путь временного файла уже в логе
            raise ReplaceIncompleteError(tmp_name, winerror) from exc
        if target_missing:
            logger.error("[MigrateEnv][_atomic_write][VANISHED] %s: файл исчез во время записи — временный файл удаляется", target)
        _discard_temp(tmp_name)
        raise


def is_template(path: Path) -> bool:
    return path.name.lower().endswith(TEMPLATE_SUFFIXES)


def stale_temp_files(path: Path) -> list[str]:
    """Временные файлы прошлых запусков для path (точная форма имени, не для X.example).

    Временный файл называется по РАЗРЕШЁННОЙ цели (реальный регистр, длинное имя, цель симлинка),
    поэтому ищем и по пути как указан, и по разрешённому; на Windows — без учёта регистра.
    """
    locations = [(path.parent, path.name)]
    with contextlib.suppress(OSError, RuntimeError):
        resolved = path.resolve()
        locations.append((resolved.parent, resolved.name))
    flags = re.IGNORECASE if sys.platform == "win32" else 0
    found: dict[str, str] = {}
    for directory, name in locations:
        if not directory.is_dir():
            continue
        pattern = re.compile(_TEMP_NAME_RE_TEMPLATE.format(name=re.escape(name)), flags)
        for candidate in directory.iterdir():
            if pattern.fullmatch(candidate.name):
                found[os.path.normcase(str(candidate))] = candidate.name
    return sorted(found.values())


def migrate_file(path: Path, *, apply: bool) -> MigrationReport:
    """Мигрировать один файл. Симлинк не заменяется файлом — изменяется его цель."""
    target = path.resolve(strict=True)
    text = target.read_bytes().decode("utf-8")
    new_text, report = migrate_text(text, template=is_template(path))
    if report.refused or not report.changed:
        return report
    original = target.stat()
    if original.st_nlink > 1:
        report.refused = "у файла есть жёсткие ссылки — замена их разорвёт; поправьте файл вручную"
        return report
    if not os.access(target, os.W_OK):
        report.refused = "файл только для чтения — снимите атрибут и повторите"
        return report
    if apply:
        _atomic_write(target, new_text.encode("utf-8"), original)
        report.applied = True
    return report


# END_BLOCK_FILE_IO


# START_BLOCK_CLI
def _fmt_lines(numbers: Sequence[int]) -> str:
    return ", ".join(str(n) for n in numbers)


def _log_report(name: str, report: MigrationReport) -> None:
    if report.refused:
        logger.error("[MigrateEnv][migrate_file][REFUSED] %s: %s", name, report.refused)
    elif report.changed:
        parts: list[str] = []
        if report.removed_keys:
            parts.append("ключи: " + ", ".join(f"{key} (стр. {no})" for key, no in report.removed_keys))
        if report.removed_comment_lines:
            parts.append("строки-описания: " + _fmt_lines(report.removed_comment_lines))
        if report.added_budget_rub:
            state = "закомментированный " if report.added_budget_rub == "commented" else ""
            parts.append(f"добавлен {state}{BUDGET_RUB_KEY}=0 с описанием")
        mode, verb = ("APPLY", "изменено") if report.applied else ("DRY_RUN", "будет изменено")
        logger.info("[MigrateEnv][migrate_file][%s] %s: %s — %s", mode, name, verb, "; ".join(parts))
    else:
        logger.info("[MigrateEnv][migrate_file][NOOP] %s: изменений не требуется", name)
    review = report.review_lines_after if report.applied else report.review_lines_before
    if review and not report.refused:
        logger.warning(
            "[MigrateEnv][migrate_file][REVIEW] %s: проверьте вручную строки %s — комментарии про цены/токены/USD "
            "рядом с удалёнными ключами (номера %s миграции; текст не выводится)",
            name, _fmt_lines(review), "после" if report.applied else "до",
        )


def default_targets(repo_root: Path, cwd: Path | None = None) -> list[tuple[Path, bool]]:
    """Тот .env, который прочитает сервер из cwd (как config._resolve_env_file), и шаблон. (путь, обязателен)."""
    env_file = os.environ.get("POLZA_ENV_FILE", "").strip()
    if env_file:
        primary = (Path(env_file).expanduser(), True)  # задан явно — отсутствие файла это ошибка
    else:
        cwd_env = (cwd or Path.cwd()) / ".env"
        primary = (cwd_env if cwd_env.is_file() else repo_root / ".env", False)
    template = (repo_root / ".env.example", False)
    same = os.path.normcase(str(primary[0].resolve())) == os.path.normcase(str(template[0].resolve()))
    return [primary] if same else [primary, template]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Миграция .env на grok-critic 1.12.0: удалить POLZA_PRICE_* и POLZA_DAILY_BUDGET_USD. "
        "Содержимое файлов не выводится.",
    )
    parser.add_argument("paths", nargs="*", type=Path, help="файлы (по умолчанию — .env, который прочитает сервер, и .env.example)")
    parser.add_argument("--apply", action="store_true", help="записать изменения (без флага — dry-run)")
    args = parser.parse_args(argv)

    if isinstance(sys.stdout, io.TextIOWrapper):  # консоль не в UTF-8 — не терять отчёт из-за кириллицы
        with contextlib.suppress(ValueError, OSError):
            sys.stdout.reconfigure(errors="backslashreplace")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt=USER_DATETIME_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        targets = [(path, True) for path in args.paths] if args.paths else default_targets(REPO_ROOT)
        logger.info(
            "[MigrateEnv][main][TARGETS] %s%s", "явные цели" if args.paths else "цели по умолчанию",
            ": " + ", ".join(str(path) for path, _ in targets),
        )
        status = 0
        for path, required in targets:
            stale = stale_temp_files(path)
            if stale:
                logger.error(
                    "[MigrateEnv][main][STALE_TEMP] %s: остались временные файлы прошлого запуска (%s) — они "
                    "содержат данные файла; если файла нет, переименуйте временный обратно, иначе удалите",
                    path, ", ".join(stale),
                )
            if not path.is_file():
                if required:
                    logger.error("[MigrateEnv][main][MISSING] %s: файла нет (или это не файл)", path)
                    status = 2
                else:
                    logger.info("[MigrateEnv][main][SKIP] %s: файла нет — пропуск", path)
                continue
            if path.is_symlink():
                logger.info("[MigrateEnv][main][SYMLINK] %s: символическая ссылка — изменяется целевой файл", path)
            try:
                report = migrate_file(path, apply=args.apply)
            except ReplaceIncompleteError as exc:
                logger.error(
                    "[MigrateEnv][main][INCOMPLETE] %s: замена не завершена (winerror=%s) — исходного файла может "
                    "не быть; новое содержимое во временном файле %s, переименуйте его в %s%s",
                    path, exc.winerror, exc.temp_path, path.name,
                    "; при winerror=1177 исходный файл мог остаться в той же папке под другим именем — найдите и "
                    "удалите эту копию (в ней данные файла)" if exc.winerror == 1177 else "",
                )
                status = 2
                continue
            except UnicodeDecodeError as exc:
                logger.error("[MigrateEnv][main][DECODE] %s: не UTF-8 (позиция %d) — файл не изменён", path, exc.start)
                status = 2
                continue
            except OSError as exc:  # только тип и коды — без текста исключения
                logger.error(
                    "[MigrateEnv][main][IO] %s: %s errno=%s winerror=%s — файл не изменён",
                    path, type(exc).__name__, exc.errno, getattr(exc, "winerror", None),
                )
                status = 2
                continue
            except Exception as exc:  # защита в глубину: без текста исключения и трейсбека
                logger.error("[MigrateEnv][main][UNEXPECTED] %s: %s — проверьте файл", path, type(exc).__name__)
                status = 2
                continue
            _log_report(str(path), report)
            if report.refused:
                status = 2
            elif report.changed and not report.applied:
                status = max(status, 1)
        if status == 1:
            logger.info("[MigrateEnv][main][DRY_RUN] ничего не записано — для записи запустите с --apply")
        return status
    finally:
        logger.removeHandler(handler)


# END_BLOCK_CLI

if __name__ == "__main__":
    sys.exit(main())
