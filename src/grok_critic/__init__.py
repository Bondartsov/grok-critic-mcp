# FILE: src/grok_critic/__init__.py
# VERSION: 1.11.2
# START_MODULE_CONTRACT
#   PURPOSE: Package public API exports
#   SCOPE: Re-export main functions and classes for external use
#   DEPENDS: M-CRITIC, M-SERVER, M-CONFIG, M-API
#   LINKS: M-CONFIG, M-API, M-CRITIC, M-SERVER
# END_MODULE_CONTRACT

from grok_critic.api_client import CritiqueResult, ResponsesClient
from grok_critic.config import AppConfig, load_config
from grok_critic.critic import (
    ARCHITECTURE_SYSTEM_PROMPT,
    CRITIC_SYSTEM_PROMPT,
    SECURITY_SYSTEM_PROMPT,
    do_architecture_review,
    do_security_audit,
    followup,
    general_review,
    health_check,
)
from grok_critic.server import (
    architecture_review,
    check_health,
    critic_followup,
    critic_review,
    main,
    reload_config_tool,
    restart_server,
    security_audit,
    self_update,
    server,
)

__all__ = [
    "ARCHITECTURE_SYSTEM_PROMPT",
    "AppConfig",
    "CRITIC_SYSTEM_PROMPT",
    "CritiqueResult",
    "SECURITY_SYSTEM_PROMPT",
    "ResponsesClient",
    "architecture_review",
    "check_health",
    "critic_followup",
    "critic_review",
    "do_architecture_review",
    "do_security_audit",
    "followup",
    "general_review",
    "health_check",
    "load_config",
    "main",
    "reload_config_tool",
    "restart_server",
    "security_audit",
    "self_update",
    "server",
]
