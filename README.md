# grok-critic-mcp

**MCP-сервер, превращающий модель [`grok-4.20-multi-agent`](https://x.ai) (xAI) во «внешнего критика» для AI-агентов** — глубокое ревью кода, анализ архитектуры и security-аудит по требованию.

Работает через [Polza.AI](https://polza.ai) — OpenAI-совместимый API-прокси к моделям xAI. Использует **Responses API** (`POST /v1/responses`), **не** Chat Completions.

---

## Содержание

- [Зачем это нужно](#зачем-это-нужно)
- [Как работает grok-4.20-multi-agent](#как-работает-grok-420-multi-agent)
- [Модель работы: MCP + Skill + Instructions](#модель-работы-mcp--skill--instructions)
- [Установка](#установка)
- [Конфигурация](#конфигурация)
- [Регистрация в MCP-клиенте](#регистрация-в-mcp-клиенте)
- [MCP-инструменты (справочник)](#mcp-инструменты-справочник)
- [Внутренняя логика запроса](#внутренняя-логика-запроса)
- [Формат ответа](#формат-ответа)
- [Правила работы с критиком](#правила-работы-с-критиком)
- [Kilo Code Skill](#kilo-code-skill)
- [Архитектура](#архитектура)
- [Тестирование](#тестирование)
- [Деплой на VM](#деплой-на-vm)
- [Оценка стоимости](#оценка-стоимости)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Зачем это нужно

AI-агент, который пишет код, склонен «глотать» собственные ошибки: он и автор, и единственный ревьюер. `grok-critic-mcp` даёт агенту **второе, независимое и более глубокое мнение** — специально настроенную модель-критика, которую агент обязан вызвать в определённых ситуациях (после существенного кода, при спорной архитектуре, для security-чувствительного кода, при неудачном баг-фиксе).

Ключевая идея: обычное ревью одним LLM — это один проход рассуждения. `grok-4.20-multi-agent` вместо этого запускает **несколько независимых reasoning-агентов параллельно** и сводит их к консенсусу — то есть даёт более полный и устойчивый разбор, чем одиночная модель.

Сервер предоставляет агентам-заказчикам (Claude Code, Kilo Code, Cursor, VS Code и др.) единый набор из **8 инструментов** и берёт на себя весь транспорт: HTTP-запросы к Polza.AI, retry, разбор ответа, подсчёт токенов и стоимости, форматирование результата на русском языке.

---

## Как работает grok-4.20-multi-agent

Модель от xAI с **multi-agent reasoning**. Вместо единственного chain-of-thought она порождает несколько параллельных reasoning-агентов, каждый независимо анализирует задачу, после чего формируется консенсус-ответ.

### Число агентов → уровень усилий (effort)

| `agent_count` | Reasoning effort | Ориентир по времени | Когда применять                      |
| :-----------: | :--------------: | :-----------------: | ------------------------------------ |
|      `4`      |      `low`       |       быстро        | Быстрая проверка, небольшие сниппеты |
|     `16`      |      `high`      | до нескольких минут | Полное ревью, архитектура, security  |

Официально xAI поддерживает два режима усилий. Сервер маппит `agent_count` на effort детерминированной функцией: **`agent_count ≤ 4 → "low"`, иначе `"high"`**. Любое переданное значение сначала клэмпится в диапазон **1–64** (двойной клэмп: pydantic-валидация в конфиге + защитная проверка в сервере).

### Особенности API

- Только Responses API (`POST /v1/responses`), **не** `/v1/chat/completions`.
- В запросе усилия задаются как `reasoning.effort` (объект), а не `reasoning_effort`.
- В ответе текст лежит в `output_text` либо в `output[].content[].text` (тип `output_text`).
- Latency при 16 агентах — заметная (обычно 1–3 минуты), поэтому таймауты клиента и сервера подбираются с запасом.

---

## Модель работы: MCP + Skill + Instructions

Три уровня, каждый отвечает за своё:

```
┌──────────────────────────────────────────────────────────────────┐
│             AI-агент (Kilo / Claude Code / Cursor)               │
│                                                                  │
│  instructions.md / AGENTS.md ──►  ПРАВИЛА: "когда обязан         │
│         │                          вызвать критика"              │
│         ▼                                                        │
│  skill/grok-critic ───────────►  КАК вызывать: инструменты,      │
│         │                          параметры, триггеры, cost     │
│         ▼                                                        │
│  MCP tool call: critic_review(content=..., ...)                  │
│         │                                                        │
│         ▼                                                        │
│  ┌──────────────────────────────────────────┐                    │
│  │  grok-critic-mcp (Python, stdio)         │                    │
│  │    server.py   ← FastMCP, 8 tools        │                    │
│  │      │  _review_tool: file_path→content, │                    │
│  │      │   clamp agent_count, log, format  │                    │
│  │      ▼                                   │                    │
│  │    critic.py   ← system-промпты, сборка  │                    │
│  │      ▼                                   │                    │
│  │    api_client.py ─── HTTP ──► Polza.AI ──► grok-4.20-         │
│  │      ▲                       /responses    multi-agent        │
│  │    config.py  ← .env (POLZA_*)           │                    │
│  └──────────────────────────────────────────┘                    │
└──────────────────────────────────────────────────────────────────┘
```

- **MCP-сервер** — транспортный слой (Python-процесс на stdio). Принимает tool-вызовы, ходит в Polza.AI, парсит ответ, считает стоимость.
- **Skill** — markdown-инструкция, объясняющая агенту, **когда и как** пользоваться инструментами. Без скилла агент не знает, что критик существует.
- **Instructions / AGENTS.md** — правила проекта/агента, **обязывающие** вызывать критика в нужные моменты.

---

## Установка

### Требования

- **Python 3.11+** (используется `str.is_relative_to`, `X | None`-аннотации, `parents[2]`).
- API-ключ [Polza.AI](https://polza.ai) — или любого OpenAI-совместимого провайдера с поддержкой Responses API.

### Из исходников

```bash
git clone https://github.com/Bondartsov/grok-critic-mcp.git
cd grok-critic-mcp
pip install -e .
```

После установки доступна console-команда `grok-critic` (entry point `grok_critic.server:main` из `pyproject.toml`).

### Dev-режим (с тестами)

```bash
pip install -e ".[dev]"   # + pytest, pytest-asyncio, respx
```

---

## Конфигурация

Конфигурация читается **только из переменных окружения с префиксом `POLZA_`** (через `pydantic-settings`). Источник — окружение процесса и файл `.env` в корне репозитория (путь вычисляется относительно `config.py`, не зависит от текущей рабочей директории). Посторонние переменные игнорируются (`extra="ignore"`).

Скопируйте шаблон и заполните:

```bash
cp .env.example .env
```

### Полная таблица параметров

| Переменная                  | Тип    | Дефолт (в коде)              | Обязательна | Назначение                                                                                                                      |
| --------------------------- | ------ | ---------------------------- | :---------: | ------------------------------------------------------------------------------------------------------------------------------- |
| `POLZA_API_KEY`             | secret | —                            |   **да**    | Ключ Polza.AI. Хранится как `SecretStr`, не попадает в логи/`repr`. `min_length=1`.                                             |
| `POLZA_BASE_URL`            | str    | `https://polza.ai/api/v1`    |     нет     | Базовый URL API.                                                                                                                |
| `POLZA_MODEL`               | str    | `x-ai/grok-4.20-multi-agent` |     нет     | Идентификатор модели.                                                                                                           |
| `POLZA_AGENT_COUNT`         | int    | `16`                         |     нет     | Число агентов по умолчанию. Диапазон `1–64` (`ge=1, le=64`).                                                                    |
| `POLZA_TIMEOUT_SECONDS`     | int    | `180`                        |     нет     | Максимальный таймаут запроса, сек (`ge=1`). Для 16 агентов рекомендуется `300`; в поставляемом `.env.example` выставлено `300`. |
| `POLZA_LOG_LEVEL`           | str    | `WARNING`                    |     нет     | Один из `DEBUG/INFO/WARNING/ERROR/CRITICAL` (валидируется, регистр нормализуется).                                              |
| `POLZA_LOG_FILE`            | str    | `""` (пусто)                 |     нет     | Пусто → лог в `stderr` (stdout занят под MCP stdio). Иначе — путь к файлу лога.                                                 |
| `POLZA_PRICE_INPUT_PER_1M`  | float  | `0.0`                        |     нет     | Цена $ за 1M input-токенов (для расчёта `cost_usd`). Ориентир Polza.AI — `2.6`.                                                 |
| `POLZA_PRICE_OUTPUT_PER_1M` | float  | `0.0`                        |     нет     | Цена $ за 1M output-токенов. Ориентир — `6.6`.                                                                                  |
| `POLZA_ALLOW_SELF_UPDATE`   | bool   | `false`                      |     нет     | Разрешает инструмент `self_update` (`git pull`+`pip install`+restart). По умолчанию **выключен**.                               |
| `POLZA_ALLOWED_READ_DIRS`   | str    | `""` (только cwd)            |     нет     | Доп. директории, откуда разрешено читать файлы через `file_path`. Разделитель — `os.pathsep` (`;` на Windows, `:` на Unix).     |
| `POLZA_MAX_RETRIES`         | int    | `2`                          |     нет     | Число повторов запроса при таймаутах/сетевых ошибках/`429`/`5xx` (`0–10`).                                                      |
| `POLZA_RETRY_BACKOFF_BASE`  | float  | `2.0`                        |     нет     | База экспоненциального backoff между повторами (сек): пауза = `base^attempt`.                                                   |
| `POLZA_MAX_CONTENT_CHARS`   | int    | `100000`                     |     нет     | Лимит размера контента ревью (~100 КБ) — защита от перерасхода. Превышение → ошибка без обращения к API.                        |
| `POLZA_TIMEOUT_LOW`         | int    | `90`                         |     нет     | Таймаут (сек) при `agent_count ≤ 4`.                                                                                            |
| `POLZA_TIMEOUT_MID`         | int    | `150`                        |     нет     | Таймаут (сек) при `4 < agent_count ≤ 8`.                                                                                        |

> **Важно про запуск.** `config` инстанцируется на уровне модуля при импорте. Если `POLZA_API_KEY` не задан (ни в окружении, ни в `.env`) — импорт упадёт с `ValidationError` сразу при старте. Это намеренно: сервер без ключа бесполезен.

Пример `.env`:

```ini
# ОБЯЗАТЕЛЬНО
POLZA_API_KEY=pza_your_key_here

# Опционально (показаны рекомендуемые значения)
POLZA_BASE_URL=https://polza.ai/api/v1
POLZA_MODEL=x-ai/grok-4.20-multi-agent
POLZA_AGENT_COUNT=16
POLZA_TIMEOUT_SECONDS=300
POLZA_LOG_LEVEL=WARNING
POLZA_LOG_FILE=
POLZA_PRICE_INPUT_PER_1M=2.6
POLZA_PRICE_OUTPUT_PER_1M=6.6
POLZA_ALLOW_SELF_UPDATE=false
POLZA_ALLOWED_READ_DIRS=
```

### Горячая перезагрузка

После изменения `.env` не обязательно перезапускать процесс — вызовите инструмент `reload_config_tool`. Он перечитывает `.env`, **обновляет объект `config` in-place** (все модули, импортировавшие `config`, сразу видят новые значения) и закрывает устаревший HTTP-клиент (у него мог быть старый `base_url`/`timeout`).

---

## Регистрация в MCP-клиенте

### Claude Code

Через CLI (рекомендуется):

```bash
claude mcp add grok-critic -- python -m grok_critic.server
```

Либо вручную в конфиге MCP-клиента:

```json
{
  "mcpServers": {
    "grok-critic": {
      "command": "python",
      "args": ["-m", "grok_critic.server"],
      "timeout": 300000
    }
  }
}
```

### Kilo Code (`~/.config/kilo/opencode.json`)

```json
{
  "mcpServers": {
    "grok-critic": {
      "command": "python",
      "args": ["-m", "grok_critic.server"],
      "timeout": 300000,
      "env": { "POLZA_API_KEY": "pza_your_key_here" }
    }
  }
}
```

> Если установлено через `pip install -e .`, можно указать `"command": "grok-critic"` (console-script).

### Cursor / VS Code (settings.json)

```json
{
  "mcp.servers": {
    "grok-critic": {
      "command": "python",
      "args": ["-m", "grok_critic.server"]
    }
  }
}
```

### Проверка

Вызовите `check_health` через клиента (или запустите `python -m grok_critic.server` вручную). Ожидаемый вывод:

```
Status: ok
Model: x-ai/grok-4.20-multi-agent
Base URL: https://polza.ai/api/v1
Pricing: input=$2.6/1M output=$6.6/1M
Balance: 1234.56 ₽
```

---

## MCP-инструменты (справочник)

Восемь инструментов делятся на **ревью** (обращаются к модели) и **административные** (управляют сервером).

### Ревью-инструменты

Все четыре обёрнуты декоратором `_review_tool`, который единообразно: клэмпит `agent_count` (1–64), опционально читает файл по `file_path`, логирует вызов, ловит исключения и форматирует результат.

#### `critic_review` — общее ревью кода

```python
critic_review(
    content: str,             # код для ревью
    context: str | None = None,      # проект, язык, назначение
    agent_count: int | None = None,  # 4 или 16 (default из конфига)
    focus_areas: str | None = None,  # "security,performance,SOLID,DRY"
) -> str
```

`focus_areas` — строка, разбиваемая по запятым в список фокус-областей. Разбирает баги и edge-cases, SOLID/DRY/KISS, производительность (N+1, память, сложность), безопасность, предлагает рефакторинг с примерами и недостающие тесты.

#### `architecture_review` — ревью архитектуры

```python
architecture_review(content, context=None, agent_count=None) -> str
```

Специализированный system-промпт. `focus_areas` фиксирован: `architecture, scalability, dependencies`. Разбирает паттерны (Modular Monolith / Microservices / DDD), Bounded Contexts, направление и цикличность зависимостей, coupling/cohesion, масштабируемость, single points of failure, технический долг.

#### `security_audit` — security-аудит

```python
security_audit(content, context=None, agent_count=None) -> str
```

Специализированный system-промпт. `focus_areas` фиксирован: `security, vulnerabilities, secrets`. Чеклист: injection (SQL/XSS/command/path traversal), аутентификация и авторизация (пароли, MFA, session fixation/hijacking, privilege escalation, IDOR), данные и секреты, инфраструктура (CORS, rate limiting, SSRF/CSRF). Вывод классифицируется по уровням **🔴 CRITICAL / 🟡 HIGH / 🟠 MEDIUM / 🔵 LOW**.

#### `critic_followup` — уточняющий вопрос

```python
critic_followup(
    previous_review: str,     # ПОЛНЫЙ текст предыдущего ответа критика
    question: str,            # уточняющий вопрос / контраргумент
    agent_count: int | None = None,
) -> str
```

Продолжение диалога по уже полученному ревью: углубиться в аспект, оспорить оценку, запросить альтернативы. История диалога **не хранится** на сервере — предыдущий ответ передаётся явно.

#### Параметр `file_path` (для трёх content-инструментов)

`critic_review`, `architecture_review`, `security_audit` дополнительно принимают опциональный `file_path`. Если он передан, сервер читает файл, подставляет его как `content`, а при отсутствии явного `context` проставляет `context = "File: <путь>"`. Если файл не найден / это не файл / пустой — возвращается ошибка **без** обращения к API (чтобы не тратить платный вызов на невалидный ввод).

> **Sandbox (ограничение области чтения).** Файл должен лежать внутри разрешённых корней: рабочая директория сервера (cwd) + директории из `POLZA_ALLOWED_READ_DIRS`. Путь вне разрешённых корней (включая выход через `..`) отклоняется с ошибкой `Access denied` **до** обращения к API. Дополнительно действует denylist типичных файлов секретов — `.env`, `id_rsa`, `id_ed25519`, `credentials.json`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.kdbx` блокируются даже внутри разрешённых директорий, т.к. содержимое файла отправляется во внешний API. Файлы больше 1 МБ также отклоняются (всё равно режутся лимитом `MAX_CONTENT_CHARS`).

### Административные инструменты

#### `check_health` — статус и баланс

```python
check_health() -> str
```

Без параметров. Возвращает статус, модель, base URL, pricing (если цены заданы), список проблем и **баланс в ₽** (запрашивает Balance-эндпоинт Polza.AI).

#### `reload_config_tool` — горячая перезагрузка `.env`

```python
reload_config_tool() -> str
```

Без параметров. Перечитывает `.env` без рестарта, обновляет `config` in-place, закрывает устаревший HTTP-клиент. Возвращает текущие значения (ключ — маскированный). Применяйте после изменения цен, таймаута, `POLZA_ALLOW_SELF_UPDATE` и др.

#### `restart_server` — полный перезапуск

```python
restart_server(reason: str | None = None) -> str
```

Закрывает соединения и делает `os._exit(0)`. MCP-клиент (Kilo Code, Claude Code и т.д.) автоматически поднимает процесс заново. `reason` логируется перед выходом.

#### `self_update` — обновление с GitHub

```python
self_update() -> str
```

Без параметров. **Требует включённого флага** `POLZA_ALLOW_SELF_UPDATE=true` — иначе вернёт `❌ self_update is disabled...`. При включённом флаге: `git pull` (таймаут 60 с) → если «Already up to date», выходит без изменений; иначе `pip install -e .` (таймаут 120 с) → `os._exit(0)`, после чего клиент перезапускает сервер с новым кодом.

> Флаг выключен по умолчанию намеренно: инструмент выполняет установку пакета и завершает процесс. Включайте осознанно.

---

## Внутренняя логика запроса

Что происходит между tool-вызовом и ответом модели:

1. **Декоратор `_review_tool`** клэмпит `agent_count` (1–64), при наличии `file_path` читает файл в `content`, логирует `content_len`/`agent_count`.
2. **`critic.py`** валидирует непустоту `content` и его длину против **`MAX_CONTENT_CHARS = 100 000`** (защита от разгона стоимости), выбирает `agent_count` (переданный или из конфига) и собирает user-промпт из секций `## Контекст`, `## Фокус внимания`, `## Код для ревью`. Подбирается один из четырёх system-промптов (общий / followup / architecture / security). **Все ответы — на русском.**
3. **`api_client.py`** формирует запрос к `POST {base_url}/responses`:
   - заголовки `Authorization: Bearer <key>`, `Content-Type: application/json`;
   - тело: `model`, `reasoning.effort` (из `agent_count`), `input` (system + user сообщения), опциональный `prompt_cache_key` (кеширование system-промпта на стороне Polza.AI);
   - **динамический таймаут** по числу агентов: `≤4 → min(base, 90с)`, `≤8 → min(base, 150с)`, иначе полный `POLZA_TIMEOUT_SECONDS`.
4. **Retry**: до 2 повторов с экспоненциальным backoff (`2^attempt`) на таймаутах, сетевых ошибках транспорта (`ConnectError`, `ReadError`, `RemoteProtocolError` и др. `httpx.TransportError` — с пересозданием HTTP-клиента), `429` и `5xx`. Явно разбираются статусы `401` (auth), `402` (недостаточно средств), `429` (rate limit), `502` (провайдер недоступен), `503` (нет провайдеров), прочие `4xx/5xx`.
5. **Разбор ответа**: извлекается текст, `usage` (input/output/total-токены, `cost_rub` от API, cached- и reasoning-токены), локально считается `cost_usd` по ценам из конфига. Наружу возвращается единый объект `CritiqueResult`.
6. **`server.py`** форматирует результат: текст ревью + metadata footer.

`ResponsesClient` (httpx.AsyncClient) создаётся один раз и переиспользуется между запросами; таймаут передаётся per-request, что и позволяет менять его динамически.

---

## Формат ответа

Каждое ревью возвращается как текст + metadata footer:

```
<текст ревью на русском>

---
📊 Metadata: model=x-ai/grok-4.20-multi-agent | agents=16 | effort=high
📈 Tokens: input=18 231 output=15 434 total=33 665
🧠 Reasoning: 9 100 (59% of output) — ~4x cost
💾 Cached: 18 000/18 231 (99%)
💰 Cost: 1.23 ₽ | $0.1493
🏷️ Review ID: rev_1f866fc571eb
```

- **Tokens** — с пробелами-разделителями разрядов.
- **Reasoning** — показывается, если reasoning-токенов > 0 (и их доля от output); стоят примерно в 4× дороже обычных.
- **Cached** — показывается, если Polza.AI закешировал часть input (обычно system-промпт); чтение из кеша дешевле.
- **Cost** — реальная стоимость в ₽ (из `usage.cost_rub`) и/или расчётная в $ (по ценам конфига). Показываются только ненулевые части.
- **Review ID** — идентификатор для ссылки в followup.

При ошибке инструмент возвращает строку вида `❌ Error: <причина>` (или `❌ <tool> failed: <exc>` для неожиданных исключений).

---

## Правила работы с критиком

Эти правила предназначены для AI-агента, который пользуется сервером (они же зашиты в [skill](skill/SKILL.md)):

1. **Всегда передавай `context`** — критик работает точнее, зная проект, язык и назначение кода.
2. **Всегда задавай `focus_areas`** для `critic_review` — целенаправленное ревью полезнее размытого.
3. **Читай ответ полностью** — критик может найти неожиданные проблемы.
4. **🔴 CRITICAL — исправляй до продолжения.** Не переходи к следующей задаче с открытым критическим замечанием.
5. **Отвечай критику через `critic_followup`.** Если не согласен — приведи аргументы; если критик не учёл контекст — объясни; если переоценил проблему — оспорь. **Не «глотай» замечания молча.**
6. **Не вызывай критика для только что сгенерированного, ещё не прочитанного тобой кода** — сначала прочитай, что написал.
7. **Следи за балансом** через `check_health` (показывает ₽) и предупреждай пользователя при низком балансе.

### Когда вызывать (триггеры)

| Ситуация                                                        | Инструмент            |   Приоритет   |
| --------------------------------------------------------------- | --------------------- | :-----------: |
| Завершено планирование / архитектура / system-design            | `architecture_review` |  Обязательно  |
| Написан существенный код (> 50 строк)                           | `critic_review`       |  Обязательно  |
| Баг-фикс не удался с первой попытки                             | `critic_review`       |  Обязательно  |
| Security-чувствительный код (auth, payments, crypto, user data) | `security_audit`      |  Обязательно  |
| Перед merge / PR                                                | `critic_review`       | Рекомендуется |
| Спорное архитектурное решение                                   | `architecture_review` | Рекомендуется |

Не вызывать для тривиального (переименование, форматирование, правка нескольких строк) — типичный вызов с 16 агентами платный.

---

## Kilo Code Skill

Репозиторий включает готовый скилл: [`skill/SKILL.md`](skill/SKILL.md) — инструкция для агента о том, когда и как вызывать критика (триггеры, сигнатуры, workflow-примеры, cost awareness, правила).

Установка:

```bash
mkdir -p ~/.kilocode/skills/grok-critic
cp skill/SKILL.md ~/.kilocode/skills/grok-critic/SKILL.md
```

Типовые сценарии из скилла:

- **Post-implementation review** → `critic_review` → при 🔴 исправить и повторить → при разногласии `critic_followup`.
- **Architecture validation** → `architecture_review` → `critic_followup` («а что если event sourcing вместо…?»).
- **Security audit** → `security_audit` → все 🔴 исправить обязательно, все 🟡 — перед продакшеном.

### Интеграция в правила агента

В `instructions.md` (глобально):

```markdown
## Критик (grok-critic MCP) — см. skill `grok-critic`
Обязателен при: планировании архитектуры, баг-фиксе со 2-й попытки, после > 50 строк кода.
```

В `AGENTS.md` (на уровне проекта) — четыре обязательных точки вызова: после планирования (`architecture_review`), после существенного кода (`critic_review`), при неудачном баг-фиксе (`critic_review`), для security-кода (`security_audit`).

---

## Архитектура

### Структура проекта

```
grok-critic-mcp/
├── src/grok_critic/
│   ├── __init__.py          # Публичный API пакета
│   ├── config.py            # M-CONFIG: pydantic-settings, env, логирование, hot-reload
│   ├── api_client.py        # M-API: async HTTP → Polza.AI, retry, usage/cost, CritiqueResult
│   ├── critic.py            # M-CRITIC: 4 system-промпта, orchestration, health_check
│   └── server.py            # M-SERVER: FastMCP, 8 tools, декоратор, metadata footer
├── tests/                   # 157 тестов (config / api_client / critic / server)
├── skill/SKILL.md           # Kilo Code skill (инструкция для агентов)
├── docs/                    # GRACE-артефакты + этот отчёт/план
├── .env.example             # Шаблон окружения
├── pyproject.toml           # Метаданные, зависимости, entry point
└── README.md
```

### Модули и контракты

| Модуль          | Роль        | PURPOSE                                                                                                                                                                           |
| --------------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config.py`     | UTILITY     | Загрузка/валидация конфигурации из `.env` (pydantic-settings). `api_key` обязателен (`SecretStr`, `min_length=1`), `agent_count` 1–64, `timeout_seconds` ≥1. Hot-reload in-place. |
| `api_client.py` | INTEGRATION | Async HTTP-клиент к Responses API. Persistent client, динамический таймаут, retry с backoff, разбор usage/cost, `CritiqueResult`.                                                 |
| `critic.py`     | CORE_LOGIC  | 4 system-промпта, сборка user-промпта, валидация размера (`MAX_CONTENT_CHARS`), три режима ревью + followup, `health_check` с балансом.                                           |
| `server.py`     | ENTRY_POINT | FastMCP-сервер (stdio), 8 инструментов, декоратор `_review_tool`, `_validate_agent_count`, `_read_file_content`, форматирование metadata.                                         |

Зависимости строго линейные: `config ← api_client ← critic ← server`.

### Ключевые технические решения

- **Persistent AsyncClient** — один httpx-клиент на весь процесс; таймаут per-request → можно менять динамически по `agent_count`.
- **Динамический таймаут** — быстрые запросы (мало агентов) не ждут полный лимит.
- **Retry c экспоненциальным backoff** — на таймаутах, сетевых ошибках транспорта (`httpx.TransportError`), `429` и `5xx`; `401` не ретраится.
- **Расчёт стоимости** — `input/1M × price_input + output/1M × price_output`; параллельно берётся фактический `cost_rub` из ответа API.
- **Hot-reload** — `reload_config_tool` обновляет `config` in-place (`object.__setattr__`) + сбрасывает `lru_cache`, все импортёры видят новые значения без рестарта.
- **Защита ключа** — `SecretStr` + извлечение `.get_secret_value()` только в момент формирования заголовка; ключ не логируется.

---

## Тестирование

```bash
pip install -e ".[dev]"

# все тесты
python -m pytest tests/ -q

# с покрытием
python -m pytest tests/ -v --cov=grok_critic --cov-report=term-missing
```

**157 тестов** (config 33 · api_client 50 · critic 27 · server 44 · package 3) на `pytest` + `pytest-asyncio` (`asyncio_mode="auto"`). Все внешние HTTP-вызовы замоканы (`unittest.mock`, `AsyncMock`), реального ключа/сети не требуется. Покрыты: дефолты и env-override конфига, валидация полей, effort-mapping, разбор ответа и usage (включая cost_rub/cached/reasoning), расчёт стоимости, обработка статусов API, регистрация 8 инструментов, декоратор `_review_tool`, клэмп `agent_count`, чтение файла, hot-reload, restart, `self_update`, health-check с балансом.

---

## Деплой на VM

Сервер работает по stdio, поэтому на удалённой машине его удобно запускать через SSH-обёртку MCP-клиента.

```bash
# на VM
sudo apt update && sudo apt install python3 python3-pip python3-venv
mkdir -p /opt/grok-critic-mcp

# с локальной машины
scp -r grok-critic-mcp/ user@vm:/opt/grok-critic-mcp/

# на VM
cd /opt/grok-critic-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env && nano .env    # вставить POLZA_API_KEY
```

Регистрация в MCP-клиенте (на машине агента), SSH-транспорт:

```json
{
  "mcpServers": {
    "grok-critic": {
      "command": "ssh",
      "args": ["user@vm", "cd /opt/grok-critic-mcp && .venv/bin/python -m grok_critic.server"],
      "timeout": 300000
    }
  }
}
```

> Нужен passwordless SSH-ключ. Для systemd-сервиса (если требуется постоянно запущенный процесс) используйте unit c `EnvironmentFile=/opt/grok-critic-mcp/.env` и `Restart=on-failure`.

---

## Оценка стоимости

При ориентировочных ценах Polza.AI ($2.6 / 1M input, $6.6 / 1M output):

| Тип вызова                |   Input |  Output | ≈ Стоимость |
| ------------------------- | ------: | ------: | ----------: |
| Быстрое ревью (4 агента)  |  ~3 000 |  ~2 000 |      ~$0.02 |
| Полное ревью (16 агентов) | ~18 000 | ~15 000 |      ~$0.15 |
| Архитектурное ревью (16)  | ~20 000 | ~18 000 |      ~$0.18 |
| Followup                  | ~25 000 | ~10 000 |      ~$0.13 |

Цены могут меняться — проверяйте на [Polza.AI](https://polza.ai), обновляйте `.env` и вызывайте `reload_config_tool`. Кеширование system-промпта на стороне Polza.AI удешевляет повторные вызовы (cached-токены дешевле).

---

## Troubleshooting

**`POLZA_API_KEY is not set` / падение при старте.** Ключ не найден. Проверьте, что `.env` в корне репозитория и `POLZA_API_KEY` заполнен; для MCP-клиента ключ можно передать через `env` в конфиге сервера.

**Стоимость не показывается в ответе.** Цены в `.env` равны `0`. Задайте `POLZA_PRICE_INPUT_PER_1M`/`POLZA_PRICE_OUTPUT_PER_1M` и вызовите `reload_config_tool`. (Фактическая `cost_rub` приходит от API независимо от этих цен.)

**Таймаут при 16 агентах.** Увеличьте `POLZA_TIMEOUT_SECONDS` (например, `300`) и таймаут в MCP-клиенте (`"timeout": 300000`, миллисекунды), затем `reload_config_tool`.

**`self_update` отвечает `is disabled`.** Флаг выключен по умолчанию. Установите `POLZA_ALLOW_SELF_UPDATE=true` в `.env` и вызовите `reload_config_tool`.

**После `restart_server`/`self_update` сервер не поднялся.** MCP-клиент должен перезапускать процесс при выходе; если нет — перезапустите клиента вручную.

**LSP-ошибки в IDE при `pip install -e .`.** Пакет ставится в system-wide Python — это нормально, тесты при этом проходят.

---

## License

MIT

---

*Built with GRACE methodology — Graph-RAG Anchored Code Engineering.*
