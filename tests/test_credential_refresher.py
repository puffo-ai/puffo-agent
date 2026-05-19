"""PUF-221: daemon-owned credential refresh — single writer to
``.credentials.json`` so Anthropic's single-use RT rotation can't be
raced by multi-agent ``claude --print`` callers."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from puffo_agent.portal import credential_refresh
from puffo_agent.portal.credential_refresh import (
    REFRESH_SAFETY_MARGIN_SECONDS,
    CredentialRefresher,
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
            "subscriptionType": "max",
        }
    }
    creds_path.write_text(json.dumps(payload))
    return creds_path


# ── expires_in_seconds + register/unregister ──────────────────


def test_expires_in_seconds_reads_disk_expiry(tmp_path):
    _write_creds(tmp_path, expires_in_seconds=600)
    r = CredentialRefresher(host_home=tmp_path)
    ttl = r.expires_in_seconds()
    assert ttl is not None
    assert 595 <= ttl <= 605  # within 5s of the seeded value


def test_expires_in_seconds_handles_missing_file(tmp_path):
    r = CredentialRefresher(host_home=tmp_path)
    assert r.expires_in_seconds() is None


def test_expires_in_seconds_handles_corrupt_file(tmp_path):
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("not json")
    r = CredentialRefresher(host_home=tmp_path)
    assert r.expires_in_seconds() is None


def test_register_unregister_agent(tmp_path):
    r = CredentialRefresher(host_home=tmp_path)
    a = tmp_path / "agent_a"
    b = tmp_path / "agent_b"
    r.register_agent(a)
    r.register_agent(b)
    assert a in r._agent_homes
    assert b in r._agent_homes
    r.unregister_agent(a)
    assert a not in r._agent_homes
    assert b in r._agent_homes
    # Idempotent — second unregister is a no-op.
    r.unregister_agent(a)
    assert a not in r._agent_homes


# ── notify_refresh_needed wakes the loop ──────────────────────


def test_notify_refresh_needed_sets_event(tmp_path):
    r = CredentialRefresher(host_home=tmp_path)
    assert not r._refresh_request.is_set()
    r.notify_refresh_needed()
    assert r._refresh_request.is_set()


# ── _tick: when fresh + no agent trigger → no refresh, view-sync only


def test_tick_no_refresh_when_fresh_and_no_agent_trigger(tmp_path, monkeypatch):
    """Fresh token + no 401 from any agent → tick should NOT spawn a
    refresh subprocess. It should still call view-sync so external
    `claude /login` propagates."""
    _write_creds(tmp_path, expires_in_seconds=3600)  # 1h, well above 10min margin
    r = CredentialRefresher(host_home=tmp_path)

    spawned: list = []

    async def fake_exec(*argv, **kwargs):
        spawned.append(argv)
        raise AssertionError("should not have spawned a refresh subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(r._tick())
    assert spawned == []


# ── _tick: when close to expiry → refresh fires ───────────────


def test_tick_refreshes_when_close_to_expiry(tmp_path, monkeypatch):
    """Token expires in 5 min < 10min safety margin → daemon refresh
    should fire (subprocess spawn observed)."""
    _write_creds(tmp_path, expires_in_seconds=5 * 60)
    r = CredentialRefresher(host_home=tmp_path)

    spawned: list = []

    async def fake_exec(*argv, **kwargs):
        spawned.append({"argv": argv, "env_home": kwargs.get("env", {}).get("HOME")})
        # Simulate clean exit without rewriting the file.
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(r._tick())
    assert len(spawned) == 1
    # The refresh subprocess must run with HOST_HOME so claude
    # writes to the canonical path.
    assert spawned[0]["env_home"] == str(tmp_path)
    # And it must be the claude --print invocation, not something else.
    assert spawned[0]["argv"][0] == "claude"
    assert "--print" in spawned[0]["argv"]


def test_tick_refreshes_when_agent_reports_401(tmp_path, monkeypatch):
    """Token still fresh, but an agent set the 401 event → refresh
    should fire anyway (fast-recovery path). ``run_loop`` reads +
    clears ``_refresh_request`` and passes ``triggered_by_agent=True``
    down to ``_tick``."""
    _write_creds(tmp_path, expires_in_seconds=3600)  # plenty of time left
    r = CredentialRefresher(host_home=tmp_path)

    spawned: list = []

    async def fake_exec(*argv, **kwargs):
        spawned.append(argv)
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(r._tick(triggered_by_agent=True))
    assert len(spawned) == 1


# ── view-sync runs after every tick ───────────────────────────


def test_tick_fans_view_sync_to_registered_agents(tmp_path, monkeypatch):
    """After a tick, every registered agent's view should be re-synced
    so external `claude /login` propagates from host to agent paths."""
    _write_creds(tmp_path, expires_in_seconds=3600)
    r = CredentialRefresher(host_home=tmp_path)
    a = tmp_path / "agent_a"
    b = tmp_path / "agent_b"
    r.register_agent(a)
    r.register_agent(b)

    synced: list[Path] = []

    def fake_link(host_home, agent_home):
        synced.append(Path(agent_home))
        return "symlink"

    monkeypatch.setattr(credential_refresh, "link_host_credentials", fake_link)
    asyncio.run(r._tick())
    assert set(synced) == {a, b}


# ── run_loop: respects stop event + wakes on refresh-request ──


def test_run_loop_exits_on_stop_event(tmp_path, monkeypatch):
    """run_loop must observe the stop event and exit promptly."""
    _write_creds(tmp_path, expires_in_seconds=3600)
    r = CredentialRefresher(host_home=tmp_path)
    stop = asyncio.Event()

    # Fast-fail any subprocess spawn — we shouldn't refresh on this path.
    monkeypatch.setattr(
        credential_refresh, "REFRESH_POLL_SECONDS", 100,  # long timeout
    )

    async def go():
        task = asyncio.ensure_future(r.run_loop(stop))
        # Let one tick happen, then signal stop.
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(go())  # should not hang or raise


def test_run_loop_wakes_early_on_refresh_request(tmp_path, monkeypatch):
    """When an agent reports a 401 via notify_refresh_needed, the
    daemon's loop must wake before the next poll interval."""
    _write_creds(tmp_path, expires_in_seconds=3600)
    r = CredentialRefresher(host_home=tmp_path)
    stop = asyncio.Event()

    refreshes: list[float] = []

    async def fake_exec(*argv, **kwargs):
        refreshes.append(time.time())
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    # Long poll interval so we KNOW the wake came from the event, not
    # the timer.
    monkeypatch.setattr(credential_refresh, "REFRESH_POLL_SECONDS", 100)

    async def go():
        task = asyncio.ensure_future(r.run_loop(stop))
        # Wait past one tick so the loop is parked in _sleep_until_next_tick.
        await asyncio.sleep(0.05)
        started_wait = time.time()
        r.notify_refresh_needed()
        # Loop should wake, do a refresh, and the subprocess should be
        # called within a fraction of a second — NOT in 100s.
        await asyncio.sleep(0.5)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        return started_wait

    started = asyncio.run(go())
    assert len(refreshes) >= 1
    wake_latency = refreshes[0] - started
    # Conservative bound: should wake within 1s of the event.
    assert wake_latency < 1.0, f"wake_latency={wake_latency:.3f}s — event didn't short-circuit poll"


# ── refresh-token flag round-trip (CLI ↔ daemon sentinel) ──────


def test_refresh_token_request_flag_round_trip(tmp_path, monkeypatch):
    """``puffo-agent agent refresh-token`` writes the sentinel
    ``write_refresh_token_request()``; the daemon's reconcile loop
    detects via ``refresh_token_request_path().exists()`` and clears
    via ``clear_refresh_token_request()``. Round-trip should be
    consistent and idempotent."""
    from puffo_agent.portal.state import (
        clear_refresh_token_request,
        refresh_token_request_path,
        write_refresh_token_request,
    )
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))

    assert not refresh_token_request_path().exists()
    write_refresh_token_request()
    assert refresh_token_request_path().exists()
    # The payload is the unix ts at write time — readable as an int.
    assert refresh_token_request_path().read_text().isdigit()

    clear_refresh_token_request()
    assert not refresh_token_request_path().exists()
    # Clearing a missing flag is idempotent (daemon may catch a stale
    # flag on next tick after it was already cleared).
    clear_refresh_token_request()
    assert not refresh_token_request_path().exists()
