"""Tests for ``puffo_agent.macos.keychain`` + the macOS-side
``KeychainBackend`` plugged into ``CredentialRefresher``.

We can't actually call the macOS ``security`` binary on a Linux / CI
runner, so the keychain primitives are exercised via
``subprocess.run`` / ``asyncio.create_subprocess_exec`` monkey-patches.
The cache, refresh path, and end-to-end refresher fan-out are real
code paths that run on every platform.
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
    siblings = list(cache.path.parent.glob(".claude-credentials.json.tmp.*"))
    assert siblings == []
    assert cache.read() == _REFRESHED_BLOB


def test_cache_access_token_handles_malformed_blob(tmp_path):
    cache = cm.CredentialCache.at(tmp_path)
    cache.path.parent.mkdir(parents=True, exist_ok=True)
    cache.path.write_text("not json", encoding="utf-8")
    assert cache.access_token() is None


def test_cache_expires_at_seconds(tmp_path):
    cache = cm.CredentialCache.at(tmp_path)
    cache.write(_BLOB)
    expires = cache.expires_at_seconds()
    assert expires is not None
    assert expires == 9_999_999_000 / 1000.0


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
    calls: list[str] = []

    def _fake_run(cmd, **kwargs):
        assert cmd[0] == "security"
        assert "find-generic-password" in cmd
        calls.append(cmd[cmd.index("-s") + 1])
        return _FakeCompletedProcess(0, stdout=_BLOB)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = cm.read_keychain_blob()
    assert result.ok is True
    assert result.blob == _BLOB
    assert result.service == "Claude Code-credentials"
    assert calls == ["Claude Code-credentials", "Claude Code"]


def test_read_keychain_blob_falls_back_to_bare_claude_code_service(monkeypatch):
    _force_macos(monkeypatch)

    def _fake_run(cmd, **kwargs):
        service = cmd[cmd.index("-s") + 1]
        if service == "Claude Code-credentials":
            return _FakeCompletedProcess(44, stderr="entry not found")
        return _FakeCompletedProcess(0, stdout=_BLOB)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = cm.read_keychain_blob()
    assert result.ok is True
    assert result.blob == _BLOB
    assert result.service == "Claude Code"


def test_read_keychain_blob_selects_fresher_candidate(monkeypatch):
    _force_macos(monkeypatch)
    stale_blob = json.dumps({
        "claudeAiOauth": {
            "accessToken": "stale",
            "refreshToken": "rt-stale",
            "expiresAt": 1_000,
        },
    })

    def _fake_run(cmd, **kwargs):
        service = cmd[cmd.index("-s") + 1]
        blob = stale_blob if service == "Claude Code-credentials" else _BLOB
        return _FakeCompletedProcess(0, stdout=blob)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = cm.read_keychain_blob()
    assert result.ok is True
    assert result.blob == _BLOB
    assert result.service == "Claude Code"


def test_read_keychain_blob_rejects_non_oauth_json(monkeypatch):
    _force_macos(monkeypatch)

    def _fake_run(cmd, **kwargs):
        service = cmd[cmd.index("-s") + 1]
        if service == "Claude Code-credentials":
            return _FakeCompletedProcess(0, stdout=json.dumps({"ok": True}))
        return _FakeCompletedProcess(44, stderr="entry not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = cm.read_keychain_blob()
    assert result.ok is False
    assert "invalid_oauth_blob" in result.error
    assert "exit_code=44" in result.error


def test_read_keychain_blob_skips_non_oauth_json_for_valid_candidate(monkeypatch):
    _force_macos(monkeypatch)

    def _fake_run(cmd, **kwargs):
        service = cmd[cmd.index("-s") + 1]
        if service == "Claude Code-credentials":
            return _FakeCompletedProcess(0, stdout=json.dumps({"ok": True}))
        return _FakeCompletedProcess(0, stdout=_BLOB)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = cm.read_keychain_blob()
    assert result.ok is True
    assert result.blob == _BLOB
    assert result.service == "Claude Code"


def test_read_keychain_blob_rejects_non_object_json_without_crashing(monkeypatch):
    # A valid-JSON-but-non-object blob (e.g. a bare number) must be
    # rejected cleanly, not raise AttributeError mid-read.
    _force_macos(monkeypatch)

    def _fake_run(cmd, **kwargs):
        service = cmd[cmd.index("-s") + 1]
        if service == "Claude Code-credentials":
            return _FakeCompletedProcess(0, stdout="5")
        return _FakeCompletedProcess(44, stderr="entry not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = cm.read_keychain_blob()
    assert result.ok is False
    assert "invalid_oauth_blob" in result.error


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
    assert "entry not found" in result.stderr


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


def test_bootstrap_overwrites_stale_cache_with_keychain(monkeypatch, tmp_path):
    """Regression: daemon restart with a stale cache (e.g. the user
    /login'd while the daemon was off) MUST pull the canonical blob
    from Keychain on bootstrap, not blindly trust the cache. Otherwise
    the daemon sync_to_agent fans out the stale RT, the spawned claude
    workers immediately 401, and the user sees auth errors until the
    401-wake recovers."""
    _force_macos(monkeypatch)
    cache = cm.CredentialCache.at(tmp_path)
    cache.write(_BLOB)  # stale cache from a previous daemon session
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout=_REFRESHED_BLOB),
    )
    ok, reason = cm.bootstrap_from_keychain(cache)
    assert ok is True
    assert reason == "bootstrapped"
    # Cache now reflects the canonical Keychain blob, not the stale one.
    assert cache.read() == _REFRESHED_BLOB


def test_bootstrap_falls_back_to_cache_when_keychain_read_fails(
    monkeypatch, tmp_path,
):
    """Transient Keychain read failure shouldn't crash the daemon if
    the cache has plausibly-current credentials — the 5-min external-
    rotation poll will keep trying. Degraded-mode boot."""
    _force_macos(monkeypatch)
    cache = cm.CredentialCache.at(tmp_path)
    cache.write(_BLOB)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(1, stderr="transient"),
    )
    ok, reason = cm.bootstrap_from_keychain(cache)
    assert ok is True
    assert "fell_back_to_cache" in reason
    # Cache untouched.
    assert cache.read() == _BLOB


def test_bootstrap_propagates_read_error_when_no_cache(monkeypatch, tmp_path):
    """No cache + Keychain unreadable → daemon can't bootstrap. Fail
    cleanly so the operator sees the cause."""
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
# KeychainBackend — plugged into CredentialRefresher
# ─────────────────────────────────────────────────────────────────────────────

class _FakeAsyncProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return (self._stdout, self._stderr)

    def kill(self):
        pass


def _make_keychain_backend(home: Path) -> cr.KeychainBackend:
    cache = cm.CredentialCache.at(home)
    return cr.KeychainBackend(home=home, cache=cache)


def test_keychain_backend_bootstrap_reads_keychain(monkeypatch, tmp_path):
    """``bootstrap`` should populate the cache from Keychain. The
    refresher calls this once on daemon-loop entry."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout=_BLOB),
    )
    backend = _make_keychain_backend(tmp_path)
    ok, reason = asyncio.run(backend.bootstrap())
    assert ok is True
    assert reason == "bootstrapped"
    assert backend.cache.read() == _BLOB
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


