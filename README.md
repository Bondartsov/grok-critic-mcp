# grok-critic-mcp

**MCP-сервер для глубокого ревью кода, архитектуры и security-аудита через [grok-4.20-multi-agent](https://x.ai) (16 reasoning agents).**

Работает через [Polza.AI](https://polza.ai) — OpenAI-совместимый API-прокси к xAI моделям. Использует Responses API (`/v1/responses`), **не** Chat Completions.

---

## Содержание

- [Что это](#что-это)
- [Как работает grok-4.20-multi-agent](#как-работает-grok-420-multi-agent)
- [Установка](#установка)
- [Настройка](#настройка)
- [MCP-инструменты](#mcp-инструменты)
- [Kilo Code Skill](#kilo-code-skill)
- [Интеграция с агентами](#интеграция-с-агентами)
- [Architecture](#architecture)
- [Testing](#testing)
- [Deployment на VM](#deployment-на-vm)
- [Cost Estimation](#cost-estimation)
- [Troubleshooting](#troubleshooting)

---

## Что это

MCP (Model Context Protocol) сервер, который предоставляет 8 инструментов для критического анализа кода:

| Инструмент | Назначение |
|------------|-----------|
| `critic_review` | Общее ревью кода (баги, SOLID, DRY, безопасность, производительность) |
| `architecture_review` | Специализированный анализ архитектуры (паттерны, зависимости, масштабируемость) |
| `security_audit` | Security-аудит с классификацией 🔴🟡🟠🔵 |
| `critic_followup` | Уточняющий вопрос по предыдущему ревью |
| `check_health` | Проверка статуса сервера, API-ключа, pricing, **баланс в ₽** |
| `reload_config_tool` | Горячая перезагрузка `.env` без перезапуска |
| `restart_server` | Полный перезапуск процесса (MCP-клиент поднимет автоматически) |
| `self_update` | Автообновление с GitHub (`git pull` + `pip install` + restart) |

Ответы на русском языке. Каждый ответ содержит metadata footer с моделью, токенами (с разделителями разрядов), кешированными токенами, стоимостью (₽ и $) и review_id.

---

## Как работает grok-4.20-multi-agent

`grok-4.20-multi-agent` — модель от xAI с **multi-agent reasoning**. Вместо одного chain-of-thought она запускает несколько параллельных reasoning agents, каждый из которых независимо анализирует проблему, а затем формирует консенсус.

### Agent count → Effort mapping

| Agent count | Reasoning effort | Timeout | Когда использовать |
|-------------|-----------------|---------|-------------------|
| 4 | `low` | ~90s | Быстрая проверка, мелкие сниппеты |
| 16 | `high` | ~300s | Полное ревью, архитектура, security |

Официально xAI поддерживает только 4 и 16 агентов. Другие значения маппятся на ближайшее: ≤4 → `low`, >4 → `high`.

### API особенности

- Работает **только** через Responses API (`POST /v1/responses`), **не** Chat Completions (`/v1/chat/completions`)
- Запрос содержит `reasoning.effort` (не `reasoning_effort`)
- Ответ содержит `output_text` или `output[].content[].text`
- Latency при 16 агентах: 1–3 минуты (в зависимости от объёма кода)

---

## Установка

### Требования

- Python 3.11+
- API-ключ [Polza.AI](https://polza.ai) (или любой OpenAI-совместимый провайдер с поддержкой Responses API)

### Из исходников

```bash
git clone https://github.com/a-bondartsov/grok-critic-mcp.git
cd grok-critic-mcp
pip install -e .
```

### Через pip (после публикации)

```bash
pip install grok-critic-mcp
```

---

## Настройка

### 1. Создайте `.env` файл

Скопируйте `.env.example` в `.env` и заполните:

```bash
cp .env.example .env
```

```ini
# ОБЯЗАТЕЛЬНО
POLZA_API_KEY=pza_your_key_here

# Опционально (значения по умолчанию показаны)
POLZA_BASE_URL=https://polza.ai/api/v1
POLZA_MODEL=x-ai/grok-4.20-multi-agent
POLZA_AGENT_COUNT=16
POLZA_TIMEOUT_SECONDS=300
POLZA_LOG_LEVEL=WARNING
POLZA_LOG_FILE=              # пусто = stderr (для MCP stdio)
POLZA_PRICE_INPUT_PER_1M=2.6   # $ за 1M input токенов
POLZA_PRICE_OUTPUT_PER_1M=6.6  # $ за 1M output токенов
```

### 2. Регистрация в MCP-клиенте

#### Kilo Code (`~/.config/kilo/opencode.json`)

Добавьте в `mcpServers`:

```json
{
  "mcpServers": {
    "grok-critic": {
      "command": "python",
      "args": ["-m", "grok_critic.server"],
      "timeout": 300000,
      "env": {
        "POLZA_API_KEY": "pza_your_key_here"
      }
    }
  }
}
```

> **Note:** Если установлен через `pip install -e .`, можно использовать `"command": "grok-critic"` (entry point из `pyproject.toml`).

#### Claude Code (`~/.claude/mcp.json`)

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

#### Cursor / VS Code (settings.json)

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

### 3. Проверка

Запустите сервер вручную и вызовите `check_health`:

```bash
python -m grok_critic.server
```

Или через MCP-клиент после регистрации — вызовите инструмент `check_health`. Должен вернуть:

```
Status: ok
Model: x-ai/grok-4.20-multi-agent
Base URL: https://polza.ai/api/v1
Pricing: input=$2.6/1M output=$6.6/1M
```

---

## MCP-инструменты

### `critic_review` — общее ревью кода

```python
critic_review(
    content: str,            # ОБЯЗАТЕЛЬНО — код для ревью
    context: str | None,     # проект, язык, назначение
    agent_count: int | None, # 4 или 16 (default из .env)
    focus_areas: str | None  # "security,performance,SOLID,DRY"
) -> str
```

**Пример вызова (из любого AI-агента):**

```
grok-critic_critic_review(
    content="def process_order(order):\n    db.execute('INSERT INTO orders VALUES (' + str(order.id) + ')')",
    context="Python e-commerce order processing",
    agent_count=16,
    focus_areas="security,performance"
)
```

**Ответ содержит:**
1. Детальный разбор по разделам (логические ошибки, SOLID, безопасность, производительность, улучшения)
2. Конкретные рекомендации с примерами кода
3. Metadata footer (модель, токены, стоимость ₽/$, cached tokens, review_id)

### `architecture_review` — ревью архитектуры

```python
architecture_review(
    content: str,            # описание архитектуры, C4 диаграмма, код
    context: str | None,     # tech stack, constraints, team size
    agent_count: int | None  # override
) -> str
```

Фокус: архитектурные паттерны, направление зависимостей, циклические связи, coupling/cohesion, масштабируемость, single points of failure, технический долг.

### `security_audit` — security аудит

```python
security_audit(
    content: str,            # код или конфигурация
    context: str | None,     # framework, deployment, threat model
    agent_count: int | None  # override
) -> str
```

Чеклист: injection (SQL, XSS, command, path traversal), auth (MFA, sessions, privilege escalation), данные и секреты (hardcoded credentials, небезопасное хранение), инфраструктура (CORS, rate limiting, SSRF).

Классификация: 🔴 CRITICAL / 🟡 HIGH / 🟠 MEDIUM / 🔵 LOW

### `critic_followup` — уточняющий вопрос

```python
critic_followup(
    previous_review: str,    # ПОЛНЫЙ текст предыдущего ответа критика
    question: str,           # уточняющий вопрос
    agent_count: int | None  # override
) -> str
```

Использовать если:
- Ответ неполный — нужно углубиться в конкретный аспект
- Не согласны с рекомендацией — привести аргументацию
- Нужны дополнительные примеры или альтернативы

### `check_health` — статус сервера

```python
check_health() -> str
```

Возвращает: статус, модель, base URL, pricing (если настроен), список проблем.

### `reload_config_tool` — горячая перезагрузка

```python
reload_config_tool() -> str
```

Перечитывает `.env` **без перезапуска** процесса. Использовать после изменения цен, API-ключа, таймаута. Старый HTTP-клиент закрывается автоматически.

### `restart_server` — полный перезапуск

```python
restart_server(reason: str | None) -> str
```

Жёсткий выход через `os._exit(0)`. MCP-клиент (Kilo Code, Claude Code и т.д.) автоматически перезапустит сервер. Использовать если hot reload недостаточен.

---

## Kilo Code Skill

Репозиторий включает готовый скилл для Kilo Code: [`skill/SKILL.md`](skill/SKILL.md).

### Установка скилла

Скопируйте `skill/SKILL.md` в `~/.kilocode/skills/grok-critic/SKILL.md`:

```bash
mkdir -p ~/.kilocode/skills/grok-critic
cp skill/SKILL.md ~/.kilocode/skills/grok-critic/SKILL.md
```

### Что даёт скилл

Скилл — это **инструкция для любого агента** в Kilo Code о том, когда и как вызывать критика. Без скилла агенты не знают о существовании MCP-инструментов.

Скилл определяет:

1. **Triggers** — когда вызывать критика обязательно:
   - Planning / architecture / system-design завершён → `architecture_review`
   - Написан существенный код (>50 строк) → `critic_review`
   - Баг-фикс не получился с первой попытки → `critic_review`
   - Security-sensitive код → `security_audit`

2. **Agent count guide** — 4 vs 16 агентов

3. **Workflow examples** — типовые сценарии

4. **Cost awareness** — типичный вызов $0.10–0.25

5. **Rules** — правила взаимодействия с критиком

---

## Интеграция с агентами

Чтобы ваши AI-агенты автоматически использовали критика, добавьте правила в их инструкции.

### В `instructions.md` (глобально для всех агентов)

```markdown
## 🔴 Критик (grok-critic MCP) — см. skill `grok-critic`

Полная документация: skill `grok-critic` (triggers, tools, workflow, cost awareness).
Обязателен при: планировании архитектуры, баг-фиксе со 2-й попытки, после >50 строк кода.
```

### В `AGENTS.md` (на уровне проекта)

```markdown
## Обязательный вызов критика

1. **Планирование** — после завершения `grace-plan`, `system-design`, `requirements-analysis`
   вызвать `architecture_review` с результатом планирования.

2. **Код** — после написания существенного кода (>50 строк) вызвать `critic_review`
   с контекстом проекта и фокусом на соответствующих областях.

3. **Баг-фикс** — если фикс не получился с первой попытки, вызвать `critic_review`
   с кодом фикса и описанием бага как context.

4. **Security** — любой код связанный с auth, payments, crypto, user data
   обязан пройти `security_audit`.
```

### В конкретном агенте (`.kilocode/agent/my-agent.md`)

```markdown
## Code Review Protocol

После завершения задачи:
1. Вызвать `critic_review` с написанным кодом
2. Если найдены 🔴 CRITICAL — исправить и вызвать повторно
3. Если не согласен — вызвать `critic_followup` с аргументацией
4. Результат ревью приложить к коммиту
```

### Как MCP + Skill работают вместе

```
┌─────────────────────────────────────────────────────┐
│                   AI Agent (Kilo Code)              │
│                                                     │
│  instructions.md ──► "вызови критика после кода"    │
│         │                                           │
│         ▼                                           │
│  skill/grok-critic ──► КАК вызывать, параметры,    │
│  │                      триггеры, cost awareness    │
│  │                                                  │
│  └──────────► MCP Tool: critic_review(content=...)  │
│                        │                            │
│                        ▼                            │
│              ┌─────────────────────┐                │
│              │  grok-critic-mcp    │                │
│              │  (Python process)   │                │
│              │                     │                │
│              │  server.py ◄── MCP protocol (stdio)  │
│              │    │                │                │
│              │    ▼                │                │
│              │  critic.py          │                │
│              │    │                │                │
│              │    ▼                │                │
│              │  api_client.py ────►│──► Polza.AI    │
│              │                     │    API         │
│              │  config.py ◄── .env │      │        │
│              └─────────────────────┘      │        │
│                                           ▼        │
│                                  grok-4.20-multi-agent│
│                                  (16 reasoning agents)│
└─────────────────────────────────────────────────────┘
```

**MCP-сервер** — это транспортный слой (Python процесс на stdio). Он принимает tool calls от MCP-клиента, делает HTTP-запросы к Polza.AI, парсит ответы, считает токены и стоимость.

**Skill** — это инструкция (markdown файл), которая говорит AI-агенту КОГДА и КАК использовать MCP-инструменты. Без скилла агент не знает о существовании инструментов.

**Instructions / AGENTS.md** — глобальные правила, обязывающие агента использовать критика в определённых ситуациях.

---

## Architecture

### Структура проекта

```
grok-critic-mcp/
├── src/grok_critic/
│   ├── __init__.py          # Package exports
│   ├── config.py            # Pydantic Settings, env vars, logging
│   ├── api_client.py        # Async HTTP client, retry, cost calculation
│   ├── critic.py            # System prompts, review orchestration
│   └── server.py            # FastMCP server, 8 tools, metadata formatting
├── tests/
│   ├── test_config.py       # Config defaults, env overrides, reload
│   ├── test_api_client.py   # Effort mapping, response parsing, retry, cost
│   ├── test_critic.py       # Prompts, review flow, followup, health check
│   └── test_server.py       # Tool registration, parameter parsing, metadata
├── skill/
│   └── SKILL.md             # Kilo Code skill (инструкция для агентов)
├── docs/                    # GRACE framework artifacts
├── .env.example             # Template for environment variables
├── pyproject.toml            # Package metadata, dependencies, entry points
└── README.md
```

### Module contracts

| Module | PURPOSE |
|--------|---------|
| `config.py` | Загрузка и валидация конфигурации из `.env` через pydantic-settings. Hot reload. |
| `api_client.py` | Async HTTP-клиент к Polza.AI Responses API. Retry, dynamic timeout, cost calculation. |
| `critic.py` | System prompts (3 специализированных), orchestration functions, health check. |
| `server.py` | FastMCP сервер с 8 инструментами. Parameter parsing, metadata footer formatting, self-update. |

### Ключевые технические решения

**Persistent AsyncClient** — httpx.AsyncClient создаётся один раз и переиспользуется между запросами. Timeout передаётся per-request через `httpx.Timeout` в `.post()`, не в конструктор клиента. Это позволяет динамически менять timeout в зависимости от agent_count.

**Dynamic timeout** — 4 агента → max 90s, 16 агентов → полный timeout из конфига (default 300s). Уменьшает ожидание для быстрых запросов.

**Retry with exponential backoff** — 429 (rate limit) и 5xx (server error) retry до 2 раз с ожиданием `2^attempt` секунд. 401 (auth error) не ретраится.

**Cost calculation** — `(input_tokens / 1_000_000 * price_input) + (output_tokens / 1_000_000 * price_output)`. Цены из env vars за 1M токенов.

**Hot reload** — `reload_config_tool()` обновляет module-level `config` in-place через `object.__setattr__` + `lru_cache.cache_clear()`. Все модули видят новые значения без restart. Закрывает stale HTTP-клиент.

---

## Testing

```bash
# Установить dev-зависимости
pip install -e ".[dev]"

# Запустить все тесты
python -m pytest tests/ -v

# С выводом покрытия
python -m pytest tests/ -v --cov=grok_critic --cov-report=term-missing
```

**96 тестов** покрывают: config defaults и env overrides, effort mapping, response parsing, usage extraction (включая cost_rub и cached_tokens), cost calculation, retry logic, persistent client, JSON decode error handling, error body parsing (402/502/503), tool registration (8 tools), parameter parsing, metadata formatting с разделителями, hot reload, server restart, self_update flow, health check с balance API.

---

## Deployment на VM

### 1. Подготовка VM

```bash
# На VM — установить Python 3.11+
sudo apt update && sudo apt install python3 python3-pip python3-venv

# Создать директорию
mkdir -p /opt/grok-critic-mcp
```

### 2. Деплой через SSH

```bash
# С локальной машины
scp -r grok-critic-mcp/ user@vm:/opt/grok-critic-mcp/
```

### 3. Установка на VM

```bash
cd /opt/grok-critic-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Создать .env
cp .env.example .env
nano .env  # вставить API ключ
```

### 4. Регистрация в MCP-клиенте на VM

В конфиге MCP-клиента (на машине, где запущен AI-агент):

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

> **Note:** Для SSH-based MCP нужен настроенный SSH key без пароля (passwordless).

### 5. Systemd service (опционально, для HTTP транспорта)

Если нужен HTTP-based доступ вместо stdio:

```ini
# /etc/systemd/system/grok-critic.service
[Unit]
Description=Grok Critic MCP Server
After=network.target

[Service]
Type=simple
User=grok-critic
WorkingDirectory=/opt/grok-critic-mcp
ExecStart=/opt/grok-critic-mcp/.venv/bin/python -m grok_critic.server
Restart=on-failure
RestartSec=5
EnvironmentFile=/opt/grok-critic-mcp/.env

[Install]
WantedBy=multi-user.target
```

---

## Cost Estimation

При текущих ценах Polza.AI ($2.6 за 1M input, $6.6 за 1M output):

| Тип вызова | Input tokens | Output tokens | Стоимость |
|------------|-------------|--------------|-----------|
| Quick review (4 agents) | ~3,000 | ~2,000 | ~$0.02 |
| Full review (16 agents) | ~18,000 | ~15,000 | ~$0.15 |
| Architecture review (16 agents) | ~20,000 | ~18,000 | ~$0.18 |
| Followup question | ~25,000 | ~10,000 | ~$0.13 |

Цены могут меняться — проверяйте на [Polza.AI](https://polza.ai). Обновите `.env` и вызовите `reload_config_tool()`.

---

## Troubleshooting

### `Status: degraded` / `POLZA_API_KEY is not set`

API-ключ не найден. Проверьте:
1. Файл `.env` существует в корне проекта
2. Переменная `POLZA_API_KEY` заполнена
3. Для MCP-клиента — ключ можно передать через `env` в конфиге

### Стоимость не отображается в ответе

Цены в `.env` равны `0` или отсутствуют. Установите:
```ini
POLZA_PRICE_INPUT_PER_1M=2.6
POLZA_PRICE_OUTPUT_PER_1M=6.6
```
И вызовите `reload_config_tool()`.

### Timeout при 16 агентах

grok-4.20-multi-agent с 16 агентами может работать 2–3 минуты. Увеличьте:
```ini
POLZA_TIMEOUT_SECONDS=300
```
И в MCP-клиенте: `"timeout": 300000` (в миллисекундах).

### `os._exit(0)` в restart_server не работает

MCP-клиент должен автоматически перезапускать сервер при падении процесса. Если этого не происходит — перезапустите MCP-клиент вручную.

### LSP errors в IDE

При установке через `pip install -e .` — это нормально. Пакет установлен в system-wide Python, LSP не видит его. Тесты при этом проходят корректно.

---

## License

MIT

---

*Built with GRACE methodology — Graph-RAG Anchored Code Engineering.*
