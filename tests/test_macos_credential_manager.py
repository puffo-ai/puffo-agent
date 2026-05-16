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
    """On non-macOS the manager still starts (host file is the
    canonical credential store there), so bootstrap returns OK with a
    descriptive reason rather than refusing."""
    _disable_macos(monkeypatch)
    m = cm.CredentialManager(tmp_path)
    ok, reason = asyncio.run(m.bootstrap())
    assert ok is True
    assert reason == "host_file_authoritative"


def test_credential_manager_start_runs_loop_on_all_platforms(
    monkeypatch, tmp_path,
):
    """The previous gate that no-op'd ``start()`` off macOS caused
    the real-world bug — Linux/Windows multi-agent runs got NO
    daemon-level refresh, and each agent fell back to its own
    refresh_ping path that raced with everyone else's. ``start()``
    now creates the loop task on every platform. We mock
    ``refresh_via_host_oneshot`` so the loop body doesn't try to
    spawn a real claude binary inside the test process."""
    _disable_macos(monkeypatch)

    async def _fake_host_refresh(host_home, *, timeout=90.0):
        return (True, "fake_ok")

    monkeypatch.setattr(cm, "refresh_via_host_oneshot", _fake_host_refresh)

    async def _drive():
        m = cm.CredentialManager(tmp_path, refresh_interval_seconds=3600.0)
        m.start()
        assert m._task is not None
        # Give the loop a moment to do its initial jitter sleep + tick.
        await asyncio.sleep(0.05)
        await m.stop()
        assert m._task is None

    asyncio.run(_drive())


# ─────────────────────────────────────────────────────────────────────────────
# Backoff state machine
# ─────────────────────────────────────────────────────────────────────────────

def test_next_interval_no_failures_returns_normal(tmp_path):
    m = cm.CredentialManager(tmp_path, refresh_interval_seconds=6 * 3600)
    assert m._next_interval_seconds() == 6 * 3600


def test_next_interval_one_failure_short(tmp_path):
    """First failure retries in 10 min — way under the normal 6h
    cadence so a transient blip doesn't strand agents with stale
    creds for hours."""
    m = cm.CredentialManager(tmp_path, refresh_interval_seconds=6 * 3600)
    m.consecutive_failures = 1
    assert m._next_interval_seconds() == 600.0


def test_next_interval_exponential(tmp_path):
    m = cm.CredentialManager(tmp_path, refresh_interval_seconds=6 * 3600)
    m.consecutive_failures = 2
    assert m._next_interval_seconds() == 1200.0
    m.consecutive_failures = 3
    assert m._next_interval_seconds() == 2400.0
    m.consecutive_failures = 4
    assert m._next_interval_seconds() == 4800.0


def test_next_interval_capped_at_normal_interval(tmp_path):
    """Permanently-broken refresh shouldn't keep retrying more
    aggressively forever — past the normal cadence the backoff
    plateaus at 6h."""
    m = cm.CredentialManager(tmp_path, refresh_interval_seconds=6 * 3600)
    m.consecutive_failures = 100
    assert m._next_interval_seconds() == 6 * 3600


# ─────────────────────────────────────────────────────────────────────────────
# refresh_via_host_oneshot (Linux/Windows path)
# ─────────────────────────────────────────────────────────────────────────────

def test_refresh_via_host_oneshot_success(monkeypatch, tmp_path):
    """Linux/Windows daemon path — spawn ``claude --print`` against
    the operator's real HOME, claude writes back to
    ``~/.claude/.credentials.json`` natively."""
    monkeypatch.setattr(cm.shutil, "which", lambda b: "/usr/local/bin/claude")
    captured = {}

    async def _fake_spawn(*args, env=None, cwd=None, **kwargs):
        captured["env"] = env
        return _FakeAsyncProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    ok, reason = asyncio.run(cm.refresh_via_host_oneshot(tmp_path))
    assert ok is True
    # Verify we passed the host home through as HOME — claude needs
    # this so its native refresh writes to the right file.
    assert captured["env"]["HOME"] == str(tmp_path)


def test_refresh_via_host_oneshot_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(cm.shutil, "which", lambda b: None)
    ok, reason = asyncio.run(cm.refresh_via_host_oneshot(tmp_path))
    assert ok is False
    assert reason == "claude_binary_missing"


def test_refresh_via_host_oneshot_claude_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(cm.shutil, "which", lambda b: "/usr/local/bin/claude")

    async def _fake_spawn(*args, **kwargs):
        return _FakeAsyncProc(1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    ok, reason = asyncio.run(cm.refresh_via_host_oneshot(tmp_path))
    assert ok is False
    assert "claude_exit_code=1" in reason


# ─────────────────────────────────────────────────────────────────────────────
# FD-leak regression — timeout path drains pipes
# ─────────────────────────────────────────────────────────────────────────────

def test_timeout_path_kills_proc_and_drains_pipes(monkeypatch, tmp_path):
    """The previous timeout path returned without awaiting proc.wait()
    + pipe close, leaking 3 FDs (stdin/stdout/stderr) per timed-out
    refresh. Under multi-agent load this surfaced as ``[Errno 24]
    Too many open files``. Verify the new helper kills + drains."""
    monkeypatch.setattr(cm.shutil, "which", lambda b: "/usr/local/bin/claude")

    kill_called = {"value": False}
    drain_calls = {"value": 0}

    class _HangingProc:
        returncode = None  # alive

        async def communicate(self):
            # First call (the wait_for'd one) hangs.
            if drain_calls["value"] == 0:
                drain_calls["value"] += 1
                await asyncio.sleep(60)  # >> than test timeout
                return (b"", b"")
            # Second call (the post-kill drain) returns immediately.
            drain_calls["value"] += 1
            self.returncode = -9
            return (b"", b"")

        def kill(self):
            kill_called["value"] = True

        async def wait(self):
            self.returncode = -9

    async def _fake_spawn(*args, **kwargs):
        return _HangingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

    # 100ms timeout — well under any plausible real wait.
    async def _drive():
        return await cm._run_claude_oneshot(
            env={}, cwd=str(tmp_path), timeout=0.1,
        )

    rc, err = asyncio.run(_drive())
    assert rc is None
    assert err == "refresh_oneshot_timeout"
    # The two assertions that prove the FD leak is fixed:
    assert kill_called["value"], "proc.kill() must run on timeout"
    assert drain_calls["value"] == 2, (
        "expected one wait_for'd communicate() + one drain communicate() "
        "after kill — got %d" % drain_calls["value"]
    )
