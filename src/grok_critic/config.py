# FILE: src/grok_critic/config.py
# VERSION: 1.6.0
# START_MODULE_CONTRACT
#   PURPOSE: Configuration management via pydantic-settings with env vars
#   SCOPE: Load and validate API key, model, timeout, agent settings, logging
#   DEPENDS: pydantic-settings, python-dotenv
#   LINKS: M-CONFIG
# END_MODULE_CONTRACT

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("grok-critic.config")


# START_BLOCK_SETTINGS_MODEL
class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLZA_",
        env_file=str(Path(__file__).resolve().parent.parent.parent / ".env"),
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
