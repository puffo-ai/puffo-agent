"""Tests for ``puffo_agent.macos.credential_manager``.

We can't actually call the macOS ``security`` binary on a Linux CI
runner, so the keychain primitives are exercised via ``subprocess.run``
monkey-patches. The cache, shim, and refresh-loop state machine are
real code paths that run on every platform.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.macos import credential_manager as cm


# ─────────────────────────────────────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────────────────────────────────────

_BLOB = json.dumps({
    "claudeAiOauth": {
        "accessToken": "sk-ant-original-access",
        "refreshToken": "rt-original",
        "expiresAt": 9_999_999_000,
    },
})

_REFRESHED_BLOB = json.dumps({
    "claudeAiOauth": {
        "accessToken": "sk-ant-NEW-access",
        "refreshToken": "rt-new",
        "expiresAt": 9_999_999_500,
    },
})


def _force_macos(monkeypatch):
    monkeypatch.setattr(cm, "is_macos", lambda: True)


def _disable_macos(monkeypatch):
    monkeypatch.setattr(cm, "is_macos", lambda: False)


# ─────────────────────────────────────────────────────────────────────────────
# CredentialCache
# ─────────────────────────────────────────────────────────────────────────────

def test_cache_write_then_read_roundtrip(tmp_path):
    cache = cm.CredentialCache.at(tmp_path)
    assert cache.read() is None
    cache.write(_BLOB)
    assert cache.read() == _BLOB
    assert cache.access_token() == "sk-ant-original-access"


def test_cache_write_is_atomic(tmp_path):
    cache = cm.CredentialCache.at(tmp_path)
    cache.write(_BLOB)
    # Mid-write power cut would leave a tmp file but never a half-
    # written final file. We can't simulate that here but we can check
    # the final file's permissions and content are correct.
    assert cache.path.exists()
    cache.write(_REFRESHED_BLOB)
    # No leftover tmp files.
    siblings = list(cache.path.parent.glob(".claude-credentials.json.tmp.*"))
    assert siblings == []
    assert cache.read() == _REFRESHED_BLOB


def test_cache_access_token_handles_malformed_blob(tmp_path):
    cache = cm.CredentialCache.at(tmp_path)
    cache.path.parent.mkdir(parents=True, exist_ok=True)
    cache.path.write_text("not json {", encoding="utf-8")
    assert cache.access_token() is None
    assert cache.expires_at_seconds() is None


def test_cache_expires_at_seconds(tmp_path):
    cache = cm.CredentialCache.at(tmp_path)
    cache.write(_BLOB)
    assert cache.expires_at_seconds() == 9_999_999.000


# ─────────────────────────────────────────────────────────────────────────────
# PATH shim
# ─────────────────────────────────────────────────────────────────────────────

def test_install_path_shim_writes_executable(tmp_path):
    d = cm.install_path_shim(tmp_path)
    binary = d / "security"
    assert binary.exists()
    body = binary.read_text(encoding="utf-8")
    assert body.startswith("#!/bin/bash")
    # Block claude code issue #37512 — shim must reference the exact
    # service name + subcommand it intercepts.
    assert "delete-generic-password" in body
    assert "Claude Code-credentials" in body
    assert "/usr/bin/security" in body


def test_install_path_shim_overwrites_existing(tmp_path):
    d = cm.install_path_shim(tmp_path)
    (d / "security").write_text("# manually edited", encoding="utf-8")
    cm.install_path_shim(tmp_path)
    assert "#!/bin/bash" in (d / "security").read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Keychain primitives — mocked subprocess
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_read_keychain_blob_success(monkeypatch):
    _force_macos(monkeypatch)

    def _fake_run(cmd, **kwargs):
        assert cmd[0] == "security"
        assert "find-generic-password" in cmd
        assert "Claude Code-credentials" in cmd
        return _FakeCompletedProcess(0, stdout=_BLOB)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = cm.read_keychain_blob()
    assert result.ok is True
    assert result.blob == _BLOB


def test_read_keychain_blob_invalid_json(monkeypatch):
    _force_macos(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout="not a json"),
    )
    result = cm.read_keychain_blob()
    assert result.ok is False
    assert "invalid_json" in result.error


def test_read_keychain_blob_nonzero_exit(monkeypatch):
    _force_macos(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(44, stderr="entry not found"),
    )
    result = cm.read_keychain_blob()
    assert result.ok is False
    assert "exit_code=44" in result.error
    assert result.stderr == "entry not found"


def test_read_keychain_blob_skipped_off_macos(monkeypatch):
    _disable_macos(monkeypatch)
    result = cm.read_keychain_blob()
    assert result.ok is False
    assert result.error == "not_macos"


def test_writeback_to_keychain_passes_blob(monkeypatch):
    _force_macos(monkeypatch)
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    ok, reason = cm.writeback_to_keychain(_BLOB)
    assert ok is True
    assert reason is None
    assert "add-generic-password" in captured["cmd"]
    assert "-U" in captured["cmd"]
    # Blob argument is what we passed.
    assert _BLOB in captured["cmd"]


def test_writeback_to_keychain_reports_failure(monkeypatch):
    _force_macos(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(1, stderr="permission denied"),
    )
    ok, reason = cm.writeback_to_keychain(_BLOB)
    assert ok is False
    assert "permission denied" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────────────────────

def test_bootstrap_writes_cache(monkeypatch, tmp_path):
    _force_macos(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout=_BLOB),
    )
    cache = cm.CredentialCache.at(tmp_path)
    ok, reason = cm.bootstrap_from_keychain(cache)
    assert ok is True
    assert reason == "bootstrapped"
    assert cache.read() == _BLOB


def test_bootstrap_skips_when_cache_warm(monkeypatch, tmp_path):
    _force_macos(monkeypatch)
    cache = cm.CredentialCache.at(tmp_path)
    cache.write(_BLOB)

    def _fake_run(*a, **k):
        raise AssertionError("should not have run security")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    ok, reason = cm.bootstrap_from_keychain(cache)
    assert ok is True
    assert reason == "cache_already_warm"


def test_bootstrap_propagates_read_error(monkeypatch, tmp_path):
    _force_macos(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(44, stderr="missing"),
    )
    cache = cm.CredentialCache.at(tmp_path)
    ok, reason = cm.bootstrap_from_keychain(cache)
    assert ok is False
    assert "exit_code=44" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Refresh oneshot — subprocess mocked
# ─────────────────────────────────────────────────────────────────────────────

class _FakeAsyncProc:
    def __init__(self, returncode: int):
        self.returncode = returncode

    async def communicate(self):
        return (b"", b"")

    def kill(self):
        pass


def test_refresh_via_oneshot_rotates_token(monkeypatch, tmp_path):
    _force_macos(monkeypatch)
    monkeypatch.setattr(cm.shutil, "which", lambda b: "/usr/local/bin/claude")
    cache = cm.CredentialCache.at(tmp_path)
    cache.write(_BLOB)

    # ``asyncio.create_subprocess_exec`` is awaited to get the proc.
    # Our fake records the sandbox cwd so we can write the refreshed
    # blob into ``.claude/.credentials.json`` like real claude would.
    sandbox_seen = {}

    async def _fake_spawn(*args, env=None, cwd=None, **kwargs):
        sandbox_seen["cwd"] = cwd
        sandbox_seen["env"] = env
        sandbox_creds = Path(cwd) / ".claude" / ".credentials.json"
        sandbox_creds.write_text(_REFRESHED_BLOB, encoding="utf-8")
        return _FakeAsyncProc(0)

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _fake_spawn,
    )

    ok, reason = asyncio.run(
        cm.refresh_via_oneshot(cache, tmp_path / "shim"),
    )
    assert ok is True
    assert reason == "token_refreshed"
    assert cache.access_token() == "sk-ant-NEW-access"
    # Refresh oneshot must NOT set CLAUDE_CODE_OAUTH_TOKEN — see
    # docstring rationale.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in (sandbox_seen.get("env") or {})
    # PATH must include the shim dir.
    path_val = (sandbox_seen.get("env") or {}).get("PATH", "")
    assert str(tmp_path / "shim") in path_val


def test_refresh_via_oneshot_token_unchanged(monkeypatch, tmp_path):
    _force_macos(monkeypatch)
    monkeypatch.setattr(cm.shutil, "which", lambda b: "/usr/local/bin/claude")
    cache = cm.CredentialCache.at(tmp_path)
    cache.write(_BLOB)

    async def _fake_spawn(*args, env=None, cwd=None, **kwargs):
        # claude wrote the same blob back (token still valid).
        sandbox_creds = Path(cwd) / ".claude" / ".credentials.json"
        sandbox_creds.write_text(_BLOB, encoding="utf-8")
        return _FakeAsyncProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    ok, reason = asyncio.run(
        cm.refresh_via_oneshot(cache, tmp_path / "shim"),
    )
    assert ok is True
    assert reason == "token_unchanged"


def test_refresh_via_oneshot_handles_claude_exit_failure(monkeypatch, tmp_path):
    _force_macos(monkeypatch)
    monkeypatch.setattr(cm.shutil, "which", lambda b: "/usr/local/bin/claude")
    cache = cm.CredentialCache.at(tmp_path)
    cache.write(_BLOB)

    async def _fake_spawn(*args, env=None, cwd=None, **kwargs):
        return _FakeAsyncProc(1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    ok, reason = asyncio.run(
        cm.refresh_via_oneshot(cache, tmp_path / "shim"),
    )
    assert ok is False
    assert reason == "claude_exit_code=1"


def test_refresh_via_oneshot_skipped_off_macos(monkeypatch, tmp_path):
    _disable_macos(monkeypatch)
    cache = cm.CredentialCache.at(tmp_path)
    cache.write(_BLOB)
    ok, reason = asyncio.run(
        cm.refresh_via_oneshot(cache, tmp_path / "shim"),
    )
    assert ok is False
    assert reason == "not_macos"


def test_refresh_via_oneshot_skipped_when_claude_missing(monkeypatch, tmp_path):
    _force_macos(monkeypatch)
    monkeypatch.setattr(cm.shutil, "which", lambda b: None)
    cache = cm.CredentialCache.at(tmp_path)
    cache.write(_BLOB)
    ok, reason = asyncio.run(
        cm.refresh_via_oneshot(cache, tmp_path / "shim"),
    )
    assert ok is False
    assert reason == "claude_binary_missing"


# ─────────────────────────────────────────────────────────────────────────────
# CredentialManager (the daemon companion)
# ─────────────────────────────────────────────────────────────────────────────

def test_credential_manager_bootstrap_off_macos(monkeypatch, tmp_path):
    _disable_macos(monkeypatch)
    m = cm.CredentialManager(tmp_path)
    ok, reason = asyncio.run(m.bootstrap())
    assert ok is False
    assert reason == "not_macos"


def test_credential_manager_start_is_noop_off_macos(monkeypatch, tmp_path):
    _disable_macos(monkeypatch)
    m = cm.CredentialManager(tmp_path)
    m.start()
    assert m._task is None
    asyncio.run(m.stop())  # should not raise
