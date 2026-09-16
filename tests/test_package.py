# FILE: tests/test_package.py
# VERSION: 1.12.0
# START_MODULE_CONTRACT
#   PURPOSE: Tests for package public API consistency (__init__.py)
#   SCOPE: Verify __all__ matches actual exports, star-import works
#   DEPENDS: M-CONFIG, M-API, M-CRITIC, M-SERVER
#   LINKS: M-CONFIG, M-API, M-CRITIC, M-SERVER
# END_MODULE_CONTRACT

from __future__ import annotations

import importlib


# START_BLOCK_ALL_CONSISTENCY
class TestPackageExports:
    """BUG-02: __all__ должен ссылаться только на реально импортированные имена."""

    def test_all_exports_exist(self) -> None:
        m = importlib.import_module("grok_critic")
        missing = [name for name in m.__all__ if not hasattr(m, name)]
        assert not missing, f"__all__ lists names not present in package: {missing}"

    def test_star_import_works(self) -> None:
        ns: dict = {}
        exec("from grok_critic import *", ns)  # noqa: S102 — суть теста
        m = importlib.import_module("grok_critic")
        missing = [name for name in m.__all__ if name not in ns]
        assert not missing, f"star-import missed: {missing}"

    def test_no_config_singleton_shadowing(self) -> None:
        """Синглтон config намеренно НЕ реэкспортируется из пакета:
        имя `grok_critic.config` должно оставаться подмодулем, иначе ломается
        `from grok_critic import config` → submodule и reload-semantics."""
        m = importlib.import_module("grok_critic")
        assert "config" not in m.__all__
        import types

        assert isinstance(m.config, types.ModuleType)


# END_BLOCK_ALL_CONSISTENCY
