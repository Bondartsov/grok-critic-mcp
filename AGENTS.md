# GRACE Framework - Project Engineering Protocol

## Keywords
MCP, grok, multi-agent, critic, xAI, Polza.AI, code review, Responses API, FastMCP

## Annotation
MCP сервер-обёртка для grok-4.20-multi-agent через Polza.AI (Responses API). Предоставляет tool `critic_review` для критического ревью кода, архитектуры и решений. Используется как субагент "Критик" в Kilo Code.

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
tests/
  test_api_client.py
  test_critic.py
  test_config.py
```
