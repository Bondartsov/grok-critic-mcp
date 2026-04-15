# FILE: src/grok_critic/critic.py
# VERSION: 1.5.2
# START_MODULE_CONTRACT
#   PURPOSE: Critical code review orchestration via grok-4.20-multi-agent
#   SCOPE: Build review prompts, call API, followup questions, perform health checks
#   DEPENDS: M-API, M-CONFIG
#   LINKS: M-CRITIC
# END_MODULE_CONTRACT

from __future__ import annotations

import logging

import httpx

from grok_critic.api_client import CritiqueResult, ResponsesClient, MAX_CONTENT_CHARS
from grok_critic.config import config

logger = logging.getLogger("grok-critic.critic")


# START_BLOCK_SYSTEM_PROMPT
CRITIC_SYSTEM_PROMPT = (
    "Ты — опытный критик-ревьюер кода. Твоя задача — провести глубокий и беспристрастный анализ "
    "представленного кода.\n\n"
    "## Структура ревью\n\n"
    "### 1. Логические ошибки\n"
    "- Найди все логические ошибки и баги\n"
    "- Проверь граничные случаи и обработку исключений\n"
    "- Проверь корректность работы с типами данных\n\n"
    "### 2. Принципы проектирования\n"
    "- **SOLID**: проверь нарушение каждого из 5 принципов\n"
    "- **DRY**: найди дублирование кода и логики\n"
    "- **KISS**: укажи на излишнюю сложность\n\n"
    "### 3. Производительность\n"
    "- Найди N+1 запросы и неэффективные операции\n"
    "- Проверь утечки памяти и неиспользуемые ресурсы\n"
    "- Оцени асимптотическую сложность\n\n"
    "### 4. Безопасность\n"
    "- SQL injection, XSS, path traversal\n"
    "- Утечки секретов и ключей\n"
    "- Небезопасная десериализация\n\n"
    "### 5. Улучшения\n"
    "- Предложи конкретные рефакторинги с примерами кода\n"
    "- Укажи недостающие тесты\n"
    "- Оцени читаемость и поддерживаемость\n\n"
    "Отвечай на русском языке. Будь конкретен и конструктивен."
)


# END_BLOCK_SYSTEM_PROMPT


# START_BLOCK_BUILD_PROMPT
def _build_user_prompt(
    content: str,
    context: str | None = None,
    focus_areas: list[str] | None = None,
) -> str:
    parts: list[str] = []

    if context:
        parts.append(f"## Контекст\n{context}\n")

    if focus_areas:
        areas = ", ".join(focus_areas)
        parts.append(f"## Фокус внимания\nОбрати особое внимание на: {areas}\n")

    parts.append(f"## Код для ревью\n```\n{content}\n```")
    return "\n\n".join(parts)


# END_BLOCK_BUILD_PROMPT


# START_BLOCK_PERFORM_REVIEW
async def _perform_review(
    content: str,
    system_prompt: str,
    *,
    context: str | None = None,
    focus_areas: list[str] | None = None,
    agent_count: int | None = None,
    error_label: str = "ревью",
) -> CritiqueResult:
    """Shared review logic: validate content → build prompt → call API."""
    if not content.strip():
        return CritiqueResult(
            text="", model=config.model,
            agent_count=agent_count or config.agent_count,
            effort="low", error=f"Пустой контент для {error_label}",
        )

    if len(content) > MAX_CONTENT_CHARS:
        return CritiqueResult(
            text="", model=config.model,
            agent_count=agent_count or config.agent_count,
            effort="low",
            error=f"Контент слишком большой ({len(content)} символов). Максимум {MAX_CONTENT_CHARS}.",
        )

    count = agent_count if agent_count is not None else config.agent_count
    prompt = _build_user_prompt(content, context, focus_areas)
    client = ResponsesClient()
    return await client.call(prompt=prompt, agent_count=count, system_prompt=system_prompt)


# END_BLOCK_PERFORM_REVIEW


# START_BLOCK_STRUCTURED_REVIEW
async def structured_review(
    content: str,
    context: str | None = None,
    agent_count: int | None = None,
    focus_areas: list[str] | None = None,
) -> CritiqueResult:
    logger.info(
        "[Critic][structured_review][STRUCTURED_REVIEW] content_len=%d agent_count=%s",
        len(content),
        agent_count,
    )
    return await _perform_review(
        content, CRITIC_SYSTEM_PROMPT,
        context=context, focus_areas=focus_areas,
        agent_count=agent_count, error_label="ревью",
    )


# END_BLOCK_STRUCTURED_REVIEW


# START_BLOCK_FOLLOWUP
FOLLOWUP_SYSTEM_PROMPT = (
    "Ты — критик-ревьюер. Продолжаешь диалог. "
    "Ответь на уточняющий вопрос по предыдущему ревью."
)


