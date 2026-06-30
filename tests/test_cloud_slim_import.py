"""Slim-import gate (in-repo, full-env runnable).

THE invariant of the refactor: the cloud package must import with ONLY
thin deps — no fat ``puffo_agent`` package, no pyside6, no psutil. We
can't easily spin a fresh thin venv inside pytest, so we simulate one:
install a ``builtins.__import__`` guard that raises on any top-level
import of a fat module, purge any cached cloud/core modules so the
import statements actually re-execute under the guard, then assert
``import puffo_agent_cloud.__main__`` (which pulls the whole runtime:
runner, bundle, config, cloud_client, keystore, tools, cloud_http)
succeeds. The CI ``slim-import`` job is the real-env twin of this.
"""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest

# Top-level module names that must NOT be reachable from the cloud
# package. ``puffo_agent`` is the fat package (distinct top-level from
# ``puffo_agent_cloud`` / ``puffo_agent_core``, so blocking it does not
# block the slim ones).
_BLOCKED = {"PySide6", "pyside6", "psutil", "puffo_agent"}

_CLOUD_PREFIXES = ("puffo_agent_cloud", "puffo_agent_core")


def _purge(prefixes: tuple[str, ...]) -> dict:
    saved = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name.split(".")[0] in prefixes
    }
    for name in saved:
        del sys.modules[name]
    return saved


def test_cloud_imports_with_only_thin_deps():
    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name.split(".")[0] in _BLOCKED:
            raise ImportError(f"blocked fat import in cloud package: {name}")
        return real_import(name, *args, **kwargs)

    saved = _purge(_CLOUD_PREFIXES)
    builtins.__import__ = guard
    try:
        # Sanity: the guard is actually wired up (no vacuous pass).
        # Call the (now-guarded) builtin directly — ``import`` statements
        # route through ``builtins.__import__`` even for cached modules,
        # whereas ``importlib.import_module`` would short-circuit on the
        # sys.modules cache and never hit the guard.
        with pytest.raises(ImportError):
            __import__("psutil")
        with pytest.raises(ImportError):
            __import__("puffo_agent")

        mod = importlib.import_module("puffo_agent_cloud.__main__")
        assert hasattr(mod, "main")
        runtime = importlib.import_module("puffo_agent_cloud")
        # The full public surface resolved without any fat import.
        assert hasattr(runtime, "ApiPuffoRunner")
        assert hasattr(runtime, "CloudMetadataClient")
        assert hasattr(runtime, "CloudAgentConfig")
    finally:
        builtins.__import__ = real_import
        # Re-import cleanly under the real importer so other tests see
        # normally-loaded modules.
        _purge(_CLOUD_PREFIXES)
        sys.modules.update(saved)
