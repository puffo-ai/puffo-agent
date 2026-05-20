"""Tests for ``puffo_agent.macos.keychain`` + the macOS-side
``KeychainBackend`` plugged into ``CredentialRefresher``.

We can't actually call the macOS ``security`` binary on a Linux / CI
runner, so the keychain primitives are exercised via
``subprocess.run`` / ``asyncio.create_subprocess_exec`` monkey-patches.
The cache, shim, refresh-oneshot, and the end-to-end refresher fan-out
are real code paths that run on every platform.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.macos import keychain as cm
from puffo_agent.portal import credential_refresh as cr


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

    sandbox_seen = {}

    async def _fake_spawn(*args, env=None, cwd=None, **kwargs):
        sandbox_seen["cwd"] = cwd
        sandbox_seen["env"] = env
        sandbox_creds = Path(cwd) / ".claude" / ".credentials.json"
        sandbox_creds.write_text(_REFRESHED_BLOB, encoding="utf-8")
        return _FakeAsyncProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

    ok, reason = asyncio.run(
        cm.refresh_via_oneshot(cache, tmp_path / "shim"),
    )
    assert ok is True
    assert reason == "token_refreshed"
    assert cache.access_token() == "sk-ant-NEW-access"
    # Must NOT set CLAUDE_CODE_OAUTH_TOKEN — that env var triggers the
    # bug #37512 Keychain-delete path on exit.
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
# KeychainBackend — plugged into CredentialRefresher
# ─────────────────────────────────────────────────────────────────────────────

def _make_keychain_backend(home: Path) -> cr.KeychainBackend:
    cache = cm.CredentialCache.at(home)
    return cr.KeychainBackend(
        home=home, cache=cache, shim_dir=home / "run" / "keychain-shim",
    )


def test_keychain_backend_bootstrap_installs_shim_and_reads_keychain(
    monkeypatch, tmp_path,
):
    """``bootstrap`` should populate the cache from Keychain and
    install the PATH shim. The refresher calls this once on
    daemon-loop entry."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout=_BLOB),
    )
    backend = _make_keychain_backend(tmp_path)
    ok, reason = asyncio.run(backend.bootstrap())
    assert ok is True
    assert reason == "bootstrapped"
    # Cache materialised from the Keychain.
    assert backend.cache.read() == _BLOB
    # Shim installed.
    assert (tmp_path / "run" / "keychain-shim" / "security").exists()
    # Internal last-propagated marker populated so the external-poll
    # loop has a baseline to diff against.
    assert backend._last_propagated_blob == _BLOB


def test_keychain_backend_expires_in_seconds_uses_cache(tmp_path, monkeypatch):
    """Hot path: cache hit returns expiry without spawning a
    subprocess."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    backend = _make_keychain_backend(tmp_path)
    import time as _time
    far_future_blob = json.dumps({
        "claudeAiOauth": {
            "accessToken": "x",
            "refreshToken": "y",
            "expiresAt": int((_time.time() + 3600) * 1000),  # +1h
        },
    })
    backend.cache.write(far_future_blob)
    ttl = backend.expires_in_seconds()
    assert ttl is not None
    assert ttl > 0
    assert 3590 <= ttl <= 3610


def test_keychain_backend_expires_in_seconds_falls_back_to_keychain(
    monkeypatch, tmp_path,
):
    """Cache miss → Keychain read → opportunistically warms cache."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout=_BLOB),
    )
    backend = _make_keychain_backend(tmp_path)
    assert backend.cache.read() is None
    ttl = backend.expires_in_seconds()
    assert ttl is not None
    # Side effect: cache now warm.
    assert backend.cache.read() == _BLOB


def test_keychain_backend_sync_to_agent_writes_per_agent_file(tmp_path, monkeypatch):
    """The macOS sync path is a copy, not a symlink — Keychain ACL is
    keyed on UID + signing identity, not HOME, so symlinking the
    daemon cache into agent dirs gives no benefit (and would diverge
    anyway when claude self-refreshes inside the agent process)."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    backend = _make_keychain_backend(tmp_path)
    backend.cache.write(_BLOB)
    agent_home = tmp_path / "agent-a"
    agent_home.mkdir()
    backend.sync_to_agent(agent_home)
    agent_creds = agent_home / ".claude" / ".credentials.json"
    assert agent_creds.exists()
    assert not agent_creds.is_symlink()
    assert agent_creds.read_text() == _BLOB


def test_keychain_backend_sync_skips_when_cache_empty(tmp_path, monkeypatch):
    """No cache → no per-agent file written. Avoids stamping an
    empty-string file that claude would later read as "no auth"."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    backend = _make_keychain_backend(tmp_path)
    agent_home = tmp_path / "agent-a"
    agent_home.mkdir()
    backend.sync_to_agent(agent_home)
    assert not (agent_home / ".claude" / ".credentials.json").exists()


