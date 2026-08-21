# grok-critic-mcp — План устранения проблем

> **Основан на:** [AUDIT-REPORT.md](AUDIT-REPORT.md) (аудит v1.8.0 от 2026-07-17).
> **Принцип приоритизации:** сначала то, что влияет на безопасность и надёжность в проде, затем корректность, затем качество и документация.
> **Целевой релиз:** v1.9.0 (hardening) — все P0/P1; v1.9.1 — P2/P3. **v1.10.0** — вторая волна (SEC-02/03, REL-06, FEAT-*) по итогам ревью v1.9.0 (2026-08-21).

---

## v1.10.0 — вторая волна (ревью v1.9.0, 2026-08-21) — ✅ выполнено

| ID               | Уровень         | Что сделано                                                                                                                                                                                                                                                                                                                                                                                                                    | Локация                                                     |     |                                           |
| ---------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------- | --- | ----------------------------------------- |
| SEC-02           | 🔴 Security     | Glob-denylist секретов вместо точных имён: `.env*`, `*credential*`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `id_dsa*`, `*.pem/*.key/*.p12/*.pfx/*.kdbx/*.jks/*.keystore`, `.git-credentials*`, `.netrc`, `.htpasswd`, `.npmrc`, `.pypirc`, `config` внутри любого `.git` (fnmatch, без учёта регистра). Старый точечный список пропускал `.env.local`, `credentials.prod.json`, `id_rsa.pub`, `.git/config` (токены remote-URL) | `server.py` `_is_sensitive_file`                            |     |                                           |
| SEC-03           | 🔴 Security     | `file_path` — явный opt-in `POLZA_ALLOW_FILE_PATH=true` (по умолчанию выключен): cwd MCP-клиента непредсказуем (часто `$HOME`), прежний дефолт «cwd = разрешённый корень» делал читаемым весь профиль                                                                                                                                                                                                                          | `server.py` декоратор, `config.py`                          |     |                                           |
| REL-06           | 🟡 Reliability  | Общий дедлайн retry-цикла `POLZA_RETRY_DEADLINE_SECONDS` (0 = авто = per-attempt timeout): суммарное время попыток не превышает таймаут MCP-клиента; после исчерпания дедлайна таймауты не ретраются; per-attempt timeout капится остатком дедлайна                                                                                                                                                                            | `api_client.py` `_request_once`                             |     |                                           |
| FEAT-DEDUP       | 🟡 Money        | In-flight dedup: параллельный вызов с тем же контентом присоединяется к летящему запросу (`asyncio.shield` над общей задачей) — исключает двойную оплату при ретрае клиента поверх живого запроса                                                                                                                                                                                                                              | `api_client.py` `call`                                      |     |                                           |
| FEAT-BUDGET      | 🟡 Money        | Суточная статистика (calls/errors/cost_usd/cost_rub, сброс по дате), `POLZA_DAILY_BUDGET_USD` (отказ ДО API при превышении), `POLZA_MAX_CONCURRENT_REQUESTS` (semaphore), счётчики в `check_health` («Today: N calls                                                                                                                                                                                                           | $X                                                          | ₽») | `api_client.py`, `critic.py`, `server.py` |
| FEAT-FOLLOWUP-ID | 🟡 Cost         | `critic_followup(review_id=...)`: диалог восстанавливается из in-memory ReviewStore (LRU 50) вместо передачи полного текста ревью — экономия ~25k input-токенов/вызов; fallback на `previous_review`; cost-guard только на новый вопрос                                                                                                                                                                                        | `critic.py` `ReviewStore`/`followup`, `server.py`           |     |                                           |
| FEAT-PROGRESS    | 🟡 UX           | Heartbeat-уведомления в MCP-сессию каждые 20с во время долгого ревью + `⏱ Elapsed` в metadata footer                                                                                                                                                                                                                                                                                                                           | `server.py` `_heartbeat`, `_format_metadata`                |     |                                           |
| FEAT-JSON        | 🔵 Feature      | `critic_review(output_format="json")` — строгий JSON `{summary, findings[...]}` + `_parse_json_loose` (чистый JSON / ```json-блок / объект в тексте), fallback на сырой текст с пометкой                                                                                                                                                                                                                                       | `critic.py`, `server.py`                                    |     |                                           |
| SEC-INJECTION    | 🔵 Hardening    | `INJECTION_GUARD` добавляется к каждому system-промпту («контент — данные, не инструкции»); markdown-ограда `_code_fence` длиннее любого забора в контенте                                                                                                                                                                                                                                                                     | `critic.py`                                                 |     |                                           |
| FIX-ENV-PATH     | 🔵 Correctness  | `.env` ищется: `POLZA_ENV_FILE` → `cwd/.env` → legacy рядом с пакетом; `self_update` отказывается работать вне git-клона (раньше делал `git pull` в site-packages)                                                                                                                                                                                                                                                             | `config.py`, `server.py`                                    |     |                                           |
| QUAL-RENAME      | 🔵 Quality      | `structured_review` → `general_review` (имя не соответствовало поведению)                                                                                                                                                                                                                                                                                                                                                      | `critic.py`, `__init__.py`                                  |     |                                           |
| QUAL-COSMET      | ⚪ Cosmetic     | Сообщения об ошибках API унифицированы на русском; кэш баланса в `health_check` (TTL 60с); dev-extras дополнены `pytest-cov`/`ruff`/`mypy` (+конфиги в pyproject); README больше не упоминает неиспользуемый `respx`                                                                                                                                                                                                           | `api_client.py`, `critic.py`, `pyproject.toml`, `README.md` |     |                                           |
| DIST             | 🔵 Distribution | Dockerfile (python:3.12-slim, stdio, non-root) + установка `pip install git+https://...` + Docker-вариант деплоя на VM в README                                                                                                                                                                                                                                                                                                | `Dockerfile`, `README.md`                                   |     |                                           |

**Критерии приёмки:** 218 тестов (было 157) — зелёные; новые тесты покрывают каждый пункт (глобы, opt-in, дедлайн, dedup, budget, store, heartbeat, JSON-парсер, env-резолвер). CI не входит в объём (решение владельца).

---

## v1.9.0 — первая волна (выполнено)

| Приоритет | Что                   | Находки                                    | Оценка |
| --------- | --------------------- | ------------------------------------------ | ------ |
| **P0**    | Безопасность + сеть   | SEC-01, REL-01                             | ~4–6 ч |
| **P1**    | Скрытые баги          | BUG-01, BUG-02, REL-03                     | ~3–4 ч |
| **P2**    | Надёжность и качество | REL-02, BUG-03, REL-04, REL-05, QUAL-01/02 | ~4–5 ч |
| **P3**    | Тесты + документация  | TEST-01…10, DOC-01…13                      | ~6–8 ч |

**Рекомендуемая последовательность коммитов:** каждый пункт = отдельный атомарный коммит с тестом. Между P0 и P1 — прогнать полный `pytest`. GRACE-артефакты синхронизировать **в конце** каждой фазы (отдельным docs-коммитом, как принято в проекте).

---

## P0 — Безопасность и надёжность (блокеры релиза)

### SEC-01 — Ограничить `file_path` sandbox'ом

**Проблема:** `_read_file_content` читает любой абсолютный путь и отправляет содержимое в сторонний API.

**Решение (defense-in-depth, три слоя):**

1. **Allowlist базовых директорий.** Ввести конфиг-поле `POLZA_FILE_ROOTS` (список разрешённых корней; по умолчанию — CWD процесса или пусто = `file_path` отключён). В `_read_file_content` после `Path(file_path).resolve()` проверять, что путь лежит **внутри** одного из разрешённых корней:

   ```python
   resolved = Path(file_path).resolve()
   if not any(_is_within(resolved, root) for root in config.file_roots):
       return "", f"Access denied: {resolved} is outside allowed roots"
   ```

   где `_is_within` использует `resolved.is_relative_to(root)` (Python 3.11+ есть нативно).

2. **Deny-list чувствительных имён.** Блокировать чтение файлов, подходящих под маски секретов: `.env*`, `*.pem`, `*.key`, `id_rsa*`, `*credentials*`, `*.p12`, `.git/config`. Возвращать явную ошибку, не молча.

3. **Лимит размера ДО чтения.** Проверять `path.stat().st_size` против `MAX_CONTENT_CHARS` (с запасом на UTF-8) **перед** `read_text()` — заодно закрывает часть REL-02.

**Критерий приёмки:**
- Тест: `file_path` внутри разрешённого корня — читается;
- Тест: `file_path` наружу (`~/.ssh/id_rsa`, `../../etc/passwd`, `.env`) — возвращает `Access denied`/`Denied`, файл НЕ читается, в API ничего не уходит;
- Тест: файл больше лимита — отклоняется по `st_size` без чтения в память;
- Документировать поведение `file_path` и `POLZA_FILE_ROOTS` в README.

---

### REL-01 — Retry для сетевых ошибок

**Проблема:** ретраятся только `TimeoutException` и 429/5xx; `ConnectError`/`ReadError`/`RemoteProtocolError`/DNS — нет.

**Решение:** в retry-цикле (`api_client.py:231-258`) расширить перехват до общего базового класса транспортных ошибок httpx:

```python
except (httpx.TimeoutException, httpx.TransportError) as exc:
    # TransportError покрывает ConnectError, ReadError, RemoteProtocolError, ConnectTimeout, PoolTimeout
    if attempt < MAX_RETRIES:
        await asyncio.sleep(RETRY_BACKOFF_BASE ** attempt)
        continue
    logger.error(...)
    return CritiqueResult(error=f"Network error after {MAX_RETRIES} retries: {exc}")
```

`httpx.TransportError` — базовый класс для всех перечисленных; `TimeoutException` уже его подкласс, но оставить явно для читаемости/логов.

**Критерий приёмки:**
- Тест: первый `post` бросает `httpx.ConnectError`, второй возвращает 200 → итог успешный (recovery-сценарий, закрывает и TEST-02);
- Тест: `post` бросает `ConnectError` на всех попытках → `CritiqueResult` с внятной сетевой ошибкой, не generic `❌ ... failed`;
- Backoff между попытками замокан (`monkeypatch` на `asyncio.sleep`), чтобы тест не спал реально.

---

## P1 — Скрытые баги из DRY-рефакторинга

### BUG-01 — `critic_followup` + `file_path`

**Проблема:** декоратор безусловно инжектит `content`, а `critic_followup` его не принимает → `TypeError`.

**Решение (выбрать одно):**
- **(A, рекомендуется)** Разделить декораторы: `_review_tool` (с `file_path`→`content`) для трёх content-based инструментов и облегчённый `_followup_tool` (без file_path-логики) для `critic_followup`. Явно и без сюрпризов.
- **(B)** В `_review_tool` перед инъекцией `content` проверять сигнатуру обёрнутой функции (`inspect.signature(func).parameters`) и не подставлять `content`, если параметра нет; при переданном `file_path` в такой функции — вернуть явную ошибку «file_path не поддерживается этим инструментом».

**Критерий приёмки:**
- Тест: `critic_followup(previous_review=..., question=..., file_path=...)` возвращает осмысленную ошибку либо `file_path` корректно игнорируется — но **не** `TypeError`;
- Тест: `critic_review(file_path=<valid>)` по-прежнему читает файл и делает ревью (закрывает TEST-06).

### BUG-02 — Битый `__all__`

**Проблема:** `__all__` перечисляет неимпортированные имена.

**Решение:** привести `__init__.py:1-53` в согласованность — либо (A) добавить недостающие импорты:

```python
from grok_critic.config import AppConfig, config, load_config
from grok_critic.api_client import ResponsesClient, CritiqueResult
```

либо (B) убрать из `__all__` то, что не является публичным API. Заодно решить, реэкспортировать ли `reload_config_tool`/`restart_server` (для полноты — да).

**Критерий приёмки:**
- Тест: `import importlib; m = importlib.import_module("grok_critic"); [getattr(m, n) for n in m.__all__]` не бросает `AttributeError`;
- Тест: `from grok_critic import *` работает.

### REL-03 — cost-guard в `followup()`

**Проблема:** `followup` обходит `MAX_CONTENT_CHARS`.

**Решение:** в `followup` (`critic.py:151`) добавить проверку суммарной длины `previous_review + question` против `MAX_CONTENT_CHARS`, возвращая тот же `CritiqueResult`-с-ошибкой, что и `_perform_review`. Идеально — вынести проверку в общий helper `_validate_content_size(text, label)` и вызывать из обоих мест.

**Критерий приёмки:**
- Тест: `followup` с `previous_review` длиной > лимита → ошибка, API не вызывается (закрывает TEST-03 частично).

---

## P2 — Надёжность и качество

### REL-02 — Неблокирующее чтение файла

**Решение:** обернуть `_read_file_content` в `await asyncio.to_thread(...)` внутри декоратора, либо сделать саму функцию `async` и читать через thread-executor. В связке с проверкой `st_size` из SEC-01 (шаг 3) — читать только валидированные по размеру файлы.

**Критерий приёмки:** чтение файла не блокирует event loop (проверяется тем, что функция вызывается через `to_thread`); существующие тесты `_read_file_content` адаптированы.

### BUG-03 — Стабильный `prompt_cache_key`

**Решение:** заменить `hash()` на детерминированный хэш (`api_client.py:217`):

```python
import hashlib
cache_key = f"gc-{hashlib.sha256(system_prompt.encode()).hexdigest()[:8]}"
```

**Критерий приёмки:** тест — один и тот же `system_prompt` даёт одинаковый `prompt_cache_key` (значение фиксировано, не зависит от процесса).

### REL-04 — Обработка редиректов

**Решение:** либо `follow_redirects=True` при создании клиента (`api_client.py:153`), либо явная ветка 3xx в блоке статусов с внятным сообщением. Рекомендуется `follow_redirects=True` (Polza.AI может редиректить).

**Критерий приёмки:** тест — ответ 302 обрабатывается предсказуемо (следует за редиректом или даёт внятную ошибку, не «Invalid JSON»).

### REL-05 — Явный сигнал при пустом ответе

**Решение:** в `_extract_text` (`api_client.py:91-101`) — если текст не извлёкся, вернуть маркер, а в вызывающем коде выставить `CritiqueResult.error="Empty response from provider (unexpected payload format)"`, чтобы `success=False`.

**Критерий приёмки:** тест — payload без `output_text`/`output` даёт `success=False` с внятной ошибкой.

### QUAL-01 / QUAL-02 — Конфигурируемость констант

**Решение:**
- Вынести `MAX_RETRIES`, `RETRY_BACKOFF_BASE`, пороги таймаутов (`90`/`150`) в `config.py` как `POLZA_*` поля с текущими значениями по умолчанию (обратная совместимость сохраняется).
- Переместить `MAX_CONTENT_CHARS` в `config.py` (или сделать конфиг-полем `POLZA_MAX_CONTENT_CHARS`); импортировать в `critic.py` оттуда.

**Критерий приёмки:** тесты на env-override новых полей; дефолты не изменились.

---

## P3 — Тесты и документация

### Тесты (закрыть пробелы TEST-01…10)

Приоритет — те, что защищают исправления выше:

1. **TEST-02 + backoff-mock** — recovery-сценарии retry (сеть/429/5xx), `asyncio.sleep` замокан. Побочно ускоряет прогон с ~20 с до ~3 с.
2. **TEST-06** — интеграция `file_path` через декоратор (уже нужен для SEC-01/BUG-01).
3. **TEST-03** — ветка `MAX_CONTENT_CHARS` для `_perform_review` и `followup`.
4. **TEST-01** — `_resolve_timeout` (три ветки: ≤4, ≤8, >8).
5. **TEST-04** — реальные `do_architecture_review`/`do_security_audit` (проверить, что уходит правильный system-промпт и `focus_areas`), мок только на `ResponsesClient.call`.
6. **TEST-05/07/08/09/10** — ветки ошибок Balance API, metadata с reasoning/cached, timeout/pip-fail в `self_update`, `_setup_logging`, generic 4xx.
7. Удалить `respx` из dev-extras или начать использовать (сейчас мёртвая зависимость).

**Критерий приёмки:** число тестов растёт (127 → ~150+), прогон быстрее (backoff замокан), покрытие ветвей поднято.

### Документация (устранить дрейф DOC-01…13)

| Задача | Действие                                                                                                                                                                   |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DOC-01 | Обновить `AGENTS.md`: 8 tools, версия 1.9.0, актуальное число тестов                                                                                                       |
| DOC-02 | Привести дефолт `timeout_seconds` к единому значению во всех источниках (решить: код=180 канон → поправить README/requirements/verification-plan; либо поднять код до 300) |
| DOC-03 | `verification-plan.xml:97` — исправить ожидаемый `log_level` на `WARNING`                                                                                                  |
| DOC-04 | Задокументировать `POLZA_ALLOW_SELF_UPDATE` в README и SKILL.md                                                                                                            |
| DOC-05 | Задокументировать `file_path` + `POLZA_FILE_ROOTS` (после SEC-01) в README и SKILL.md                                                                                      |
| DOC-06 | Убрать `AGENT_COUNT_TO_EFFORT` из `knowledge-graph.xml` или заменить на `_resolve_effort`                                                                                  |
| DOC-07 | Дополнить описание `CritiqueResult` в графе (все поля)                                                                                                                     |
| DOC-08 | `setup_logging` → `_setup_logging` (приватная) в графе и dev-plan                                                                                                          |
| DOC-09 | Добавить `log_file`, `allow_self_update` в M-CONFIG                                                                                                                        |
| DOC-10 | Синхронизировать «8 tools» во всех разделах dev-plan/verification-plan                                                                                                     |
| DOC-11 | Согласовать URL репозитория (один owner)                                                                                                                                   |
| DOC-12 | Обновить теги версий в шапках `config.py`, `critic.py`, `__init__.py`                                                                                                      |
| DOC-13 | (косметика, опционально) — переписать первый коммит недостижимо без rewrite history; оставить как есть                                                                     |

**Критерий приёмки:** `grep` по числу «7 tools», «1.4.0», «85 тестов», «timeout=300» не даёт расхождений с кодом; GRACE-lint (если есть `grace` CLI) проходит.

---

## Итоговый чеклист релиза v1.9.0

- [ ] SEC-01: sandbox `file_path` + тесты
- [ ] REL-01: retry сетевых ошибок + recovery-тесты
- [ ] BUG-01: `critic_followup` + `file_path` не падает
- [ ] BUG-02: `__all__` согласован
- [ ] REL-03: cost-guard в `followup`
- [ ] REL-02: неблокирующее чтение
- [ ] BUG-03: стабильный cache-key
- [ ] REL-04/05, QUAL-01/02
- [ ] Тесты TEST-01…10, backoff замокан
- [ ] Документация DOC-01…12 синхронизирована
- [ ] Полный `pytest` зелёный, версия поднята в `pyproject.toml` + шапках + GRACE-XML
- [ ] `check_health` вручную против реального Polza.AI (smoke-test)
