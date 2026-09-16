# grok-critic-mcp — Детальный аудит-отчёт

> **Статус: находки этого аудита закрыты в v1.9.0–v1.11.2; история устранения — [REMEDIATION-PLAN.md](REMEDIATION-PLAN.md).**

> **Версия проекта на момент аудита:** 1.8.0 (`pyproject.toml`, HEAD `6abc4e7`)
> **Дата аудита:** 2026-07-17
> **Метод:** статический анализ всех модулей + GRACE-артефактов + документации, живой прогон `pytest` (127/127 passed), точечные repro-запуски подтверждающие баги.
> **Область:** `src/grok_critic/*`, `tests/*`, `docs/*.xml`, `README.md`, `skill/SKILL.md`, `AGENTS.md`, git-история.

---

## 1. Резюме

`grok-critic-mcp` — MCP-сервер-обёртка, экспонирующая модель `grok-4.20-multi-agent` (xAI, через прокси Polza.AI) как «внешнего критика» для AI-агентов. Проект зрелый и аккуратный: чистая четырёхслойная архитектура, GRACE-дисциплина, 127 проходящих тестов (≈треть кодовой базы), продуманная защита API-ключа.

Аудит выявил **1 критичную проблему безопасности**, **1 критичную проблему надёжности**, **несколько багов среднего уровня из недавнего DRY-рефакторинга** и **системный дрейф документации**. Ни одна проблема не блокирует работу «счастливого пути», но три из них влияют на безопасность и предсказуемость в реальной эксплуатации.

### Сводная таблица находок

| ID         | Уровень | Категория   | Краткое описание                                                             | Локация                                |
| ---------- | ------- | ----------- | ---------------------------------------------------------------------------- | -------------------------------------- |
| SEC-01     | 🔴 HIGH | Security    | Чтение произвольного файла через `file_path` → эксфильтрация в сторонний API | `server.py:75-88, 120-127`             |
| REL-01     | 🔴 HIGH | Reliability | Retry не покрывает сетевые ошибки (только timeout и HTTP-статусы)            | `api_client.py:231-258`                |
| BUG-01     | 🟡 MED  | Correctness | `critic_followup` + `file_path` → `TypeError` (подтверждено repro)           | `server.py:120-127` vs `191-195`       |
| REL-02     | 🟡 MED  | Reliability | Блокирующий `read_text()` в async-хендлере                                   | `server.py:83`                         |
| REL-03     | 🟡 MED  | Reliability | `followup()` обходит лимит `MAX_CONTENT_CHARS`                               | `critic.py:140-177`                    |
| BUG-02     | 🟡 MED  | Correctness | `__all__` ссылается на неимпортированные имена → `import *` падает           | `__init__.py:31-53`                    |
| BUG-03     | 🔵 LOW  | Correctness | Нестабильный `prompt_cache_key` (`hash()` рандомизирован)                    | `api_client.py:217`                    |
| REL-04     | 🔵 LOW  | Reliability | Нет `follow_redirects`, 3xx → `JSONDecodeError`                              | `api_client.py:153`                    |
| REL-05     | 🔵 LOW  | Reliability | `_extract_text` тихо возвращает `""` при `success=True`                      | `api_client.py:91-101`                 |
| QUAL-01    | 🔵 LOW  | Quality     | Hardcoded `MAX_RETRIES`/backoff/таймауты мимо конфига                        | `api_client.py:32-33, 81-83`           |
| QUAL-02    | 🔵 LOW  | Quality     | `MAX_CONTENT_CHARS` объявлена в `api_client.py`, используется в `critic.py`  | `api_client.py:25`                     |
| DOC-01…13  | ⚪ DOC  | Docs drift  | Дрейф документации (см. раздел 6)                                            | `AGENTS.md`, `README.md`, `docs/*.xml` |
| TEST-01…10 | ⚪ TEST | Coverage    | Пробелы в тестовом покрытии (см. раздел 7)                                   | `tests/*`                              |

---

## 2. Архитектура (как есть)

Строго линейная четырёхслойка, каждый слой = один модуль = один тест-файл. Импорты идут только «вниз».

