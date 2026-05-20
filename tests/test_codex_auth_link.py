"""Tests for ``link_host_codex_auth`` — the OAuth fallback path that
shares ``~/.codex/auth.json`` with each agent's ``$CODEX_HOME``.

Mirrors ``test_host_credentials.py``'s shape: symlink-preferred,
copy-fallback for Windows non-dev-mode, idempotent on second call.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.state import link_host_codex_auth


def _symlinks_available(tmp_path: Path) -> bool:
    probe = tmp_path / "_probe_link"
    target = tmp_path / "_probe_target"
    target.write_text("x", encoding="utf-8")
    try:
        os.symlink(target, probe)
        probe.unlink()
        target.unlink()
        return True
    except (OSError, NotImplementedError):
        try:
            target.unlink()
        except OSError:
            pass
        return False


def _write_host_auth(host_home: Path, content: str = '{"token": "v1"}') -> Path:
    p = host_home / ".codex" / "auth.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_no_host_file_returns_marker(tmp_path):
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    mode = link_host_codex_auth(host, agent_codex)
    assert mode == "no-host-file"
    assert not agent_codex.exists()


def test_symlink_created_when_supported(tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    host_auth = _write_host_auth(host, '{"token": "v1"}')

    mode = link_host_codex_auth(host, agent_codex)
    assert mode == "symlink"
    agent_auth = agent_codex / "auth.json"
    assert agent_auth.is_symlink()
    assert agent_auth.read_text() == '{"token": "v1"}'
    # Host rewrite (OAuth refresh) is visible through the symlink with
    # no relink — the property the symlink path exists for.
    host_auth.write_text('{"token": "v2"}', encoding="utf-8")
    assert agent_auth.read_text() == '{"token": "v2"}'


def test_symlink_idempotent_on_second_call(tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host_auth(host)
    link_host_codex_auth(host, agent_codex)
    # Second call must not re-create — returns the (already) marker so
    # the daemon log doesn't spam "shared host codex auth (symlink)"
    # every tick.
    mode = link_host_codex_auth(host, agent_codex)
    assert mode == "symlink (already)"


def test_copy_path_when_symlink_fails(tmp_path, monkeypatch):
    # Force the os.symlink call inside state.py to raise so we exercise
    # the copy fallback regardless of platform.
    import puffo_agent.portal.state as state_mod
    monkeypatch.setattr(
        state_mod.os, "symlink",
        lambda *a, **k: (_ for _ in ()).throw(NotImplementedError("no symlink")),
    )
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host_auth(host, '{"token": "v1"}')

    mode = link_host_codex_auth(host, agent_codex)
    assert mode == "copy"
    agent_auth = agent_codex / "auth.json"
    assert agent_auth.exists()
    assert not agent_auth.is_symlink()
    assert agent_auth.read_text() == '{"token": "v1"}'


def test_copy_fresh_marker_when_already_in_sync(tmp_path, monkeypatch):
    """Copy-mode second call sees the destination is up-to-date and
    returns ``copy (fresh)``."""
    import puffo_agent.portal.state as state_mod
    monkeypatch.setattr(
        state_mod.os, "symlink",
        lambda *a, **k: (_ for _ in ()).throw(NotImplementedError("no symlink")),
    )
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host_auth(host)
    link_host_codex_auth(host, agent_codex)
    mode = link_host_codex_auth(host, agent_codex)
    assert mode == "copy (fresh)"