def _stub_keychain_sequence(monkeypatch, blobs):
    """Stub ``cm.read_keychain_blob`` to return successive blobs from
    ``blobs`` on each call (last one repeats if exhausted)."""
    queue = list(blobs)

    def fake_read(timeout=cm.SECURITY_TIMEOUT_SECONDS):
        blob = queue.pop(0) if len(queue) > 1 else queue[0]
        return cm.KeychainReadResult(True, blob, None, None)

    monkeypatch.setattr(cm, "read_keychain_blob", fake_read)


def test_keychain_backend_refresh_uses_real_home(monkeypatch, tmp_path):
    """KeychainBackend.refresh must spawn ``claude --print`` with the
    user's real HOME (NOT a sandbox HOME) — mirrors FileBackend so
    claude's own OAuth path writes Keychain directly the same way the
    user's interactive ``claude`` invocation does."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)

    spawned = {}

    async def _fake_spawn(*args, env=None, cwd=None, **kwargs):
        spawned["env"] = env
        spawned["cwd"] = cwd
        spawned["argv"] = args
        return _FakeAsyncProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    # First Keychain read returns the pre-refresh blob; second returns
    # the post-refresh (rotated) blob.
    _stub_keychain_sequence(monkeypatch, [_BLOB, _REFRESHED_BLOB])

    backend = _make_keychain_backend(tmp_path)
    backend.cache.write(_BLOB)

    outcome = asyncio.run(backend.refresh())
    assert outcome == cr.RefreshOutcome.REFRESHED
    # HOME = real user HOME, not a sandbox tempdir.
    assert spawned["env"]["HOME"] == str(Path.home())
    assert spawned["cwd"] == str(Path.home())
    # Sentinel against #37512 regression — must never set this env var.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in spawned["env"]
    # Cache is now synced to the rotated blob (read straight from Keychain).
    assert backend.cache.read() == _REFRESHED_BLOB
    assert backend._last_propagated_blob == _REFRESHED_BLOB


def test_keychain_backend_refresh_returns_unchanged_when_token_stays_fresh(
    monkeypatch, tmp_path,
):
    """When claude sees the token isn't close to expiring, it skips
    the OAuth round-trip and Keychain stays put. Backend reports
    UNCHANGED, not FAILED."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)

    async def _fake_spawn(*args, **kwargs):
        return _FakeAsyncProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    # Same blob both reads → Keychain unchanged.
    _stub_keychain_sequence(monkeypatch, [_BLOB, _BLOB])

    backend = _make_keychain_backend(tmp_path)
    backend.cache.write(_BLOB)
    outcome = asyncio.run(backend.refresh())
    assert outcome == cr.RefreshOutcome.UNCHANGED