```
config.py       M-CONFIG  (UTILITY)      — pydantic-settings, POLZA_* env, SecretStr, hot-reload
   ▲ импортируют все
api_client.py   M-API     (INTEGRATION)  — httpx.AsyncClient → Polza.AI /responses, retry, usage/cost
   ▲
critic.py       M-CRITIC  (CORE_LOGIC)   — 4 system-промпта, orchestration, health_check
   ▲
server.py       M-SERVER  (ENTRY_POINT)  — FastMCP, stdio, 8 tools, декоратор _review_tool
```

**Точка входа:** `main()` → `server.run(transport="stdio")` (`server.py:424-426`). Console-script `grok-critic = "grok_critic.server:main"` (`pyproject.toml`).

**Runtime-зависимости:** `mcp>=1.6.0`, `httpx>=0.28.0`, `python-dotenv>=1.1.0`, `pydantic>=2.10.0`, `pydantic-settings>=2.7.0`. Dev: `pytest`, `pytest-asyncio`, `respx` (последняя — заявлена, но фактически не используется, см. TEST).

### Инструменты (8)

| #   | Имя                   | Сигнатура                                         | `_review_tool`? | Что делает                                         |
| --- | --------------------- | ------------------------------------------------- | --------------- | -------------------------------------------------- |
| 1   | `critic_review`       | `(content, context?, agent_count?, focus_areas?)` | да              | общее ревью кода                                   |
| 2   | `critic_followup`     | `(previous_review, question, agent_count?)`       | да              | вопрос к готовому ревью                            |
| 3   | `check_health`        | `()`                                              | нет             | статус + баланс Polza.AI (₽)                       |
| 4   | `architecture_review` | `(content, context?, agent_count?)`               | да              | архитектурное ревью                                |
| 5   | `security_audit`      | `(content, context?, agent_count?)`               | да              | security-аудит (🔴🟡🟠🔵)                          |
| 6   | `reload_config_tool`  | `()`                                              | нет             | горячая перезагрузка `.env`                        |
| 7   | `self_update`         | `()`                                              | нет             | `git pull`+`pip install`+`os._exit(0)` (за флагом) |
| 8   | `restart_server`      | `(reason?)`                                       | нет             | `os._exit(0)`, клиент поднимет заново              |

### Декоратор `_review_tool` (`server.py:106-143`)

Централизует пять сквозных забот трёх review-инструментов + followup:
1. клэмп `agent_count` через `_validate_agent_count` (1–64);
2. разрешение `file_path` → `content` (читает файл, подставляет как контент);
3. structured-логирование вызова (`content_len`, `agent_count`);
4. `try/except` с `logger.exception` (stack trace) и унифицированной строкой ошибки;
5. форматирование результата через `_format_result` → текст + metadata footer.

Именно пункт 2 — источник SEC-01 и BUG-01.

---

## 3. Что сделано хорошо (подтверждено чтением)

- **`SecretStr` для `api_key`** (`config.py:32`) + `.get_secret_value()` ровно в 3 точках (`api_client.py:174`, `critic.py:190`, `server.py:303`). Ни одного случая передачи сырого ключа в `logger.*`. Маскирование в `reload_config_tool` (`server.py:304`).
- **DoS/cost-guard `MAX_CONTENT_CHARS=100_000`** применён ко всем трём основным путям ревью (`critic.py:94-100`).
- **Двойной клэмп `agent_count`** — pydantic (`config.py:35`, `ge=1, le=64`) + защитно в декораторе (`server.py:91-99`).
- **`self_update` выключен по умолчанию** за explicit-opt-in флагом (`config.py:41`, gate `server.py:336-337`) — разумный defense-in-depth для тула, делающего `pip install` и `os._exit(0)`.
- **Structured logging** по паттерну `[Module][function][BLOCK] message`, без секретов.
- **Hot-reload с in-place обновлением** (`config.py:96-116`) через `object.__setattr__` — все ранее импортировавшие `config` видят новые значения без рестарта; закрывается stale HTTP-клиент.
- **127/127 тестов проходят** (живой прогон: `127 passed in 20.26s`).

