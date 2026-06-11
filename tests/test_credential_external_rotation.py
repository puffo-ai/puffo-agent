"""Bug-2: external credential rotation (operator `claude /login`) must
propagate to running agents on copy-mode hosts (Windows), where the
FileBackend's symlink-propagation assumption doesn't hold.

- backends expose a ``fingerprint`` so the refresher can spot the change
- the refresher fires refresh-success on a detected change (not on the
  first/baseline tick)
- the daemon's on_refresh_success restarts agents that were auth_failed
  so their claude session respawns with the fresh credential
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from puffo_agent.portal.credential_refresh import (
    CodexFileBackend,
    CredentialRefresher,
    FileBackend,
)


def _write_creds(host_home: Path, *, expires_in_seconds: int) -> Path:
    creds_path = host_home / ".claude" / ".credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-test",
            "refreshToken": "sk-ant-ort01-test",
            "expiresAt": int((time.time() + expires_in_seconds) * 1000),
            "scopes": ["user:inference"],
        }
    }
    creds_path.write_text(json.dumps(payload))
    return creds_path


# ── backend fingerprints ───────────────────────────────────────────


def test_filebackend_fingerprint(tmp_path):
    fb = FileBackend(host_home=tmp_path)
    assert fb.fingerprint() is None            # missing host file
    p = _write_creds(tmp_path, expires_in_seconds=3600)
    fp = fb.fingerprint()
    assert fp is not None and fp[1] == p.stat().st_size


def test_codexbackend_fingerprint(tmp_path):
    cb = CodexFileBackend(host_home=tmp_path)
    assert cb.fingerprint() is None
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"tokens": {"access_token": "x"}}')
    fp = cb.fingerprint()
    assert fp is not None and fp[1] == auth.stat().st_size


# ── change detection ───────────────────────────────────────────────


def test_detect_fires_on_fingerprint_change(tmp_path, monkeypatch):
    _write_creds(tmp_path, expires_in_seconds=3600)
    r = CredentialRefresher(host_home=tmp_path)
    fired: list[int] = []
    r.register_on_refresh_success(lambda: fired.append(1))

    seq = {"fp": (1, 10)}
    monkeypatch.setattr(r.backend, "fingerprint", lambda: seq["fp"])

    # No baseline yet → no fire.
    r._detect_external_rotation()
    assert fired == []

    r._record_cred_fingerprint()        # baseline = (1, 10)
    seq["fp"] = (2, 11)                  # operator re-login
    r._detect_external_rotation()
    assert fired == [1]

    # Unchanged on the next tick → no re-fire.
    r._record_cred_fingerprint()        # baseline = (2, 11)
    r._detect_external_rotation()
    assert fired == [1]


@pytest.mark.asyncio
async def test_first_tick_establishes_baseline_without_firing(tmp_path):
    """A fresh daemon start must NOT fire refresh-success (which would
    mass-restart agents); the first tick only records the baseline."""
    _write_creds(tmp_path, expires_in_seconds=3600)   # far future → no refresh
    r = CredentialRefresher(host_home=tmp_path)
    fired: list[int] = []
    r.register_on_refresh_success(lambda: fired.append(1))

    await r._tick()
    assert fired == []
    assert r._last_cred_fingerprint is not None


# ── daemon: restart auth_failed agents on recovery ─────────────────


def _daemon_harness(monkeypatch, tmp_path, health: str):
    from puffo_agent.portal import daemon as daemon_module
    from puffo_agent.portal.state import RuntimeState

    flag = tmp_path / "restart.flag"
    monkeypatch.setattr(daemon_module, "restart_flag_path", lambda aid: flag)

    class _StubRefresher:
        def __init__(self):
            self.callback = None

        def register_agent(self, _p):
            pass

        def register_on_refresh_success(self, cb):
            self.callback = cb

    class _StubAgentCfg:
        id = "t-agent"

        class runtime:
            harness = "claude-code"

        class puffo_core:
            slug = "alice-0001"

    class _StubWorker:
        agent_cfg = _StubAgentCfg()
        runtime = RuntimeState(status="running", started_at=0, msg_count=0)
        _auth_failed_notification_sent = True
        _refresh_success_callback = None

    class _StubDaemon:
        refresher = _StubRefresher()
        codex_refresher = _StubRefresher()

        def _refresher_for(self, _cfg):
            return self.refresher

        _register_with_refresher = daemon_module.Daemon._register_with_refresher

    d = _StubDaemon()
    w = _StubWorker()
    w.runtime.health = health
    d._register_with_refresher(w.agent_cfg, w)
    return d, w, flag


def test_on_refresh_success_restarts_auth_failed_agent(tmp_path, monkeypatch):
    d, w, flag = _daemon_harness(monkeypatch, tmp_path, "auth_failed")
    d.refresher.callback()
    assert w.runtime.health == "ok"
    assert flag.exists()        # restart requested to pick up new cred


def test_on_refresh_success_no_restart_when_healthy(tmp_path, monkeypatch):
    d, w, flag = _daemon_harness(monkeypatch, tmp_path, "ok")
    d.refresher.callback()
    assert not flag.exists()    # nothing to recover → no restart


# ── new message while auth_failed wakes the refresher ──────────────


def _wake_stub(health: str, *, has_cb: bool = True):
    from puffo_agent.portal.worker import Worker

    class _RT:
        def __init__(self, h):
            self.health = h

    class _W:
        pass

    w = _W()
    w.runtime = _RT(health)
    w.fired: list[int] = []
    w._notify_refresh_needed = (lambda: w.fired.append(1)) if has_cb else None
    return Worker, w


def test_new_message_while_auth_failed_wakes_refresher():
    Worker, w = _wake_stub("auth_failed")
    Worker._maybe_wake_refresher_if_auth_failed(w, "t-agent")
    assert w.fired == [1]


def test_new_message_when_healthy_does_not_wake():
    Worker, w = _wake_stub("ok")
    Worker._maybe_wake_refresher_if_auth_failed(w, "t-agent")
    assert w.fired == []


def test_wake_is_noop_without_notify_callback():
    Worker, w = _wake_stub("auth_failed", has_cb=False)
    Worker._maybe_wake_refresher_if_auth_failed(w, "t-agent")   # no crash


def test_wake_survives_notify_raising():
    Worker, w = _wake_stub("auth_failed")

    def _boom():
        raise RuntimeError("no loop")

    w._notify_refresh_needed = _boom
    Worker._maybe_wake_refresher_if_auth_failed(w, "t-agent")   # no crash
