"""Tests for trading_commons lazy submodule imports (PEP 562).

Importing one submodule must not pull in the others' third-party deps. The
strongest check runs in a fresh subprocess where ``config`` (and its
pydantic-settings / PyYAML deps) is poisoned: importing
``trading_commons.telegram`` must still succeed.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
    )


def test_importing_telegram_does_not_import_config():
    # In a fresh interpreter, import telegram and assert config never loaded.
    proc = _run("""
        import sys
        import trading_commons.telegram  # noqa: F401
        assert "trading_commons.config" not in sys.modules, (
            "importing telegram must NOT import config"
        )
        # pydantic_settings / yaml are config's deps — must be absent too.
        assert "pydantic_settings" not in sys.modules
        assert "yaml" not in sys.modules
        print("OK")
        """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_telegram_imports_even_if_config_deps_broken():
    # Poison pydantic_settings + yaml so that importing config would fail.
    # telegram must still import because it is loaded lazily / independently.
    proc = _run("""
        import sys, types
        broken = types.ModuleType("pydantic_settings")
        def _boom(*a, **k):
            raise RuntimeError("pydantic_settings is poisoned")
        broken.__getattr__ = _boom
        sys.modules["pydantic_settings"] = broken
        sys.modules["yaml"] = None  # importing yaml will now raise

        import trading_commons.telegram  # must NOT touch config
        assert "trading_commons.config" not in sys.modules
        print("OK")
        """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_redisx_does_not_import_config():
    proc = _run("""
        import sys
        from trading_commons import RedisBase  # noqa: F401
        assert "trading_commons.config" not in sys.modules
        print("OK")
        """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_lazy_top_level_name_access_works():
    # Accessing a lazily-exported name imports its submodule on demand.
    import trading_commons

    assert trading_commons.BaseServiceSettings is not None
    assert trading_commons.RedisBase is not None
    assert trading_commons.TelegramClient is not None
    assert trading_commons.run_daemon is not None
    assert trading_commons.read_secret is not None
    assert trading_commons.redis_url is not None


def test_submodule_attribute_access_works():
    import trading_commons

    assert trading_commons.metrics is not None
    assert trading_commons.config is not None


def test_submodule_attribute_access_fresh_subprocess():
    # Fresh interpreter: accessing a submodule attribute triggers the
    # _SUBMODULES branch of __getattr__ (not yet cached in globals).
    proc = _run("""
        import trading_commons
        m = trading_commons.metrics
        assert m.__name__ == "trading_commons.metrics"
        c = trading_commons.config
        assert c.__name__ == "trading_commons.config"
        print("OK")
        """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_submodule_getattr_branch_in_process():
    # Force the _SUBMODULES branch of __getattr__ in-process by clearing any
    # cached global so attribute access goes through __getattr__ again.
    import trading_commons

    trading_commons.__dict__.pop("daemon", None)
    mod = trading_commons.__getattr__("daemon")
    assert mod.__name__ == "trading_commons.daemon"


def test_unknown_attribute_raises_attributeerror():
    import trading_commons

    try:
        trading_commons.does_not_exist  # noqa: B018
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected AttributeError")


def test_dir_lists_exports():
    import trading_commons

    names = dir(trading_commons)
    assert "BaseServiceSettings" in names
    assert "config" in names
    assert "__version__" in names


def test_version_is_020():
    import trading_commons

    assert trading_commons.__version__ == "0.2.0"