---

## 4. Проблемы безопасности

### SEC-01 (🔴 HIGH) — Чтение произвольного файла через `file_path`

**Локация:** `server.py:75-88` (`_read_file_content`), инъекция в `server.py:120-127`.

```python
path = Path(file_path).resolve()          # ← нет allowlist / базовой директории
...
content = path.read_text(encoding="utf-8", errors="replace")
```

Декоратор `_review_tool` принимает недокументированный `file_path` для любого из `critic_review`/`architecture_review`/`security_audit`, читает файл по **произвольному абсолютному пути** и подставляет содержимое как `content`, которое затем **целиком уходит в сторонний API Polza.AI** (`critic.py:69`, без экранирования).

**Сценарий эксплуатации:** SKILL.md предписывает агентам обязательно вызывать критика для «существенного кода». Prompt-injection в проверяемом файле или инструкция пользователю вида «проверь файл `~/.ssh/id_rsa`» превращает легитимный инструмент в канал эксфильтрации секретов (`.env`, приватные ключи, `credentials.json`) на внешний хост.

**Почему серьёзно:** обход происходит через штатную функцию, без признаков атаки в логах (логируется лишь `content_len`), и данные покидают периметр.

### Проверка утечки секретов в логи — ✅ чисто

Отдельно проверены все `logger.*` вызовы во всех модулях: `api_key` нигде не логируется. Единственный нюанс — `self._api_key = config.api_key.get_secret_value()` (`api_client.py:174`) хранит ключ как обычную `str` в атрибуте инстанса (нужно для заголовка `Authorization`). Это не текущий баг, но точка риска при будущем добавлении debug-дампа объекта.

---

## 5. Проблемы надёжности и корректности

### REL-01 (🔴 HIGH) — Retry не покрывает сетевые ошибки

**Локация:** `api_client.py:231-258`. В retry-цикле ловится только `except httpx.TimeoutException` (стр. 235) и проверяется `resp.status_code in (429, *range(500,600))` (стр. 250). `httpx.ConnectError`, `httpx.ReadError`, `httpx.RemoteProtocolError`, DNS-сбои **не перехватываются** — пробрасываются мимо retry и гасятся generic `except Exception` в `server.py:139`, без backoff. При нестабильной сети заявленная в `requirements.xml` политика «retry ≤2» фактически не работает для самого частого класса ошибок.

### BUG-01 (🟡 MED) — `critic_followup` + `file_path` → `TypeError` (подтверждено repro)

**Локация:** `server.py:120-127` (декоратор безусловно кладёт `kwargs["content"]`) против `server.py:191-195` (сигнатура `critic_followup(previous_review, question, agent_count)` — параметра `content` нет). Живой вызов через `.fn` с `file_path` даёт `TypeError: critic_followup() got an unexpected keyword argument 'content'`, замаскированный под невнятное `❌ critic_followup failed: ...`. Grep по `tests/` на `file_path` — **0 совпадений**: ветка не покрыта, баг не пойман.

### REL-02 (🟡 MED) — Блокирующий I/O в async-хендлере

**Локация:** `server.py:83`. `path.read_text(...)` выполняется прямо в `async def wrapper` без `asyncio.to_thread`/executor → блокирует единственный event loop на время чтения. Плюс лимит `MAX_CONTENT_CHARS` проверяется **после** полного чтения файла в память — файл любого размера сначала читается целиком.

### REL-03 (🟡 MED) — `followup()` без cost-guard

**Локация:** `critic.py:140-177`. `_perform_review` проверяет `len(content) > MAX_CONTENT_CHARS` (`critic.py:94`), а `followup` — только непустоту (`critic.py:151`). Гигантский `previous_review`/`question` уйдёт в платный API без ограничения. Тот же DoS/cost-вектор, что закрыт для остальных путей.

### BUG-02 (🟡 MED) — Битый `__all__` в `__init__.py` (подтверждено запуском)

