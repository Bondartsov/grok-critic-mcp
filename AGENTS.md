# GRACE Framework - Project Engineering Protocol

## Keywords
MCP, grok, multi-agent, critic, xAI, Polza.AI, code review, architecture review, security audit, Responses API, FastMCP

## Annotation
MCP сервер-обёртка для grok-4.20-multi-agent через Polza.AI (Responses API). 8 MCP tools: critic_review, architecture_review, security_audit, critic_followup, check_health, reload_config_tool, restart_server, self_update + терминальный CLI (grok-critic: serve/health/doctor/review/followup/logs/config) — ревью и диагностика работают из Bash даже при отвалившемся MCP. file_path — в MCP-схеме у трёх content-инструментов (content не обязателен при file_path; critic_followup file_path не принимает); sandbox — opt-in POLZA_ALLOW_FILE_PATH, разрешённые корни = директория проекта сессии (cwd процесса сервера, не используется как корень если внутри неё лежит $HOME) + POLZA_ALLOWED_READ_DIRS, плюс glob-denylist секретов (SEC-02/03/DENY). Retry с общим дедлайном (REL-06), in-flight dedup, semaphore и дневной бюджет в ₽ (FEAT-BUDGET, POLZA_DAILY_BUDGET_RUB, soft limit по фактической cost_rub), followup по review_id через пер-файловый дисковый ReviewStore (db/reviews/, race-free, переживает рестарты, общий с CLI), JSON-режим, injection-guard. Стоимость — только в ₽ (тариф Polza.AI GET /models/{model}, кэш 1ч; неудача — 60с; оценка по тарифу при отсутствии cost_rub в ответе API — cost_is_estimate); устаревшие POLZA_PRICE_*/POLZA_DAILY_BUDGET_USD (до 1.12.0) не ломают запуск — один DEPRECATED-warning с именами ключей. Внешний критик через MCP + универсальный skill в Kilo Code, ZCode, Claude Code, Cursor. Версия 1.12.0, все модули STATUS=complete, 406 тестов.

## Core Principles

### 1. Never Write Code Without a Contract
Before generating or editing any module, create or update its MODULE_CONTRACT with PURPOSE, SCOPE, INPUTS, and OUTPUTS. The contract is the source of truth. Code implements the contract, not the other way around.

### 2. Semantic Markup Is Load-Bearing Structure
Markers like `# START_BLOCK_<NAME>` and `# END_BLOCK_<NAME>` are navigation anchors, not documentation. They must be:
- uniquely named
- paired
- proportionally sized so one block fits inside an LLM working window

### 3. Knowledge Graph Is Always Current
`docs/knowledge-graph.xml` is the project map. When you add a module, move a module, rename exports, or add dependencies, update the graph so future agents can navigate deterministically.

### 4. Verification Is a First-Class Artifact
Testing, traces, and log anchors are designed before large execution waves. `docs/verification-plan.xml` is part of the architecture, not an afterthought.

### 5. Top-Down Synthesis
Code generation follows:
`RequirementsAnalysis -> TechnologyStack -> DevelopmentPlan -> VerificationPlan -> Code + Tests`

### 6. Governed Autonomy
Agents have freedom in HOW to implement, but not in WHAT to build. Contracts, plans, graph references, and verification requirements define the allowed space.

## Semantic Markup Reference

### Module Level (Python)
```python
# FILE: path/to/file.py
# VERSION: 1.0.0
# START_MODULE_CONTRACT
#   PURPOSE: [What this module does - one sentence]
#   SCOPE: [What operations are included]
#   DEPENDS: [List of module dependencies]
#   LINKS: [Knowledge graph references]
# END_MODULE_CONTRACT
```

### Code Block Level
```python
# START_BLOCK_VALIDATE_INPUT
# ... code ...
# END_BLOCK_VALIDATE_INPUT
```

## Logging and Trace Convention
```python
logger.info("[ModuleName][function_name][BLOCK_NAME] message", extra={"correlation_id": cid})
```

## File Structure
```
docs/
  requirements.xml
  technology.xml
  development-plan.xml
  verification-plan.xml
  knowledge-graph.xml
  operational-packets.xml
src/
  grok_critic/
    __init__.py
    server.py
    api_client.py
    critic.py
    config.py
    cli.py
tests/
  test_server.py
  test_api_client.py
  test_critic.py
  test_config.py
  test_cli.py
  test_package.py
```
