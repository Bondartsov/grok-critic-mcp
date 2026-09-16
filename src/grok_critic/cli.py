# FILE: src/grok_critic/cli.py
# VERSION: 1.12.0
# START_MODULE_CONTRACT
#   PURPOSE: Terminal CLI over critic/api_client — agents can use/fix the critic via Bash when MCP is down
#   SCOPE: serve (stdio MCP), health, doctor, review, followup, logs, config; exit codes; --json
#   DEPENDS: M-CRITIC, M-CONFIG, M-API, M-SERVER (formatting + serve entry)
#   LINKS: M-CLI
# END_MODULE_CONTRACT
# START_CHANGE_SUMMARY
#   PRICING-RUB: --json review/followup — cost_usd удалён, добавлен cost_is_estimate;
#     health --ping — тариф/лимиты модели в ₽ (JSON-ключ pricing), usage_today.date
#     DD.MM.YYYY; config — daily_budget_rub вместо price_* / daily_budget_usd.
#     Денежный формат — общий _fmt_rub из server.
# END_CHANGE_SUMMARY

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
from collections import deque
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from grok_critic.config import _resolve_env_file, config
from grok_critic.critic import followup as critic_followup_fn
from grok_critic.critic import general_review, health_check, review_store
from grok_critic.server import (
    _fmt_budget_rub,
    _fmt_rub,
    _format_pricing_lines,
    _format_result,
    _format_usage_today,
    _is_sensitive_file,
)
from grok_critic.server import main as _server_main

logger = logging.getLogger("grok-critic.cli")

EXIT_OK = 0
EXIT_WARN = 1
EXIT_ERR = 2

_REPO_DIR = Path(__file__).resolve().parents[2]


def _utf8_console() -> None:
    """Windows-консоли (cp1251) не хватает на ₽/эмодзи — форсируем UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def _mask_key(value: str) -> str:
    return f"***{value[-4:]}" if len(value) > 4 else "(not set)"


def _pkg_version() -> str:
    try:
        return _metadata_version("grok-critic-mcp")
    except PackageNotFoundError:
        return "unknown"


def _result_payload(result) -> dict[str, Any]:
    """Машиночитаемое представление CritiqueResult для --json."""
    return {
        "success": result.success,
        "error": result.error,
        "text": result.text,
        "review_id": result.review_id,
        "model": result.model,
        "agent_count": result.agent_count,
        "effort": result.effort,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "total_tokens": result.total_tokens,
        "cost_rub": result.cost_rub,
        "cost_is_estimate": result.cost_is_estimate,
        "cached_tokens": result.cached_tokens,
        "reasoning_tokens": result.reasoning_tokens,
    }


# ---------------------------------------------------------------- serve ----
def cmd_serve(_args: argparse.Namespace) -> int:
    """Запуск MCP stdio-сервера (дефолтное поведение без подкоманд)."""
    _server_main()
    return EXIT_OK


# --------------------------------------------------------------- health ----
def cmd_health(args: argparse.Namespace) -> int:
    """Быстрый статус без MCP: конфиг + store; с --ping — реальный запрос баланса."""
    if args.ping:
        result = asyncio.run(health_check())
        usage = result.get("usage_today", {})
        payload = {
            "status": result["status"],
            "model": result["model"],
            "base_url": result["base_url"],
            "issues": result["issues"],
            "pricing": result.get("pricing"),
            "balance_rub": result.get("balance_rub"),
            "usage_today": usage,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Status: {payload['status']}")
            print(f"Model: {payload['model']}")
            print(f"Base URL: {payload['base_url']}")
            if payload["pricing"]:
                for line in _format_pricing_lines(payload["pricing"]):
                    print(line)
            if payload["balance_rub"] is not None:
                print(f"Balance: {_fmt_rub(payload['balance_rub'])}")
            print(_format_usage_today(usage))
            for issue in payload["issues"]:
                print(f"Issue: {issue}")
        return EXIT_OK if payload["status"] == "ok" else EXIT_WARN

    # без --ping: только локальные проверки (мгновенно, без сети)
    problems: list[str] = []
    if not config.api_key.get_secret_value():
        problems.append("POLZA_API_KEY не задан")
    store_path = review_store._dir
    if not _store_writable(store_path):
        problems.append(f"Store недоступен для записи: {store_path}")

    if args.json:
        # --json без --ping: тот же машинный контракт, что у ветки --ping (status + issues);
        # раньше флаг молча игнорировался и печатался текст — скрипты падали на разборе.
        payload = {"status": "ok" if not problems else "error", "mode": "offline", "issues": problems}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK if not problems else EXIT_ERR
    for p in problems:
        print(f"❌ {p}", file=sys.stderr)
    if not problems:
        print("✅ Config OK, store OK (без обращения к API; детали: --ping)")
        return EXIT_OK
    return EXIT_ERR


def _store_writable(path: Path) -> bool:
    """Проверяет, что директорию store'а можно создать и писать в неё."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".probe-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _emit_result(result, as_json: bool) -> int:
    """DRY-вывод CritiqueResult для review/followup: stdout или stderr + exit code."""
    if not result.success:
        print(f"❌ {result.error}", file=sys.stderr)
        return EXIT_ERR
    if as_json:
        print(json.dumps(_result_payload(result), ensure_ascii=False, indent=2))
    else:
        print(_format_result(result))
    return EXIT_OK