async def followup(
    previous_review: str,
    question: str,
    agent_count: int | None = None,
) -> CritiqueResult:
    logger.info(
        "[Critic][followup][FOLLOWUP] prev_len=%d question_len=%d",
        len(previous_review),
        len(question),
    )

    if not previous_review.strip() or not question.strip():
        return CritiqueResult(
            text="",
            model=config.model,
            agent_count=agent_count or config.agent_count,
            effort="low",
            error="Пустой предыдущий ревью или вопрос",
        )

    count = agent_count if agent_count is not None else config.agent_count
    prompt = (
        f"## Предыдущее ревью\n{previous_review}\n\n"
        f"## Уточняющий вопрос\n{question}"
    )
    client = ResponsesClient()
    result = await client.call(
        prompt=prompt,
        agent_count=count,
        system_prompt=FOLLOWUP_SYSTEM_PROMPT,
    )

    logger.info(
        "[Critic][followup][FOLLOWUP] Followup complete, result_len=%d success=%s",
        len(result.text),
        result.success,
    )
    return result


# END_BLOCK_FOLLOWUP


# START_BLOCK_HEALTH_CHECK
async def health_check() -> dict:
    logger.info("[Critic][health_check][HEALTH_CHECK] Running health check")

    issues: list[str] = []

    if not config.api_key:
        issues.append("POLZA_API_KEY is not set")

    healthy = len(issues) == 0
    result: dict = {
        "status": "ok" if healthy else "degraded",
        "model": config.model,
        "base_url": config.base_url,
        "issues": issues,
    }

    if config.price_input_per_1m > 0 or config.price_output_per_1m > 0:
        result["pricing"] = {
            "input_per_1m": config.price_input_per_1m,
            "output_per_1m": config.price_output_per_1m,
        }

    # Query Polza.AI balance API
    if config.api_key:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{config.base_url}/balance",
                    headers={"Authorization": f"Bearer {config.api_key}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result["balance_rub"] = float(data.get("amount", 0))
                else:
                    issues.append(f"Balance API returned {resp.status_code}")
        except Exception as exc:
            logger.warning("[Critic][health_check][BALANCE] Failed to fetch balance: %s", exc)
            issues.append(f"Balance API error: {exc}")

    logger.info("[Critic][health_check][HEALTH_CHECK] status=%s", result["status"])
    return result


# END_BLOCK_HEALTH_CHECK


# START_BLOCK_SPECIALIZED_PROMPTS
ARCHITECTURE_SYSTEM_PROMPT = (
    "Ты — архитектор-критик. Специализируешься на анализе архитектурных решений.\n\n"
    "## Структура анализа\n\n"
    "### 1. Архитектурные паттерны\n"
    "- Соответствие выбранному паттерну (Modular Monolith, Microservices, DDD и т.д.)\n"
    "- Разделение ответственности между модулями/слоями\n"
    "- Чёткость границ контекстов (Bounded Contexts)\n\n"
    "### 2. Зависимости и связность\n"
    "- Направление зависимостей (Dependency Rule)\n"
    "- Циклические зависимости\n"
    "- Coupling vs Cohesion баланс\n\n"
    "### 3. Масштабируемость\n"
    "- Горизонтальное/вертикальное масштабирование\n"
    "- Узкие места (Bottlenecks)\n"
    "- Data flow и consistency\n\n"
    "### 4. Риски\n"
    "- Single points of failure\n"
    "- Технический долг\n"
    "- Migration complexity\n\n"
    "Отвечай на русском языке. Предлагай конкретные альтернативы."
)

SECURITY_SYSTEM_PROMPT = (
    "Ты — security-аудитор. Специализируешься на поиске уязвимостей.\n\n"
    "## Чеклист аудита\n\n"
    "### 1. Injection-атаки\n"
    "- SQL injection (parameterized queries?)\n"
    "- XSS (output encoding?)\n"
    "- Command injection (shell escaping?)\n"
    "- Path traversal (input validation?)\n\n"
    "### 2. Аутентификация и авторизация\n"
    "- Слабые пароли, отсутствие MFA\n"
    "- Session management (fixation, hijacking)\n"
    "- Privilege escalation\n"
    "- IDOR (Insecure Direct Object Reference)\n\n"
    "### 3. Данные и секреты\n"
    "- Hardcoded credentials и API keys\n"
    "- Небезопасное хранение (plaintext)\n"
    "- Утечки в логах\n"
    "- Небезопасная десериализация\n\n"
    "### 4. Инфраструктура\n"
    "- CORS misconfiguration\n"
    "- Insecure defaults\n"
    "- Missing rate limiting\n"
    "- SSRF / CSRF\n\n"
    "Классифицируй: 🔴 CRITICAL / 🟡 HIGH / 🟠 MEDIUM / 🔵 LOW\n"
    "Отвечай на русском языке."
)


# END_BLOCK_SPECIALIZED_PROMPTS


# START_BLOCK_SPECIALIZED_REVIEWS
async def do_architecture_review(
    content: str,
    context: str | None = None,
    agent_count: int | None = None,
) -> CritiqueResult:
    """Специализированный архитектурный ревью."""
    return await _perform_review(
        content, ARCHITECTURE_SYSTEM_PROMPT,
        context=context,
        focus_areas=["architecture", "scalability", "dependencies"],
        agent_count=agent_count, error_label="архитектурного ревью",
    )


async def do_security_audit(
    content: str,
    context: str | None = None,
    agent_count: int | None = None,
) -> CritiqueResult:
    """Специализированный security-аудит."""
    return await _perform_review(
        content, SECURITY_SYSTEM_PROMPT,
        context=context,
        focus_areas=["security", "vulnerabilities", "secrets"],
        agent_count=agent_count, error_label="security аудита",
    )


# END_BLOCK_SPECIALIZED_REVIEWS
