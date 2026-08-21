---
name: grok-critic
description: Deep code review, architecture analysis and security audit via grok-4.20-multi-agent (16 reasoning agents). Use for post-implementation review, architecture validation, security checks, and bug-fix verification.
---

# Grok Critic Skill

## What

MCP-сервер `grok-critic` оборачивает модель `grok-4.20-multi-agent` (xAI) через Polza.AI для глубокого ревью кода. Модель запускает 16 reasoning agents параллельно и формирует консенсус.

## When (triggers)

| Trigger                                          | Tool                  | Priority    |
| ------------------------------------------------ | --------------------- | ----------- |
| Planning / architecture / system-design завершён | `architecture_review` | Obligatory  |
| Написан существенный код (>50 строк)             | `critic_review`       | Obligatory  |
| Баг-фикс не получился с первой попытки           | `critic_review`       | Obligatory  |
| Security-sensitive код (auth, payments, crypto)  | `security_audit`      | Obligatory  |
| Перед merge / PR                                 | `critic_review`       | Recommended |
| Спорное архитектурное решение                    | `architecture_review` | Recommended |

## MCP Tools

### 1. `critic_review` — общее ревью кода

```
grok-critic_critic_review(
  content: str,            # ОБЯЗАТЕЛЬНО — код для ревью
  context: str | None,     # проект, язык, назначение
  agent_count: int | None, # 4=быстро (~30s), 16=глубоко (~2-3min)
  focus_areas: str | None, # "security,performance,SOLID,DRY,architecture"
  output_format: str | None # "json" → строгий JSON {summary, findings[]}; по умолчанию текст
)
```

### 2. `architecture_review` — ревью архитектуры

```
grok-critic_architecture_review(
  content: str,           # описание архитектуры, диаграмма, код
  context: str | None,    # tech stack, constraints, team size
  agent_count: int | None  # override
)
```

Фокус: паттерны, зависимости, масштабируемость, риски.

### 3. `security_audit` — security аудит

```
grok-critic_security_audit(
  content: str,           # код или конфигурация
  context: str | None,    # framework, deployment, threat model
  agent_count: int | None  # override
)
```

Классификация: 🔴 CRITICAL / 🟡 HIGH / 🟠 MEDIUM / 🔵 LOW

### 4. `critic_followup` — уточняющий вопрос

```
grok-critic_critic_followup(
  question: str,          # ОБЯЗАТЕЛЬНО — уточняющий вопрос
  previous_review: str | None, # ПОЛНЫЙ текст предыдущего ответа критика
  review_id: str | None,  # Review ID из metadata footer — ПРЕДПОЧТИТЕЛЬНЕЕ
  agent_count: int | None # override
)
```

Использовать если ответ критика неполный или нужно углубиться в конкретный аспект.

**Экономия токенов:** вместо `previous_review` (полный текст, ~25k input-токенов) передавайте `review_id` из metadata footer предыдущего ревью — сервер восстановит диалог из внутреннего хранилища. Fallback на `previous_review`, если сервер перезапускался (хранилище in-memory).

### 5. `check_health` — проверка доступности

```
grok-critic_check_health()
```

Без параметров. Показывает статус, модель, pricing и **баланс в ₽** (запрашивает Polza.AI Balance API).

### 6. `reload_config_tool` — горячая перезагрузка .env

```
grok-critic_reload_config_tool()
```

Без параметров. Перечитывает `.env` без перезапуска. Использовать после изменения цен, API-ключа, таймаута.

### 7. `restart_server` — полный перезапуск

```
grok-critic_restart_server(reason: str | None)
```

Жёсткий выход процесса. MCP-клиент автоматически перезапустит сервер.

### 8. `self_update` — автообновление с GitHub

```
grok-critic_self_update()
```

Без параметров. Делает `git pull` + `pip install -e .` + `os._exit(0)`. MCP-клиент автоматически перезапустит сервер с новым кодом. Использовать когда новая версия запушена в GitHub.

**Feature flag:** инструмент выключен по умолчанию. Включается `POLZA_ALLOW_SELF_UPDATE=true` в `.env` + `reload_config_tool()` (или рестарт). Пока флаг выключен — возвращает отказ без побочных эффектов.

## Параметр `file_path` (critic_review / architecture_review / security_audit)

Вместо `content` можно передать `file_path` — сервер сам прочитает файл и подставит как content (context по умолчанию = `File: <путь>`).

**⚠️ Opt-in (SEC-03):** по умолчанию `file_path` **отключён** — cwd MCP-клиента непредсказуем. Для включения: `POLZA_ALLOW_FILE_PATH=true` в `.env` + `reload_config_tool()`.

**Sandbox (при включённом file_path):** файл обязан лежать внутри разрешённых корней: рабочая директория сервера + директории из `POLZA_ALLOWED_READ_DIRS` (разделитель `;` на Windows). Выход через `..` отклоняется. Файлы-секреты блокируются всегда по glob-маскам (`.env*`, `*credential*`, `id_rsa*`, `*.pem`, `*.key`, `.git/config` и т.п.) — их содержимое не должно уходить во внешний API. Лимит размера — 1 МБ. При отказе возвращается `Access denied` без обращения к API.

