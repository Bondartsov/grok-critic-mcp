# FILE: src/grok_critic/config.py
# VERSION: 1.11.1
# START_MODULE_CONTRACT
#   PURPOSE: Configuration management via pydantic-settings with env vars
#   SCOPE: Load and validate API key, model, timeout, agent settings, logging
#   DEPENDS: pydantic-settings, python-dotenv
#   LINKS: M-CONFIG
# END_MODULE_CONTRACT

from __future__ import annotations

import logging
import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("grok-critic.config")


# START_BLOCK_ENV_FILE_RESOLUTION
def _resolve_env_file() -> str:
    """Путь к .env с приоритетом: POLZA_ENV_FILE → cwd/.env → legacy (рядом с пакетом).

    FIX-ENV-PATH: старый путь вычислялся от расположения config.py и при обычном
    ``pip install`` указывал на site-packages, где .env не бывает. Теперь сначала
    ищем .env в текущей рабочей директории (как запущен сервер), затем падаем
    на legacy-путь (pip install -e . из клона репозитория).
    """
    explicit = os.getenv("POLZA_ENV_FILE", "").strip()
    if explicit:
        return explicit
    cwd_candidate = Path.cwd() / ".env"
    if cwd_candidate.is_file():
        return str(cwd_candidate)
    return str(Path(__file__).resolve().parent.parent.parent / ".env")


# END_BLOCK_ENV_FILE_RESOLUTION


# START_BLOCK_SETTINGS_MODEL
class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLZA_",
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: SecretStr = Field(min_length=1, description="Polza.AI API key (POLZA_API_KEY)")
    base_url: str = Field(default="https://polza.ai/api/v1")
    model: str = Field(default="x-ai/grok-4.20-multi-agent")
    agent_count: int = Field(default=16, ge=1, le=64)
    timeout_seconds: int = Field(default=180, ge=1)
    log_level: str = Field(default="WARNING")
    log_file: str = Field(default="")  # пустой = stderr (MCP stdio не занимается)
    price_input_per_1m: float = Field(default=0.0)
    price_output_per_1m: float = Field(default=0.0)
    allow_self_update: bool = Field(default=False)
    # Дополнительные директории, откуда разрешено читать файлы через file_path.
    # Разделитель — os.pathsep (';' на Windows, ':' на Unix).
    # Пусто = разрешена только текущая рабочая директория (cwd) сервера.
    allowed_read_dirs: str = Field(default="")
    # Тюнинг клиента (раньше — hardcoded константы в api_client.py)
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_backoff_base: float = Field(default=2.0, ge=0.0)
    max_content_chars: int = Field(default=100_000, ge=1)  # ~100KB — защита от DoS по стоимости
    timeout_low: int = Field(default=90, ge=1)    # таймаут при agent_count <= 4
    timeout_mid: int = Field(default=150, ge=1)   # таймаут при 4 < agent_count <= 8
    # SEC-03: file_path выключен по умолчанию — cwd MCP-клиента непредсказуем
    # (часто это $HOME), поэтому чтение файлов включается явным opt-in.
    allow_file_path: bool = Field(default=False)
    # REL-06: общий дедлайн retry-цикла (сек). 0 = авто (= timeout_seconds):
    # суммарное время попыток не должно превышать таймаут MCP-клиента,
    # иначе клиент отваливается и платно ретраит поверх живого запроса.
    retry_deadline_seconds: float = Field(default=0.0, ge=0.0)
    # FEAT-BUDGET: дневной лимит расходов в $ по расчётной стоимости (cost_usd).
    # 0 = без лимита. Превышение → ошибка ДО обращения к платному API.
    daily_budget_usd: float = Field(default=0.0, ge=0.0)
    # FEAT-BUDGET: максимум одновременных платных запросов к API (semaphore).
    max_concurrent_requests: int = Field(default=2, ge=1, le=16)
    # FEAT-CLI: файл store'а диалогов (review_id переживает рестарты, работает из CLI).
    # Пусто = <repo>/db/reviews/ (per-id файлы, db/ в .gitignore).
    store_path: str = Field(default="")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}, got {v!r}")
        return upper


# END_BLOCK_SETTINGS_MODEL


# START_BLOCK_SETUP_LOGGING
def _setup_logging(cfg: AppConfig) -> None:
    root = logging.getLogger("grok-critic")
    root.setLevel(cfg.log_level)

    # Убираем дефолтные handler'ы
    root.handlers.clear()

    formatter = logging.Formatter(
        "[%(asctime)s] %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if cfg.log_file:
        # Логирование в файл
        handler = logging.FileHandler(cfg.log_file, encoding="utf-8")
    else:
        # Логирование в stderr (stdout занят под MCP stdio protocol)
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(formatter)
    root.addHandler(handler)


# END_BLOCK_SETUP_LOGGING


# START_BLOCK_LOAD_CONFIG
@lru_cache(maxsize=1)
def load_config() -> AppConfig:
    cfg = AppConfig()
    _setup_logging(cfg)
    logger.info("[Config][load_config][LOAD_CONFIG] model=%s timeout=%ds log_level=%s", cfg.model, cfg.timeout_seconds, cfg.log_level)
    return cfg


# END_BLOCK_LOAD_CONFIG


# START_BLOCK_RELOAD_CONFIG
def reload_config() -> AppConfig:
    """Hot-reload config from .env without restarting the server.

    Updates the module-level ``config`` object *in-place* so that every
    module which imported ``from grok_critic.config import config``
    immediately sees the new values — no restart required.
    """
    global config
    load_config.cache_clear()
    new_cfg = load_config()
    # In-place update: all external references point to the same object.
    for field_name in new_cfg.__class__.model_fields:
        object.__setattr__(config, field_name, getattr(new_cfg, field_name))
    # Also update the module-level binding for late importers.
    config = new_cfg
    logger.info(
        "[Config][reload_config][RELOAD] model=%s timeout=%ds prices=$%.2f/$%.2f per 1M",
        config.model, config.timeout_seconds,
        config.price_input_per_1m, config.price_output_per_1m,
    )
    return config


# END_BLOCK_RELOAD_CONFIG


# START_BLOCK_MODULE_INSTANCE
config = load_config()


# END_BLOCK_MODULE_INSTANCE
