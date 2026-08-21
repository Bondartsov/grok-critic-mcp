# FILE: tests/test_config.py
# VERSION: 1.10.0
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
from pydantic import ValidationError
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

    def test_default_allow_file_path_false(self) -> None:
        """SEC-03: чтение файлов через file_path выключено по умолчанию."""
        cfg = _make_no_env(api_key="test-key")
        assert cfg.allow_file_path is False

    def test_default_daily_budget_usd(self) -> None:
        cfg = _make_no_env(api_key="test-key")
        assert cfg.daily_budget_usd == 0.0

    def test_default_max_concurrent_requests(self) -> None:
        cfg = _make_no_env(api_key="test-key")
        assert cfg.max_concurrent_requests == 2

    def test_default_retry_deadline_seconds(self) -> None:
        """REL-06: 0 = авто (= per-attempt timeout)."""
        cfg = _make_no_env(api_key="test-key")
        assert cfg.retry_deadline_seconds == 0.0


# END_BLOCK_DEFAULTS


# START_BLOCK_ENV_FILE_RESOLUTION
class TestEnvFileResolution:
    """FIX-ENV-PATH: путь к .env — POLZA_ENV_FILE → cwd/.env → legacy."""

    def test_explicit_env_var_wins(self, monkeypatch) -> None:
        from grok_critic.config import _resolve_env_file

        monkeypatch.setenv("POLZA_ENV_FILE", "/custom/path/.env")
        assert _resolve_env_file() == "/custom/path/.env"

    def test_fallback_returns_dotenv_path(self, monkeypatch) -> None:
        from grok_critic.config import _resolve_env_file

        monkeypatch.delenv("POLZA_ENV_FILE", raising=False)
        result = _resolve_env_file()
        assert result.endswith(".env")

    def test_empty_env_var_ignored(self, monkeypatch) -> None:
        from grok_critic.config import _resolve_env_file

        monkeypatch.setenv("POLZA_ENV_FILE", "   ")
        result = _resolve_env_file()
        assert result.endswith(".env")


# END_BLOCK_ENV_FILE_RESOLUTION


# START_BLOCK_ENV_OVERRIDE
class TestEnvOverride:
    def test_api_key_from_env(self) -> None:
        with patch.dict(os.environ, {"POLZA_API_KEY": "env-key-123"}):
            cfg = AppConfig()
            assert cfg.api_key.get_secret_value() == "env-key-123"

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

    def test_allow_file_path_override(self) -> None:
        """SEC-03: file_path включается явным флагом."""
        with patch.dict(os.environ, {"POLZA_ALLOW_FILE_PATH": "true"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.allow_file_path is True

    def test_daily_budget_override(self) -> None:
        with patch.dict(os.environ, {"POLZA_DAILY_BUDGET_USD": "5.5"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.daily_budget_usd == 5.5

    def test_max_concurrent_requests_override(self) -> None:
        with patch.dict(os.environ, {"POLZA_MAX_CONCURRENT_REQUESTS": "4"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.max_concurrent_requests == 4

    def test_retry_deadline_override(self) -> None:
        with patch.dict(os.environ, {"POLZA_RETRY_DEADLINE_SECONDS": "120"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.retry_deadline_seconds == 120.0


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


# START_BLOCK_API_KEY_VALIDATION
class TestApiKeyValidation:
    def test_api_key_required(self) -> None:
        """api_key is mandatory — creating AppConfig without it raises error."""
        with pytest.raises(ValidationError):
            # _make_no_env bypasses .env, so no api_key is available
            _make_no_env()

    def test_api_key_empty_string_rejected(self) -> None:
        """Empty string is not valid for api_key (min_length=1)."""
        with pytest.raises(ValidationError):
            _make_no_env(api_key="")

    def test_api_key_valid(self) -> None:
        """Non-empty api_key is accepted."""
        cfg = _make_no_env(api_key="pza_test-key-123")
        assert cfg.api_key.get_secret_value() == "pza_test-key-123"


# END_BLOCK_API_KEY_VALIDATION


# START_BLOCK_TIMEOUT_VALIDATION
class TestTimeoutValidation:
    def test_timeout_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_no_env(api_key="test-key", timeout_seconds=0)

    def test_timeout_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_no_env(api_key="test-key", timeout_seconds=-1)

    def test_timeout_minimum_valid(self) -> None:
        cfg = _make_no_env(api_key="test-key", timeout_seconds=1)
        assert cfg.timeout_seconds == 1

    def test_timeout_large_value(self) -> None:
        cfg = _make_no_env(api_key="test-key", timeout_seconds=600)
        assert cfg.timeout_seconds == 600


# END_BLOCK_TIMEOUT_VALIDATION


# START_BLOCK_AGENT_COUNT_VALIDATION
class TestAgentCountValidation:
    def test_agent_count_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_no_env(api_key="test-key", agent_count=0)

    def test_agent_count_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_no_env(api_key="test-key", agent_count=-1)

    def test_agent_count_too_large_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_no_env(api_key="test-key", agent_count=65)

    def test_agent_count_minimum_valid(self) -> None:
        cfg = _make_no_env(api_key="test-key", agent_count=1)
        assert cfg.agent_count == 1

    def test_agent_count_maximum_valid(self) -> None:
        cfg = _make_no_env(api_key="test-key", agent_count=64)
        assert cfg.agent_count == 64

    def test_agent_count_16_valid(self) -> None:
        cfg = _make_no_env(api_key="test-key", agent_count=16)
        assert cfg.agent_count == 16


# END_BLOCK_AGENT_COUNT_VALIDATION


# START_BLOCK_RELOAD_CONFIG
class TestReloadConfig:
    def test_reload_returns_appconfig(self) -> None:
        """reload_config() должен возвращать AppConfig с актуальными полями."""
        from grok_critic.config import reload_config
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