def test_keychain_backend_refresh_returns_failed_on_claude_exit_failure(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(cm, "is_macos", lambda: True)

    async def _fake_spawn(*args, **kwargs):
        return _FakeAsyncProc(1, stderr=b"boom")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

    backend = _make_keychain_backend(tmp_path)
    backend.cache.write(_BLOB)
    outcome = asyncio.run(backend.refresh())
    assert outcome == cr.RefreshOutcome.FAILED


def test_keychain_backend_refresh_returns_failed_on_post_keychain_read_failure(
    monkeypatch, tmp_path,
):
    """claude exits 0 but the post-refresh Keychain re-read fails (e.g.
    transient permission issue). Don't poison the cache — return
    FAILED so the daemon retries on the next tick."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)

    async def _fake_spawn(*args, **kwargs):
        return _FakeAsyncProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(
        cm, "read_keychain_blob",
        lambda timeout=cm.SECURITY_TIMEOUT_SECONDS: cm.KeychainReadResult(
            False, None, "exit_code=44", None,
        ),
    )

    backend = _make_keychain_backend(tmp_path)
    backend.cache.write(_BLOB)
    outcome = asyncio.run(backend.refresh())
    assert outcome == cr.RefreshOutcome.FAILED
    # Cache must not have been clobbered.
    assert backend.cache.read() == _BLOB


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
    # Seed with a blob whose expiresAt is comfortably outside the
    # refresh safety margin, so _tick goes straight to _sync_views
    # without trying to spawn claude — otherwise the test would either
    # spawn the real binary (polluting tester credentials!) or need a
    # subprocess monkeypatch we don't care about here.
    import time as _time
    far_future_blob = json.dumps({
        "claudeAiOauth": {
            "accessToken": "x",
            "refreshToken": "y",
            "expiresAt": int((_time.time() + 24 * 3600) * 1000),  # +24h
        },
    })
    backend.cache.write(far_future_blob)
    refresher = cr.CredentialRefresher(backend=backend)

    agent_a = tmp_path / "agent-a"
    agent_b = tmp_path / "agent-b"
    refresher.register_agent(agent_a)
    refresher.register_agent(agent_b)

    asyncio.run(refresher._tick())

    assert (agent_a / ".claude" / ".credentials.json").read_text() == far_future_blob
    assert (agent_b / ".claude" / ".credentials.json").read_text() == far_future_blob


def test_refresher_with_keychain_backend_refreshes_when_close_to_expiry(
    monkeypatch, tmp_path,
):
    """Cache blob expiring soon → refresher triggers
    backend.refresh() with real HOME (mirror FileBackend)."""
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    backend = _make_keychain_backend(tmp_path)
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
        return _FakeAsyncProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(
        cm, "read_keychain_blob",
        lambda timeout=cm.SECURITY_TIMEOUT_SECONDS: cm.KeychainReadResult(
            True, _REFRESHED_BLOB, None, None,
        ),
    )

    refresher = cr.CredentialRefresher(backend=backend)
    asyncio.run(refresher._tick())
    assert spawned, "backend.refresh should have run claude --print"
    # Refresh uses real HOME — same model as FileBackend.
    env = spawned[0]
    assert env["HOME"] == str(Path.home())
