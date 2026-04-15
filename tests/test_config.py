# FILE: tests/test_config.py
# VERSION: 1.1.0
# START_MODULE_CONTRACT
#   PURPOSE: Tests for M-CONFIG configuration loading and validation
#   SCOPE: Test env var reading, defaults, log_level validation, price fields
#   DEPENDS: M-CONFIG
#   LINKS: M-CONFIG
# END_MODULE_CONTRACT

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic_settings import SettingsConfigDict

from grok_critic.config import AppConfig


def _make_no_env(**kwargs) -> AppConfig:
    no_env_config = SettingsConfigDict(
        env_prefix="POLZA_",
        env_file=None,
        extra="ignore",
    )
    with patch.object(AppConfig, "model_config", no_env_config):
        return AppConfig(**kwargs)


# START_BLOCK_DEFAULTS
class TestAppConfigDefaults:
    def test_default_base_url(self) -> None:
        cfg = _make_no_env(api_key="test-key")
        assert cfg.base_url == "https://polza.ai/api/v1"

    def test_default_model(self) -> None:
        cfg = _make_no_env(api_key="test-key")
        assert cfg.model == "x-ai/grok-4.20-multi-agent"

    def test_default_agent_count(self) -> None:
        cfg = _make_no_env(api_key="test-key")
        assert cfg.agent_count == 16

    def test_default_timeout_seconds(self) -> None:
        cfg = _make_no_env(api_key="test-key")
        assert cfg.timeout_seconds == 180

    def test_default_log_level(self) -> None:
        cfg = _make_no_env(api_key="test-key")
        assert cfg.log_level == "WARNING"

    def test_default_log_file(self) -> None:
        cfg = _make_no_env(api_key="test-key")
        assert cfg.log_file == ""

    def test_default_price_input_per_1m(self) -> None:
        cfg = _make_no_env(api_key="test-key")
        assert cfg.price_input_per_1m == 0.0

    def test_default_price_output_per_1m(self) -> None:
        cfg = _make_no_env(api_key="test-key")
        assert cfg.price_output_per_1m == 0.0


# END_BLOCK_DEFAULTS


# START_BLOCK_ENV_OVERRIDE
class TestEnvOverride:
    def test_api_key_from_env(self) -> None:
        with patch.dict(os.environ, {"POLZA_API_KEY": "env-key-123"}):
            cfg = AppConfig()
            assert cfg.api_key == "env-key-123"

    def test_base_url_override(self) -> None:
        with patch.dict(os.environ, {"POLZA_BASE_URL": "http://localhost:8080"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.base_url == "http://localhost:8080"

    def test_timeout_override(self) -> None:
        with patch.dict(os.environ, {"POLZA_TIMEOUT_SECONDS": "60"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.timeout_seconds == 60

    def test_log_level_override(self) -> None:
        with patch.dict(os.environ, {"POLZA_LOG_LEVEL": "DEBUG"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.log_level == "DEBUG"

    def test_log_file_override(self) -> None:
        with patch.dict(os.environ, {"POLZA_LOG_FILE": "/tmp/grok-critic.log"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.log_file == "/tmp/grok-critic.log"

    def test_price_input_override(self) -> None:
        with patch.dict(os.environ, {"POLZA_PRICE_INPUT_PER_1M": "2.5"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.price_input_per_1m == 2.5

    def test_price_output_override(self) -> None:
        with patch.dict(os.environ, {"POLZA_PRICE_OUTPUT_PER_1M": "6.6"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.price_output_per_1m == 6.6


# END_BLOCK_ENV_OVERRIDE


# START_BLOCK_VALIDATION
class TestLogLevelValidation:
    def test_valid_levels(self) -> None:
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            cfg = AppConfig(api_key="test-key", log_level=level)
            assert cfg.log_level == level

    def test_case_insensitive(self) -> None:
        cfg = AppConfig(api_key="test-key", log_level="debug")
        assert cfg.log_level == "DEBUG"

    def test_invalid_level(self) -> None:
        with pytest.raises(ValueError, match="log_level must be one of"):
            AppConfig(api_key="test-key", log_level="VERBOSE")


# END_BLOCK_VALIDATION


# START_BLOCK_RELOAD_CONFIG
class TestReloadConfig:
    def test_reload_returns_appconfig(self) -> None:
        """reload_config() должен возвращать AppConfig с актуальными полями."""
        from grok_critic.config import reload_config, config
        result = reload_config()
        assert hasattr(result, "model")
        assert hasattr(result, "api_key")
        assert hasattr(result, "price_input_per_1m")
        # Module-level reference updated
        from grok_critic import config as cfg_mod
        assert cfg_mod.config is result

    def test_reload_picks_up_env_changes(self) -> None:
        """После изменения env var reload_config() возвращает новое значение."""
        from grok_critic.config import reload_config
        with patch.dict(os.environ, {"POLZA_TIMEOUT_SECONDS": "42"}):
            new_cfg = reload_config()
            assert new_cfg.timeout_seconds == 42
        # Restore original
        reload_config()


# END_BLOCK_RELOAD_CONFIG