**Локация:** `__init__.py:31-53`. `__all__` перечисляет `AppConfig`, `CritiqueResult`, `ResponsesClient`, `config`, `load_config`, которые в файле **не импортируются**. Запуск `python -c "from grok_critic import *"` → `AttributeError: module 'grok_critic' has no attribute 'AppConfig'`. Также `reload_config_tool` и `restart_server` (реальные MCP-tools) не реэкспортированы.

### BUG-03 (🔵 LOW) — Нестабильный `prompt_cache_key`

**Локация:** `api_client.py:217` — `f"gc-{hash(system_prompt) & 0xFFFFFFFF:x}"`. Встроенный `hash()` для `str` рандомизирован per-process (`PYTHONHASHSEED`) → один и тот же system-промпт даёт разный cache-key после каждого рестарта, убивая prompt caching (заявленную фичу v1.6.0) после каждого `restart_server`/`self_update`.

### REL-04 (🔵 LOW) — Нет `follow_redirects`

**Локация:** `api_client.py:153`. `httpx.AsyncClient` без `follow_redirects=True`; блок статусов (`api_client.py:279-327`) не имеет ветки для 3xx → редирект провалится в `resp.json()` (стр. 330) с `JSONDecodeError` → неинформативное «Invalid JSON response». Маловероятно, но нечисто.

### REL-05 (🔵 LOW) — Тихий пустой успех `_extract_text`

**Локация:** `api_client.py:91-101`. При неожиданном формате ответа функция возвращает `""`, а `CritiqueResult.success` остаётся `True` (`error=""`) → вызывающий агент получает пустое ревью без сигнала об ошибке.

### QUAL-01 / QUAL-02 (🔵 LOW) — Конфигурационная несогласованность

- `MAX_RETRIES=2`, `RETRY_BACKOFF_BASE=2.0` (`api_client.py:32-33`) и магические `90`/`150` в `_resolve_timeout` (`api_client.py:81,83`) захардкожены, тогда как соседние параметры вынесены в `POLZA_*`.
- `MAX_CONTENT_CHARS` объявлена в `api_client.py:25`, но используется только в `critic.py` — логически чужая этому модулю.

---

## 6. Дрейф документации

Ядро GRACE синхронно с кодом (4 модуля, контракты, версии XML = 1.8.0, зависимости, `SecretStr`, декоратор). Но накопились расхождения:

| ID     | Уровень | Расхождение                                                                                                                                                           |
| ------ | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DOC-01 | 🔴      | `AGENTS.md:7` — «7 tools, версия 1.4.0, 85 тестов». Реально: 8 tools, 1.8.0, 127 тестов. Файл отстал на 4 релиза.                                                     |
| DOC-02 | 🟡      | Дефолт `timeout_seconds`: `README.md:108,439`, `requirements.xml:38`, `verification-plan.xml:97` говорят `300`; код (`config.py:36`) и тест `test_config.py` — `180`. |
| DOC-03 | 🟡      | Дефолт `log_level`: `verification-plan.xml:97` требует `INFO`; код — `WARNING` (`config.py:37`, подтверждено тестом).                                                 |
| DOC-04 | 🟡      | Флаг `POLZA_ALLOW_SELF_UPDATE` не упомянут ни в README, ни в SKILL.md — `self_update` описан как «просто работает», а по умолчанию выключен.                          |
| DOC-05 | 🟡      | Параметр `file_path` не задокументирован ни в README, ни в SKILL.md (grep — 0 совпадений).                                                                            |
| DOC-06 | ⚪      | `knowledge-graph.xml:35` — константа `AGENT_COUNT_TO_EFFORT`, которой в коде нет (реально функция `_resolve_effort`).                                                 |
| DOC-07 | ⚪      | `knowledge-graph.xml:26` неполно описывает `CritiqueResult` (нет `input/output/total_tokens`, `reasoning_tokens`, `error`, `success`).                                |
| DOC-08 | ⚪      | `knowledge-graph.xml:15` / `development-plan.xml:29` документируют `setup_logging` как публичный экспорт; реально `_setup_logging` (приватная).                       |
| DOC-09 | ⚪      | Поля `log_file`, `allow_self_update` (`config.py:38,41`) не упомянуты в GRACE-артефактах.                                                                             |
| DOC-10 | ⚪      | Внутреннее противоречие «7 tools» (`development-plan.xml:183`, `verification-plan.xml:195-202`) vs «8 tools» (те же файлы в других разделах).                         |
| DOC-11 | ⚪      | Разные репозитории: `README.md:77` → `github.com/a-bondartsov/...`; `development-plan.xml:207` → `github.com/Bondartsov/...`.                                         |
| DOC-12 | ⚪      | Теги версий в шапках: `config.py` 1.6.0, `critic.py` 1.7.0, `__init__.py` 1.6.0 — при фактическом 1.8.0.                                                              |
| DOC-13 | ⚪      | Первый коммит `4b3aac9` содержит иероглифы `初始` в message (косметика).                                                                                              |

