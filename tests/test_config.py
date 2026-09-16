# FILE: tests/test_config.py
# VERSION: 1.12.0
# START_MODULE_CONTRACT
#   PURPOSE: Tests for M-CONFIG configuration loading and validation
#   SCOPE: Test env var reading, defaults, log_level validation, daily_budget_rub,
#          deprecated price/budget keys warning, log datefmt
#   DEPENDS: M-CONFIG
#   LINKS: M-CONFIG
# END_MODULE_CONTRACT

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

import grok_critic.config as config_mod
from grok_critic.config import AppConfig, _setup_logging


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

    def test_removed_usd_price_fields(self) -> None:
        """PRICING-RUB: цены в $ и бюджет в $ удалены из модели конфига."""
        cfg = _make_no_env(api_key="test-key")
        for removed in ("price_input_per_1m", "price_output_per_1m", "daily_budget_usd"):
            assert removed not in AppConfig.model_fields
            assert not hasattr(cfg, removed)

    def test_default_allow_file_path_false(self) -> None:
        """SEC-03: чтение файлов через file_path выключено по умолчанию."""
        cfg = _make_no_env(api_key="test-key")
        assert cfg.allow_file_path is False

    def test_default_daily_budget_rub(self) -> None:
        cfg = _make_no_env(api_key="test-key")
        assert cfg.daily_budget_rub == 0.0

    def test_negative_daily_budget_rub_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_no_env(api_key="test-key", daily_budget_rub=-1)

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

    def test_deprecated_usd_keys_do_not_break_startup(self) -> None:
        """PRICING-RUB: устаревшие ключи игнорируются (extra="ignore"), запуск не падает."""
        with patch.dict(
            os.environ,
            {
                "POLZA_PRICE_INPUT_PER_1M": "2.5",
                "POLZA_PRICE_OUTPUT_PER_1M": "6.6",
                "POLZA_DAILY_BUDGET_USD": "5.5",
            },
        ):
            cfg = AppConfig(api_key="test-key")
        assert not hasattr(cfg, "price_input_per_1m")
        assert not hasattr(cfg, "daily_budget_usd")

    def test_allow_file_path_override(self) -> None:
        """SEC-03: file_path включается явным флагом."""
        with patch.dict(os.environ, {"POLZA_ALLOW_FILE_PATH": "true"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.allow_file_path is True

    def test_daily_budget_rub_override(self) -> None:
        with patch.dict(os.environ, {"POLZA_DAILY_BUDGET_RUB": "1500.5"}):
            cfg = AppConfig(api_key="test-key")
            assert cfg.daily_budget_rub == 1500.5

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
        assert hasattr(result, "daily_budget_rub")
        assert not hasattr(result, "price_input_per_1m")
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


# START_BLOCK_SETUP_LOGGING
class TestSetupLogging:
    """A10 / TEST-09: покрытие _setup_logging."""

    @pytest.fixture(autouse=True)
    def _restore_logger_state(self):
        logger = logging.getLogger("grok-critic")
        saved_handlers = list(logger.handlers)
        saved_level = logger.level
        logger.handlers.clear()
        yield
        for h in logger.handlers:
            if isinstance(h, logging.FileHandler):
                h.close()
        logger.handlers.clear()
        logger.handlers.extend(saved_handlers)
        logger.setLevel(saved_level)

    def test_file_handler_writes_to_log_file(self, tmp_path) -> None:
        log_file = tmp_path / "x.log"
        cfg = _make_no_env(api_key="test-key", log_level="INFO", log_file=str(log_file))
        _setup_logging(cfg)

        logger = logging.getLogger("grok-critic")
        assert len(logger.handlers) == 1
        handler = logger.handlers[0]
        assert isinstance(handler, logging.FileHandler)
        assert logger.level == logging.INFO

        logger.info("hello from test")
        handler.flush()
        handler.close()

        content = log_file.read_text(encoding="utf-8")
        assert "hello from test" in content

    def test_empty_log_file_uses_stream_handler_to_stderr(self) -> None:
        import sys

        cfg = _make_no_env(api_key="test-key", log_file="")
        _setup_logging(cfg)

        logger = logging.getLogger("grok-critic")
        assert len(logger.handlers) == 1
        handler = logger.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert not isinstance(handler, logging.FileHandler)
        assert handler.stream is sys.stderr

    def test_repeated_call_does_not_duplicate_handlers(self, tmp_path) -> None:
        log_file = tmp_path / "y.log"
        cfg = _make_no_env(api_key="test-key", log_file=str(log_file))

        _setup_logging(cfg)
        _setup_logging(cfg)

        logger = logging.getLogger("grok-critic")
        assert len(logger.handlers) == 1
        logger.handlers[0].close()

    def test_repeated_call_closes_previous_file_handler(self, tmp_path) -> None:
        """reload_config с log_file не должен оставлять незакрытый FileHandler."""
        cfg = _make_no_env(api_key="test-key", log_file=str(tmp_path / "w.log"))
        _setup_logging(cfg)
        logger = logging.getLogger("grok-critic")
        first = logger.handlers[0]
        assert isinstance(first, logging.FileHandler)

        _setup_logging(cfg)

        assert first.stream is None  # FileHandler.close() сбрасывает stream
        assert logger.handlers[0] is not first
        logger.handlers[0].close()

    def test_log_level_normalized_by_validator(self, tmp_path) -> None:
        log_file = tmp_path / "z.log"
        cfg = _make_no_env(api_key="test-key", log_level="info", log_file=str(log_file))
        assert cfg.log_level == "INFO"

        _setup_logging(cfg)
        logger = logging.getLogger("grok-critic")
        assert logger.level == logging.INFO
        logger.handlers[0].close()


    def test_log_datefmt_is_dd_mm_yyyy(self) -> None:
        """Даты в логах — DD.MM.YYYY."""
        cfg = _make_no_env(api_key="test-key", log_file="")
        _setup_logging(cfg)
        handler = logging.getLogger("grok-critic").handlers[0]
        assert handler.formatter is not None
        assert handler.formatter.datefmt == "%d.%m.%Y %H:%M:%S"


# END_BLOCK_SETUP_LOGGING


# START_BLOCK_DEPRECATED_KEYS
_DEPRECATED_VALUES = {
    "POLZA_PRICE_INPUT_PER_1M": "2.6123",
    "POLZA_PRICE_OUTPUT_PER_1M": "6.6456",
    "POLZA_DAILY_BUDGET_USD": "9.8765",
}


class TestDeprecatedKeysWarning:
    """PRICING-RUB: один warning с ИМЕНАМИ устаревших ключей, значения не логируются."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, tmp_path, monkeypatch):
        for key in _DEPRECATED_VALUES:
            monkeypatch.delenv(key, raising=False)
        # .env пользователя не читаем: указываем несуществующий файл
        monkeypatch.setenv("POLZA_ENV_FILE", str(tmp_path / "absent.env"))
        yield

    def test_no_warning_without_deprecated_keys(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="grok-critic"):
            assert config_mod._warn_deprecated_keys() == []
        assert "DEPRECATED" not in caplog.text

    def test_warning_lists_names_from_environ(self, monkeypatch, caplog) -> None:
        for key, value in _DEPRECATED_VALUES.items():
            monkeypatch.setenv(key, value)
        with caplog.at_level(logging.WARNING, logger="grok-critic"):
            keys = config_mod._warn_deprecated_keys()
        assert keys == list(_DEPRECATED_VALUES)
        records = [r for r in caplog.records if "[Config][load_config][DEPRECATED]" in r.getMessage()]
        assert len(records) == 1
        message = records[0].getMessage()
        for key, value in _DEPRECATED_VALUES.items():
            assert key in message
            assert value not in message
        assert "POLZA_DAILY_BUDGET_RUB" in message

    def test_warning_from_dotenv_file(self, tmp_path, monkeypatch, caplog) -> None:
        env_file = tmp_path / "test.env"
        env_file.write_text("POLZA_PRICE_OUTPUT_PER_1M=6.6456\nPOLZA_MODEL=x\n", encoding="utf-8")
        monkeypatch.setenv("POLZA_ENV_FILE", str(env_file))
        with caplog.at_level(logging.WARNING, logger="grok-critic"):
            keys = config_mod._warn_deprecated_keys()
        assert keys == ["POLZA_PRICE_OUTPUT_PER_1M"]
        assert "POLZA_PRICE_OUTPUT_PER_1M" in caplog.text
        assert "6.6456" not in caplog.text

    def test_env_file_with_tilde_is_expanded(self, tmp_path, monkeypatch) -> None:
        """pydantic-settings раскрывает ~ в env_file — проверка устаревших ключей должна тоже."""
        (tmp_path / "grok.env").write_text("POLZA_DAILY_BUDGET_USD=1\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("POLZA_ENV_FILE", "~/grok.env")
        assert config_mod._find_deprecated_keys() == ["POLZA_DAILY_BUDGET_USD"]

    def test_non_ascii_lookalike_key_not_reported(self, tmp_path, monkeypatch) -> None:
        env_file = tmp_path / "lookalike.env"
        env_file.write_text("POLZA_PRICE_ıNPUT_PER_1M=1\n", encoding="utf-8")  # ı.upper() == "I"
        monkeypatch.setenv("POLZA_ENV_FILE", str(env_file))
        assert config_mod._find_deprecated_keys() == []

    def test_load_config_emits_warning(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv("POLZA_DAILY_BUDGET_USD", "9.8765")
        logger = logging.getLogger("grok-critic")
        saved_handlers, saved_level = list(logger.handlers), logger.level
        config_mod.load_config.cache_clear()
        try:
            with caplog.at_level(logging.WARNING, logger="grok-critic"):
                config_mod.load_config()
        finally:
            config_mod.load_config.cache_clear()
            for handler in list(logger.handlers):
                if handler not in saved_handlers:
                    handler.close()
            logger.handlers.clear()
            logger.handlers.extend(saved_handlers)
            logger.setLevel(saved_level)
        records = [r for r in caplog.records if "[Config][load_config][DEPRECATED]" in r.getMessage()]
        assert len(records) == 1
        assert "POLZA_DAILY_BUDGET_USD" in records[0].getMessage()
        assert "9.8765" not in caplog.text


# END_BLOCK_DEPRECATED_KEYS