`critic_followup` параметр `file_path` **не поддерживает** — вернёт явную ошибку.

## Agent Count Guide

| Agents | Effort | Timeout            | When to use                         |
| ------ | ------ | ------------------ | ----------------------------------- |
| 4      | low    | ~90s               | Quick sanity check, small snippets  |
| 8      | high   | ~150s              | Medium review                       |
| 16     | high   | ~180s (из конфига) | Full review, architecture, security |

Default: 16 (из `.env`).

**Validation:** `agent_count` автоматически clamps к диапазону 1-64. Некорректные значения (0, -5, 100) будут приведены к границам. Config-level валидация (pydantic): `ge=1, le=64`.

## Response Format

Каждый ответ содержит metadata footer:

```
---
⏱ Elapsed: 87 s
📊 Metadata: model=x-ai/grok-4.20-multi-agent | agents=16 | effort=high
📈 Tokens: input=18 231 output=15 434 total=33 665
💾 Cached: 18 000/18 231 (99%)
💰 Cost: 1.23 ₽ | $0.1493
🏷️ Review ID: rev_1f866fc571eb
```

- **Elapsed** — время выполнения запроса (FEAT-PROGRESS: пока ревью идёт, сервер шлёт heartbeat-уведомления в сессию)
- **Tokens** — с thin-space разделителями (1 234 567)
- **Cached** — показывается только если есть кешированные токены (Polza.AI автоматически кеширует system prompt, чтение из кеша в 4 раза дешевле)
- **Cost** — сначала реальная стоимость в ₽ (из Polza.AI API `usage.cost_rub`), затем расчётная в $ (по ценам из конфига)

## Error Messages

Ошибки парсятся из тела ответа Polza.AI (`{error: {code, message}}`), сообщения на русском:

| Код | Пример сообщения                                                   |
| --- | ------------------------------------------------------------------ |
| 401 | `Ошибка авторизации: API key invalid`                              |
| 402 | `Недостаточно средств: Недостаточно средств на балансе`            |
| 429 | `Превышен лимит запросов: Too many requests for ...`               |
| 502 | `Провайдер недоступен: xAI provider unavailable`                   |
| 503 | `Нет доступных провайдеров: No providers available`                |
| —   | `Превышен дневной бюджет: $0.51 из $0.50` (POLZA_DAILY_BUDGET_USD) |

## Workflow Examples

### Post-implementation review

```
1. critic_review(content=code, context="Python FastAPI auth module", focus_areas="security,performance")
2. Если найдены критические проблемы → исправить → critic_review повторно
3. Если есть спорные моменты → critic_followup(previous_review=result, question="...")
```

### Architecture validation

```
1. architecture_review(content=architecture_description, context="Microservices, 5 devs, PostgreSQL")
2. critic_followup(previous_review=result, question="What about event sourcing instead?")
```

### Security audit

```
1. security_audit(content=auth_code, context="JWT + OAuth2, deployed to AWS")
2. Все 🔴 CRITICAL → исправить ОБЯЗАТЕЛЬНО
3. Все 🟡 HIGH → исправить перед production
```

### Update to latest version

```
1. self_update() — подтянет новый код с GitHub и перезапустит
```

## Cost Awareness

При $2.6/$6.6 за 1M токенов типичный вызов с 16 агентами стоит $0.10–0.25. Не вызывать критика для тривиальных задач (изменение 5 строк, форматирование, rename).

**Кеширование:** Polza.AI автоматически кеширует system prompt через `prompt_cache_key`. Повторные вызовы с тем же system prompt обходятся дешевле (cached tokens ~0.25x стоимости).

**Followup по review_id** вместо полного `previous_review` экономит ~25k input-токенов на каждый уточняющий вопрос.

**Защита от перерасхода:** сервер считает суточный расход (`check_health` показывает «Today: N calls | $X | ₽») и при `POLZA_DAILY_BUDGET_USD > 0` отклоняет вызовы сверх лимита ДО обращения к API. Параллельные дублирующие вызовы с тем же контентом склеиваются в один платный запрос (in-flight dedup).

## Rules

1. **Всегда** передавай `context` — критик работает лучше с контекстом проекта
2. **Всегда** используй `focus_areas` для целенаправленного ревью
3. **Всегда** читай ответ полностью — критик может найти неожиданные проблемы
4. **Если** критик нашёл 🔴 CRITICAL → исправь ПЕРЕД продолжением
5. **Всегда отвечай критику через `critic_followup`** — если не согласен с замечанием, приведи аргументы. Если есть контекст который критик не учёл — объясни. Если критик переоценил проблему — оспорь. Критик пересматривает оценку при сильных аргументах. **НЕ глотай замечания молча.** Передавай `review_id` вместо полного текста ревью.
6. **Никогда** не вызывай критика для кода, который ты сам только что сгенерировал и ещё не проверил — сначала прочитай что написал
7. **Следи за балансом** — `check_health` показывает баланс в ₽ и суточный расход. Если баланс низкий или расход у дневного лимита — предупреди пользователя.