---

## 7. Тестовое покрытие

**Живой прогон:** `python -m pytest tests/ -q` → **127 passed** (config 33, critic 21, server 36, api_client 37). Мок-стратегия: `unittest.mock` (`patch`, `AsyncMock`), `httpx.Response` конструируется напрямую. Внешней сети/реального ключа не требуется.

### Пробелы (что НЕ покрыто)

| ID      | Пробел                                                                                                                                | Локация                 |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| TEST-01 | `_resolve_timeout` не тестируется вообще                                                                                              | `api_client.py:77-84`   |
| TEST-02 | Recovery-сценарий retry («упал → повторил → успех») не проверяется; `asyncio.sleep` не замокан → ~18 из 20 сек прогона — реальный сон | `api_client.py:231-258` |
| TEST-03 | Ветка `len(content) > MAX_CONTENT_CHARS` не покрыта                                                                                   | `critic.py:94-100`      |
| TEST-04 | Реальная реализация `do_architecture_review`/`do_security_audit` не выполняется (замокана на уровне server)                           | `critic.py:287-312`     |
| TEST-05 | Ветки ошибок Balance API в `health_check` не покрыты                                                                                  | `critic.py:216-223`     |
| TEST-06 | Интеграция `file_path` через декоратор не тестируется (поймала бы BUG-01)                                                             | `server.py:120-127`     |
| TEST-07 | Ветки `reasoning_tokens>0`/`cached_tokens>0` в metadata не рендерятся в тестах                                                        | `server.py:47-52`       |
| TEST-08 | Timeout-ветки и pip-fail в `self_update` не покрыты                                                                                   | `server.py:361-385`     |
| TEST-09 | `_setup_logging` не тестируется                                                                                                       | `config.py:57-77`       |
| TEST-10 | Generic 4xx-ветка не покрыта                                                                                                          | `api_client.py:321-327` |

**Мёртвая зависимость:** `respx` в dev-extras не используется (0 совпадений в `tests/`).

---

## 8. История и динамика

14 коммитов за ~29 часов (15–16 апреля 2026), одна ветка `main`. Эволюция v1.3.0 → v1.8.0, ~минорная версия каждые 1–2 часа. Чёткий GRACE-паттерн: после функциональных изменений — отдельный docs-коммит синхронизации XML (3 из 14). Серий «fix-fix-fix» нет — единственный `fix:` (`48efb28`) пойман через 6 минут после интродукции. Тесты росли синхронно: 99 (v1.7.0) → 127 (v1.8.0). v1.8.0 — явная фаза hardening (SecretStr, обязательный ключ, pydantic-валидации, DRY-декоратор).

**Косметика:** иероглифы `初始` в первом коммит-месседже (DOC-13).

---

## 9. Вывод

Продукт хорошо инженерно сделан и безопасен в части обращения с ключом, но имеет **одну реальную дыру безопасности** (SEC-01: arbitrary file read → эксфильтрация), **один неработающий на практике retry** (REL-01), **пару скрытых багов из DRY-рефакторинга** (BUG-01, BUG-02) и **системный дрейф документации**. Приоритет устранения — в [REMEDIATION-PLAN.md](REMEDIATION-PLAN.md).
