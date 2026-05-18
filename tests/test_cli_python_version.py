"""PUF-206: cli.py's runtime guard against Python <3.11.

The check lives at module-level above any submodule import. We test
the helper function directly with a mocked ``sys.version_info`` so
the test doesn't reload the module (which would re-fire every
submodule import + side-effect under our test harness).
"""

from __future__ import annotations

import io
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.cli import _require_python_311


class _VersionInfo(tuple):
    """Minimal stand-in for ``sys.version_info`` that supports both
    tuple comparison and named-attribute access (``major``, ``minor``,
    ``micro``)."""

    def __new__(cls, major: int, minor: int, micro: int):
        obj = super().__new__(cls, (major, minor, micro, "final", 0))
        obj.major = major
        obj.minor = minor
        obj.micro = micro
        return obj


def test_require_python_311_rejects_310(monkeypatch):
    monkeypatch.setattr(sys, "version_info", _VersionInfo(3, 10, 12))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    with pytest.raises(SystemExit) as exc:
        _require_python_311()
    assert exc.value.code == 1
    msg = buf.getvalue()
    assert ">= 3.11" in msg
    assert "3.10.12" in msg
    # At least one of the upgrade-path hints should be cited so the
    # user has a concrete next step rather than just "go figure it
    # out".
    assert any(token in msg for token in ("pyenv", "brew", "python.org"))


def test_require_python_311_rejects_old_3x(monkeypatch):
    monkeypatch.setattr(sys, "version_info", _VersionInfo(3, 9, 18))
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    with pytest.raises(SystemExit):
        _require_python_311()


def test_require_python_311_passes_on_311(monkeypatch):
    monkeypatch.setattr(sys, "version_info", _VersionInfo(3, 11, 0))
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    _require_python_311()


def test_require_python_311_passes_on_312_and_later(monkeypatch):
    monkeypatch.setattr(sys, "version_info", _VersionInfo(3, 14, 4))
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    _require_python_311()