def test_keychain_backend_refresh_returns_refreshed_on_token_rotation(
    monkeypatch, tmp_path,
):
    """Backend refresh: cache → refresh oneshot → cache write →
    writeback to Keychain (best-effort). Returns REFRESHED when the
    access token actually rotated."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    monkeypatch.setattr(cm.shutil, "which", lambda b: "/usr/local/bin/claude")

    backend = _make_keychain_backend(tmp_path)
    backend.cache.write(_BLOB)

    async def _fake_spawn(*args, env=None, cwd=None, **kwargs):
        sandbox_creds = Path(cwd) / ".claude" / ".credentials.json"
        sandbox_creds.write_text(_REFRESHED_BLOB, encoding="utf-8")
        return _FakeAsyncProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

    writeback_calls: list = []

    def _fake_writeback(blob, timeout=cm.SECURITY_TIMEOUT_SECONDS):
        writeback_calls.append(blob)
        return (True, None)

    monkeypatch.setattr(cm, "writeback_to_keychain", _fake_writeback)

    outcome = asyncio.run(backend.refresh())
    assert outcome == cr.RefreshOutcome.REFRESHED
    assert backend.cache.access_token() == "sk-ant-NEW-access"
    assert writeback_calls == [_REFRESHED_BLOB]
    assert backend._last_propagated_blob == _REFRESHED_BLOB


def test_keychain_backend_refresh_returns_unchanged_when_token_stays(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    monkeypatch.setattr(cm.shutil, "which", lambda b: "/usr/local/bin/claude")

    backend = _make_keychain_backend(tmp_path)
    backend.cache.write(_BLOB)

    async def _fake_spawn(*args, env=None, cwd=None, **kwargs):
        sandbox_creds = Path(cwd) / ".claude" / ".credentials.json"
        sandbox_creds.write_text(_BLOB, encoding="utf-8")  # same blob back
        return _FakeAsyncProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

    outcome = asyncio.run(backend.refresh())
    assert outcome == cr.RefreshOutcome.UNCHANGED


def test_keychain_backend_refresh_returns_failed_on_claude_exit_failure(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    monkeypatch.setattr(cm.shutil, "which", lambda b: "/usr/local/bin/claude")

    backend = _make_keychain_backend(tmp_path)
    backend.cache.write(_BLOB)

    async def _fake_spawn(*args, env=None, cwd=None, **kwargs):
        return _FakeAsyncProc(1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

    outcome = asyncio.run(backend.refresh())
    assert outcome == cr.RefreshOutcome.FAILED


def test_keychain_backend_poll_external_rotation_detects_change(
    monkeypatch, tmp_path,
):
    """Operator runs ``claude /login`` (or an agent's own claude
    self-refreshes on 401) → Keychain has a new blob → poll returns
    True so the refresher fans out to every running agent."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    backend = _make_keychain_backend(tmp_path)
    backend._last_propagated_blob = _BLOB
    backend.cache.write(_BLOB)
    monkeypatch.setattr(
        cm, "read_keychain_blob",
        lambda timeout=cm.SECURITY_TIMEOUT_SECONDS: cm.KeychainReadResult(
            True, _REFRESHED_BLOB, None, None,
        ),
    )
    rotated = asyncio.run(backend.poll_external_rotation())
    assert rotated is True
    assert backend.cache.read() == _REFRESHED_BLOB
    assert backend._last_propagated_blob == _REFRESHED_BLOB