# --------------------------------------------------------------- doctor ----
def cmd_doctor(_args: argparse.Namespace) -> int:
    """Диагностика «почему критик отвалился»: чеклист с советами."""
    checks: list[tuple[bool, str]] = []  # (ok, line)

    # 1. Python
    ok = sys.version_info >= (3, 11)
    checks.append((ok, f"Python {'.'.join(map(str, sys.version_info[:3]))} (нужен >= 3.11)"))

    # 2. Версия пакета + git HEAD + шим
    ver = _pkg_version()
    head = "(не git-клон)"
    try:
        proc = subprocess.run(
            ["git", "-C", str(_REPO_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            head = proc.stdout.strip()
    except Exception:
        pass
    shim = shutil.which("grok-critic")
    if not shim:
        # pip user-install кладёт шим в %APPDATA%\Python\Python3XX\Scripts — он может
        # отсутствовать в PATH текущей оболочки, но работать в PowerShell/cmd агента.
        appdata = os.getenv("APPDATA", "")
        probe = Path(appdata) / "Python" / f"Python{sys.version_info.major}{sys.version_info.minor}" / "Scripts" / "grok-critic.exe"
        if probe.is_file():
            shim = f"{probe} (есть, но не на PATH этой оболочки)"
        else:
            shim = "НЕТ (pip install -e .)"
    checks.append((True, f"Пакет grok-critic-mcp {ver}, HEAD {head}, шим CLI: {shim}"))

    # 3. .env
    env_file = Path(_resolve_env_file())
    checks.append((env_file.is_file(), f".env: {env_file}{' ✅' if env_file.is_file() else ' — НЕ НАЙДЕН (POLZA_ENV_FILE?)'}"))

    # 4. Ключ
    key_masked = _mask_key(config.api_key.get_secret_value())
    checks.append((len(config.api_key.get_secret_value()) > 0, f"POLZA_API_KEY: {key_masked}"))

    # 5. Доступность base_url (TCP, прокси-совместимо)
    parts = urlsplit(config.base_url)
    host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=5):
            tcp_ok, tcp_detail = True, f"TCP {host}:{port} — доступен"
    except Exception as exc:
        tcp_ok, tcp_detail = False, f"TCP {host}:{port} — НЕ доступен: {exc}"
    proxies = [v for v in (os.getenv("HTTPS_PROXY"), os.getenv("HTTP_PROXY")) if v]
    if proxies:
        tcp_detail += f" (proxy: {', '.join(proxies)})"
    checks.append((tcp_ok, tcp_detail))

    # 6. Store
    store_path = review_store._dir
    store_ok = _store_writable(store_path)
    checks.append((store_ok, f"Store диалогов: {store_path} {'✅' if store_ok else '— НЕ ПИШЕТСЯ'}"))

    # 7. Лог
    if config.log_file:
        lf = Path(config.log_file)
        state = "✅" if lf.is_file() else "(файл ещё не создан)"
        checks.append((True, f"Лог-файл: {config.log_file} {state} — смотрите `grok-critic logs`"))
    else:
        checks.append((True, "Лог: stderr процесса сервера (POLZA_LOG_FILE пуст)"))

    core_failed = not all([checks[0][0], checks[3][0], tcp_ok, store_ok])
    print("grok-critic doctor:")
    for ok, line in checks:
        print(f"  {'✅' if ok else '❌'} {line}")
    print()
    print("Подсказки:")
    print("  • MCP-клиент кэширует схему tools на сессию: после обновления сервера нужна новая сессия/переподключение.")
    print("  • Сервер работает? Пока MCP мёртв, ревью доступно отсюда: `grok-critic review <файл|-> --agents 4`")
    return EXIT_ERR if core_failed else EXIT_OK


# --------------------------------------------------------------- review ----
def cmd_review(args: argparse.Namespace) -> int:
    """One-shot ревью файла/stdin в обход MCP: `grok-critic review src/foo.py --agents 4`."""
    try:
        if args.path == "-":
            content = sys.stdin.read()
            origin = "stdin"
        else:
            path = Path(args.path).expanduser().resolve()
            if not path.is_file():
                print(f"❌ Файл не найден: {path}", file=sys.stderr)
                return EXIT_ERR
            content = path.read_text(encoding="utf-8", errors="replace")
            origin = str(path)
            if _is_sensitive_file(path):
                print(
                    f"⚠️ ВНИМАНИЕ: {path.name} похож на файл секретов — содержимое уйдёт во внешний API.",
                    file=sys.stderr,
                )
    except OSError as exc:
        print(f"❌ Не удалось прочитать источник: {exc}", file=sys.stderr)
        return EXIT_ERR

    focus = [a.strip() for a in args.focus.split(",") if a.strip()] if args.focus else None
    agent_count = max(1, min(64, args.agents)) if args.agents is not None else None
    logger.info(
        "[CLI][review][REVIEW] origin=%s content_len=%d agents=%s json=%s",
        origin, len(content), agent_count, args.json,
    )
    result = asyncio.run(
        general_review(
            content=content,
            context=args.context,
            agent_count=agent_count,
            focus_areas=focus,
            output_format="json" if args.json_output else None,
        )
    )
    if not result.success:
        print(f"❌ {result.error}", file=sys.stderr)
        return EXIT_ERR
    logger.info(
        "[CLI][review][DONE] review_id=%s tokens=%d cost_rub=%s",
        result.review_id, result.total_tokens,
        f"{result.cost_rub:.2f}" if result.cost_rub is not None else "n/a",
    )
    return _emit_result(result, args.json)


# ------------------------------------------------------------- followup ----
def cmd_followup(args: argparse.Namespace) -> int:
    """Уточняющий вопрос из терминала: по review_id или по файлу/stdin с текстом ревью."""
    previous: str | None = None
    if args.review_id and args.from_source:
        print("❌ Передайте что-то одно: --review-id ИЛИ --from", file=sys.stderr)
        return EXIT_ERR
    if args.review_id:
        # store общий с MCP-сервером — диалог достанет сам server-side followup
        if review_store.load(args.review_id) is None:
            print(
                f"❌ review_id не найден: {args.review_id} (TTL 24ч, store: {review_store._dir})",
                file=sys.stderr,
            )
            return EXIT_ERR
    elif args.from_source:
        try:
            previous = (
                sys.stdin.read()
                if args.from_source == "-"
                else Path(args.from_source).read_text(encoding="utf-8", errors="replace")
            )
        except OSError as exc:
            print(f"❌ Не удалось прочитать {args.from_source}: {exc}", file=sys.stderr)
            return EXIT_ERR
    else:
        print("❌ Нужен источник предыдущего ревью: --review-id <id> или --from <файл|->", file=sys.stderr)
        return EXIT_ERR

    result = asyncio.run(
        critic_followup_fn(
            previous_review=previous,
            question=args.question,
            agent_count=args.agents,
            review_id=args.review_id,
        )
    )
    return _emit_result(result, args.json)


# ----------------------------------------------------------------- logs ----
def cmd_logs(args: argparse.Namespace) -> int:
    """Хвост лога сервера — агент сам читает, почему тот отвалился."""
    if not config.log_file:
        print(
            "Лог-файл не задан (POLZA_LOG_FILE пуст): сервер пишет в stderr своего процесса.\n"
            "Включите файл: POLZA_LOG_FILE=<путь> в .env + reload_config_tool.",
            file=sys.stderr,
        )
        return EXIT_WARN
    path = Path(config.log_file)
    if not path.is_file():
        print(f"❌ Лог-файл не найден: {path}", file=sys.stderr)
        return EXIT_ERR
    # deque: держим только последние tail строк — большой лог не уедет в память
    # (находка ревью rev_4e2fb8bca326)
    tail: deque[str] = deque(maxlen=args.tail)
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            tail.append(line.rstrip("\n"))
    for line in tail:
        print(line)
    return EXIT_OK


# --------------------------------------------------------------- config ----
def cmd_config(args: argparse.Namespace) -> int:
    """Конфиг с маскированным ключом (то же, что reload_config_tool, но из шелла)."""
    payload = {
        "version": _pkg_version(),
        "api_key": _mask_key(config.api_key.get_secret_value()),
        "base_url": config.base_url,
        "model": config.model,
        "agent_count": config.agent_count,
        "timeout_seconds": config.timeout_seconds,
        "log_level": config.log_level,
        "log_file": config.log_file,
        "allow_file_path": config.allow_file_path,
        "allowed_read_dirs": config.allowed_read_dirs,
        "allow_self_update": config.allow_self_update,
        "daily_budget_rub": config.daily_budget_rub,
        "max_concurrent_requests": config.max_concurrent_requests,
        "retry_deadline_seconds": config.retry_deadline_seconds,
        "store_path": str(review_store._dir),
        "env_file": _resolve_env_file(),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    for key, value in payload.items():
        if key == "daily_budget_rub":
            # человекочитаемо: «без лимита» / «500,00 ₽» (в --json остаётся числом)
            value = _fmt_budget_rub(config.daily_budget_rub)
        print(f"{key}: {value}")
    return EXIT_OK


# ----------------------------------------------------------------- main ----
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grok-critic",
        description="grok-critic MCP: терминальный вход для агентов (serve/health/doctor/review/followup/logs/config).",
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="запустить MCP stdio-сервер (то же, что вызов без подкоманды)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("health", help="статус конфигурации/баланса; --ping — реальный запрос к API")
    p.add_argument("--ping", action="store_true", help="обратиться к Polza.AI (баланс, счётчики)")
    p.add_argument("--json", action="store_true", help="машинно-читаемый вывод")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("doctor", help="диагностика: .env, ключ, сеть, store, лог, версии")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("review", help="one-shot ревью файла или stdin ('-') без MCP")
    p.add_argument("path", help="путь к файлу или '-' для stdin")
    p.add_argument("--context", default=None, help="контекст: проект, язык, назначение")
    p.add_argument("--agents", type=int, default=None, help="4=быстро, 16=глубоко (default из конфига)")
    p.add_argument("--focus", default=None, help="'security,performance,SOLID'")
    p.add_argument("--json", action="store_true", help="JSON-результат (токены/стоимость/текст)")
    p.add_argument("--json-output", action="store_true", help="попросить у модели строгий JSON-findings")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("followup", help="уточняющий вопрос по прошлому ревью")
    p.add_argument("question", help="вопрос/контраргумент")
    p.add_argument("--review-id", default=None, help="ID из metadata ревью (store на диске, TTL 24ч)")
    p.add_argument("--from", dest="from_source", default=None, help="файл или '-' (stdin) с текстом ревью")
    p.add_argument("--agents", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_followup)

    p = sub.add_parser("logs", help="хвост лога сервера")
    p.add_argument("--tail", type=int, default=50, help="сколько последних строк (default 50)")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("config", help="показать конфиг (ключ маскирован)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command is None:
            # без подкоманды — совместимое поведение: запускаем MCP stdio-сервер
            return cmd_serve(args)
        return args.func(args)
    except KeyboardInterrupt:
        # Ctrl+C в интерактивном терминале — стандартный код 130 (128+SIGINT)
        print("\n⏹ Прервано пользователем", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
