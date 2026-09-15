# Review Brief — для внешнего ревью (Claude)

> **Кому читать:** ревьюеру-агенту (Claude Code), которому поручено ревью этого репозитория.
> **Дата:** 2026-09-16. **Версия на момент брифа:** 1.11.1 (`f073c37`).
> **Режим:** review-only. Код не менять; находки — в отчёт с severity и путями `файл:строка`.

## 1. Что это за проект

MCP-сервер `grok-critic` (Python 3.11+, FastMCP, stdio): оборачивает модель `grok-4.20-multi-agent` (xAI через Polza.AI, Responses API) как «внешнего критика» для AI-агентов. 8 MCP tools + терминальный CLI. Используется в Kilo Code / ZCode / Claude Code как субагент-критик.

**Карта:** `docs/knowledge-graph.xml` — актуальная карта модулей (M-CONFIG → M-API → M-CRITIC → M-SERVER, + M-CLI). `AGENTS.md` — протокол GRACE (маркеры START_BLOCK, контракты модулей). Ключевые исходники: `src/grok_critic/{config,api_client,critic,server,cli}.py` (~1900 строк суммарно), тесты `tests/` (249 шт., всё мокается, сеть не нужна).

## 2. Как проверить сборку

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q          # ожидание: 249 passed
python -m ruff check src tests      # ожидание: чисто
node scripts/align-md-tables.mjs --check $(git ls-files '*.md')   # docs-lint
```

## 3. История релевантных изменений (ревьюить в первую очередь)

Последний полный внешний аудит — v1.8.0 (`docs/AUDIT-REPORT.md`, все находки закрыты в v1.9.0, см. `docs/REMEDIATION-PLAN.md`). После него три волны — **свежий код, обзоренный только «самим собой»**:

| Диапазон                                    | Что появилось                                                                                                                                                                                                                                                    |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| v1.10.0 (`2dc084a` + `e9d60c1`)             | glob-denylist секретов для `file_path` (SEC-02), opt-in `POLZA_ALLOW_FILE_PATH` (SEC-03), retry-дедлайн (REL-06), in-flight dedup, semaphore + дневной бюджет, followup по review_id, heartbeat + Elapsed, JSON-режим, injection-guard, FIX-ENV-PATH, Dockerfile |
| v1.11.0 (`91a9680`..`bc954b6`)              | CLI (`src/grok_critic/cli.py`: serve/health/doctor/review/followup/logs/config), дисковый ReviewStore                                                                                                                                                            |
| v1.11.1 (`91a9680` + `07114ca` + `f073c37`) | ReviewStore переписан на **per-file layout** (`db/reviews/rev_*.json`) после глубокого саморевью (race condition общего JSON)                                                                                                                                    |

## 4. Вопросы, где особенно ценно второе мнение

1. **Per-file ReviewStore** (`critic.py`, блок REVIEW_STORE): действительно ли конкурентно-безопасен на Windows? Смотреть: `os.replace` поверх открытого файла, гонки `_prune()` между процессами (двое удаляют/пишут одновременно), поведение при переполнении диска, `_entry_path` sanitization.
2. **`_review_tool` + FastMCP** (`server.py`): корректность декоратора — `functools.wraps`/сигнатуры для генерации MCP-схемы, inject `ctx: Context`, жизненный цикл heartbeat-таски (утечки при отмене запроса клиентом?), поведение при `reload_config` посреди запроса.
3. **Retry + дедлайн** (`api_client.py`, `_request_once`): корректность расчёта `remaining`, cap per-attempt timeout, взаимодействие дедлайна с semaphore и dedup (дедлок невозможен?).
4. **In-flight dedup** (`api_client.py`, `call`): `asyncio.shield` поверх общей задачи — утечки задач при отмене всех подписчиков? Корректность double-check после semaphore.
5. **Sandbox `file_path`** (`server.py`, `_read_file_content`/`_is_sensitive_file`): `resolve()` + `is_relative_to()` + glob-denylist — обходы (symlink, регистр, Unicode, альтернативные потоки NTFS), TOCTOU между `exists/stat/read`.
6. **CLI как инструмент агента** (`cli.py`): опасные паттерны при вызове из Bash LLM-агентом (инъекции через имена файлов/параметры, смешение stdout/stderr, парсинг `--json`), корректность exit codes (0/1/2/130).

## 5. Осознанные решения — не считать находками (либо оспаривать аргументированно)

- **CLI не применяет sandbox `file_path`** — by design: владелец сам читает файл, CLI только предупреждает о секретных именах.
- **Ошибки API на русском**, часть сообщений CLI тоже — согласованно, для русскоязычных агентов.
- **Store хранит диалоги plaintext 24ч** — задокументировано (README, `db/` вне git); шифрование осознанно не делается.
- **Путь store в сообщении об ошибке CLI** — локальный инструмент владельца.
- **`structured_review` переименован в `general_review`** без back-compat алиаса — осознанный breaking (внутренний проект).
- **HTTP-detached транспорт (фаза 2)** — отложен, не предлагать как «отсутствие».
- **mypy конфиг есть, прогон не выполнялся** (пакет не установлен) — можно прогнать, если доступен.

## 6. Формат отчёта

- Резюме (3–5 строк) → таблица находок: ID / severity (🔴🟡🔵⚪) / что / `файл:строка` / почему / как чинить.
- Отдельно: «что уже хорошо» и ответы на вопросы из секции 4 (по каждому — вердикт ok/проблема).
- Язык отчёта — русский. Пожелания к архитектуре — с учётом ограничения: **новых runtime-зависимостей нет и не планируется** (stdlib + mcp + httpx + pydantic(-settings) + python-dotenv).
