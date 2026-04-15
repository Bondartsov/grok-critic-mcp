---
name: grok-critic
description: Deep code review, architecture analysis and security audit via grok-4.20-multi-agent (16 reasoning agents). Use for post-implementation review, architecture validation, security checks, and bug-fix verification.
---

# Grok Critic Skill

## What

MCP-сервер `grok-critic` оборачивает модель `grok-4.20-multi-agent` (xAI) через Polza.AI для глубокого ревью кода. Модель запускает 16 reasoning agents параллельно и формирует консенсус.

## When (triggers)

| Trigger | Tool | Priority |
|---------|------|----------|
| Planning / architecture / system-design завершён | `architecture_review` | Obligatory |
| Написан существенный код (>50 строк) | `critic_review` | Obligatory |
| Баг-фикс не получился с первой попытки | `critic_review` | Obligatory |
| Security-sensitive код (auth, payments, crypto) | `security_audit` | Obligatory |
| Перед merge / PR | `critic_review` | Recommended |
| Спорное архитектурное решение | `architecture_review` | Recommended |

## MCP Tools

### 1. `critic_review` — общее ревью кода

```
grok-critic_critic_review(
  content: str,           # ОБЯЗАТЕЛЬНО — код для ревью
  context: str | None,    # проект, язык, назначение
  agent_count: int | None, # 4=быстро (~30s), 16=глубоко (~2-3min)
  focus_areas: str | None  # "security,performance,SOLID,DRY,architecture"
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
  previous_review: str,   # ПОЛНЫЙ текст предыдущего ответа критика
  question: str,          # уточняющий вопрос
  agent_count: int | None  # override
)
```

Использовать если ответ критика неполный или нужно углубиться в конкретный аспект.

### 5. `check_health` — проверка доступности

```
grok-critic_check_health()
```

Без параметров. Показывает статус, модель, pricing.

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

## Agent Count Guide

| Agents | Effort | Timeout | When to use |
|--------|--------|---------|-------------|
| 4 | low | ~90s | Quick sanity check, small snippets |
| 16 | high | ~300s | Full review, architecture, security |

Default: 16 (из `.env`).

## Response Format

Каждый ответ содержит metadata footer:

```
---
📊 Metadata: model=x-ai/grok-4.20-multi-agent | agents=16 | effort=high
📈 Tokens: input=18231 output=15434 total=33665
💰 Cost: $0.1493
🏷️ Review ID: rev_1f866fc571eb
```

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

## Cost Awareness

При $2.6/$6.6 за 1M токенов типичный вызов с 16 агентами стоит $0.10–0.25. Не вызывать критика для тривиальных задач (изменение 5 строк, форматирование, rename).

## Rules

1. **Всегда** передавай `context` — критик работает лучше с контекстом проекта
2. **Всегда** используй `focus_areas` для целенаправленного ревью
3. **Всегда** читай ответ полностью — критик может найти неожиданные проблемы
4. **Если** критик нашёл 🔴 CRITICAL → исправь ПЕРЕД продолжением
5. **Если** не согласен → `critic_followup` с аргументацией, а не игнорирование
6. **Никогда** не вызывай критика для кода, который ты сам только что сгенерировал и ещё не проверил — сначала прочитай что написал