def test_keychain_backend_poll_external_rotation_no_change_returns_false(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    backend = _make_keychain_backend(tmp_path)
    backend._last_propagated_blob = _BLOB
    backend.cache.write(_BLOB)
    monkeypatch.setattr(
        cm, "read_keychain_blob",
        lambda timeout=cm.SECURITY_TIMEOUT_SECONDS: cm.KeychainReadResult(
            True, _BLOB, None, None,
        ),
    )
    rotated = asyncio.run(backend.poll_external_rotation())
    assert rotated is False


def test_keychain_backend_poll_external_rotation_swallows_read_failure(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    backend = _make_keychain_backend(tmp_path)
    monkeypatch.setattr(
        cm, "read_keychain_blob",
        lambda timeout=cm.SECURITY_TIMEOUT_SECONDS: cm.KeychainReadResult(
            False, None, "security_timeout", None,
        ),
    )
    rotated = asyncio.run(backend.poll_external_rotation())
    assert rotated is False


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: CredentialRefresher + KeychainBackend fan-out
# ─────────────────────────────────────────────────────────────────────────────

def test_refresher_with_keychain_backend_fans_sync_to_registered_agents(
    monkeypatch, tmp_path,
):
    """After a _tick on macOS, the refresher's fan-out calls
    KeychainBackend.sync_to_agent for every registered agent — same
    contract as the FileBackend's link_host_credentials path, just
    plumbed through the backend abstraction."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    backend = _make_keychain_backend(tmp_path)
    backend.cache.write(_BLOB)
    refresher = cr.CredentialRefresher(backend=backend)

    agent_a = tmp_path / "agent-a"
    agent_b = tmp_path / "agent-b"
    refresher.register_agent(agent_a)
    refresher.register_agent(agent_b)

    asyncio.run(refresher._tick())

    assert (agent_a / ".claude" / ".credentials.json").read_text() == _BLOB
    assert (agent_b / ".claude" / ".credentials.json").read_text() == _BLOB


def test_refresher_with_keychain_backend_refreshes_when_close_to_expiry(
    monkeypatch, tmp_path,
):
    """Cache blob expiring soon → refresher triggers
    backend.refresh()."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    monkeypatch.setattr(cm.shutil, "which", lambda b: "/usr/local/bin/claude")
    backend = _make_keychain_backend(tmp_path)
    # Seed with a blob expiring in 1 minute (well inside the 10-min margin).
    import time as _time
    soon_expiry = int((_time.time() + 60) * 1000)
    near_expiry_blob = json.dumps({
        "claudeAiOauth": {
            "accessToken": "soon-expiry",
            "refreshToken": "rt",
            "expiresAt": soon_expiry,
        },
    })
    backend.cache.write(near_expiry_blob)

    spawned: list = []

    async def _fake_spawn(*args, env=None, cwd=None, **kwargs):
        spawned.append(env)
        # Pretend claude wrote a rotated blob.
        sandbox_creds = Path(cwd) / ".claude" / ".credentials.json"
        sandbox_creds.write_text(_REFRESHED_BLOB, encoding="utf-8")
        return _FakeAsyncProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(cm, "writeback_to_keychain", lambda *a, **k: (True, None))

    refresher = cr.CredentialRefresher(backend=backend)
    asyncio.run(refresher._tick())
    assert spawned, "backend.refresh should have run claude --print"
    # Refresh ran inside a sandbox HOME (not the operator's), so HOME
    # must be a tempdir, not host_home.
    env = spawned[0]
    assert "HOME" in env
    assert env["HOME"] != str(Path.home())


# ─────────────────────────────────────────────────────────────────────────────
# FD-leak regression — timeout path drains pipes
# ─────────────────────────────────────────────────────────────────────────────

def test_timeout_path_kills_proc_and_drains_pipes(monkeypatch, tmp_path):
    """The previous timeout path returned without awaiting proc.wait()
    + pipe close, leaking 3 FDs (stdin/stdout/stderr) per timed-out
    refresh. Under multi-agent load this surfaced as ``[Errno 24]
    Too many open files``. Verify the helper kills + drains."""
    monkeypatch.setattr(cm.shutil, "which", lambda b: "/usr/local/bin/claude")

    kill_called = {"value": False}
    drain_calls = {"value": 0}

    class _HangingProc:
        returncode = None  # alive

        async def communicate(self):
            if drain_calls["value"] == 0:
                drain_calls["value"] += 1
                await asyncio.sleep(60)
                return (b"", b"")
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

    async def _drive():
        return await cm._run_claude_oneshot(
            env={}, cwd=str(tmp_path), timeout=0.1,
        )

    rc, err = asyncio.run(_drive())
    assert rc is None
    assert err == "refresh_oneshot_timeout"
    assert kill_called["value"], "proc.kill() must run on timeout"
    assert drain_calls["value"] == 2, (
        "expected one wait_for'd communicate() + one drain communicate() "
        "after kill — got %d" % drain_calls["value"]
    )
