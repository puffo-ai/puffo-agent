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
    REFRESH_BROKEN_THRESHOLD,
    REFRESH_SAFETY_MARGIN_SECONDS,
    CredentialRefresher,
    RefreshOutcome,
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
    refresh subprocess. It SHOULD still call view-sync so external
    `claude /login` (operator running it manually) propagates to
    registered agents."""
    _write_creds(tmp_path, expires_in_seconds=3600)  # 1h, well above 10min margin
    r = CredentialRefresher(host_home=tmp_path)
    agent_a = tmp_path / "agent_a"
    r.register_agent(agent_a)

    spawned: list = []

    async def fake_exec(*argv, **kwargs):
        spawned.append(argv)
        raise AssertionError("should not have spawned a refresh subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    sync_calls: list[tuple[Path, Path]] = []

    def fake_link(host_home, agent_home):
        sync_calls.append((host_home, agent_home))

    monkeypatch.setattr(credential_refresh, "link_host_credentials", fake_link)

    asyncio.run(r._tick())
    assert spawned == []
    # View-sync MUST fire even when no refresh happened — that's how
    # an out-of-band `claude /login` from the operator propagates.
    assert sync_calls == [(tmp_path, agent_a)]


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


# ── PUF-265: outcome dispatch + refresh_broken propagation ─────


def _make_agent_runtime(home_root: Path, agent_id: str) -> Path:
    """Seed an ``agents/<id>/runtime.json`` under ``home_root`` so
    ``RuntimeState.load(agent_id)`` resolves via ``PUFFO_AGENT_HOME``."""
    from puffo_agent.portal.state import RuntimeState
    rs = RuntimeState(status="running", started_at=int(time.time()))
    # Need PUFFO_AGENT_HOME to be set before save() — the caller's
    # monkeypatch handles that.
    rs.save(agent_id)
    return home_root / "agents" / agent_id / "runtime.json"


def _make_refresher_with_agent(
    tmp_path: Path, monkeypatch, *, agent_id: str = "agent-puf265",
) -> tuple[CredentialRefresher, str]:
    """Build a refresher whose registered agent maps to a real
    runtime.json under ``PUFFO_AGENT_HOME=tmp_path``."""
    from puffo_agent.portal.state import agent_home_dir

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    # Seed runtime + creds.
    _write_creds(tmp_path / "host", expires_in_seconds=3600)
    _make_agent_runtime(tmp_path, agent_id)

    r = CredentialRefresher(host_home=tmp_path / "host")
    r.register_agent(agent_home_dir(agent_id))
    return r, agent_id


def test_propagate_outcome_refreshed_resets_counter(tmp_path, monkeypatch):
    r, _aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    r._consecutive_non_success = 1
    r._propagate_outcome(RefreshOutcome.REFRESHED)
    assert r._consecutive_non_success == 0


def test_propagate_outcome_unchanged_increments_counter(tmp_path, monkeypatch):
    r, _aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    r._propagate_outcome(RefreshOutcome.UNCHANGED)
    assert r._consecutive_non_success == 1


def test_propagate_outcome_failed_increments_counter(tmp_path, monkeypatch):
    r, _aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    r._propagate_outcome(RefreshOutcome.FAILED)
    assert r._consecutive_non_success == 1


def test_refresh_broken_flips_after_threshold_consecutive(tmp_path, monkeypatch):
    """N consecutive UNCHANGED|FAILED outcomes must flip every
    registered agent's ``runtime.health`` to ``"refresh_broken"`` with a
    human-readable ``runtime.error``. Below threshold, health stays
    untouched."""
    from puffo_agent.portal.state import RuntimeState
    r, aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    # 1 non-success: below threshold, no flip.
    r._propagate_outcome(RefreshOutcome.UNCHANGED)
    rs = RuntimeState.load(aid)
    assert rs is not None
    assert rs.health != "refresh_broken"
    # 2nd consecutive non-success hits the threshold → flip.
    assert REFRESH_BROKEN_THRESHOLD == 2
    r._propagate_outcome(RefreshOutcome.UNCHANGED)
    rs = RuntimeState.load(aid)
    assert rs is not None
    assert rs.health == "refresh_broken"
    assert "claude /login" in rs.error
    assert "unchanged" in rs.error


def test_refresh_broken_clears_on_next_refreshed(tmp_path, monkeypatch):
    """A REFRESHED outcome after the flip must clear ``refresh_broken``
    back to ``"ok"`` AND reset the counter, so the next streak starts
    fresh."""
    from puffo_agent.portal.state import RuntimeState
    r, aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    # Drive the flip.
    for _ in range(REFRESH_BROKEN_THRESHOLD):
        r._propagate_outcome(RefreshOutcome.UNCHANGED)
    assert RuntimeState.load(aid).health == "refresh_broken"
    # Recover.
    r._propagate_outcome(RefreshOutcome.REFRESHED)
    rs = RuntimeState.load(aid)
    assert rs is not None
    assert rs.health == "ok"
    assert rs.error == ""
    assert r._consecutive_non_success == 0


def test_refresh_broken_does_not_overwrite_auth_failed(tmp_path, monkeypatch):
    """``auth_failed`` is a stronger downstream signal — refresh_broken
    is the upstream warning that the daemon's refresh mechanism is
    dead. Once any agent has actually been blocked by a 401 and worker
    flipped it to ``auth_failed``, refresh_broken must NOT downgrade
    that signal."""
    from puffo_agent.portal.state import RuntimeState
    r, aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    # Seed auth_failed on the agent first.
    rs = RuntimeState.load(aid)
    rs.health = "auth_failed"
    rs.error = "401 from a real turn"
    rs.save(aid)
    # Drive the would-be flip.
    for _ in range(REFRESH_BROKEN_THRESHOLD):
        r._propagate_outcome(RefreshOutcome.UNCHANGED)
    rs = RuntimeState.load(aid)
    assert rs is not None
    assert rs.health == "auth_failed", (
        "refresh_broken should not overwrite auth_failed"
    )
    assert rs.error == "401 from a real turn"


def test_refresh_broken_does_not_overwrite_api_error_abandoned(
    tmp_path, monkeypatch,
):
    from puffo_agent.portal.state import RuntimeState
    r, aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    rs = RuntimeState.load(aid)
    rs.health = "api_error_abandoned"
    rs.save(aid)
    for _ in range(REFRESH_BROKEN_THRESHOLD):
        r._propagate_outcome(RefreshOutcome.FAILED)
    rs = RuntimeState.load(aid)
    assert rs.health == "api_error_abandoned"


def test_refresh_now_captures_outcome_instead_of_dropping(tmp_path, monkeypatch):
    """Regression for the PUF-265 root cause: the caller at line 779
    used to do ``await self.backend.refresh()`` without capturing the
    outcome, so UNCHANGED silently looked like REFRESHED to
    ``_refresh_now``. This test pins the new contract — outcome MUST
    feed ``_propagate_outcome`` for the counter + flip to work."""
    r, _aid = _make_refresher_with_agent(tmp_path, monkeypatch)

    captured: list[RefreshOutcome] = []

    class _FakeBackend:
        def expires_in_seconds(self):
            return 60
        async def refresh(self):
            return RefreshOutcome.UNCHANGED
        def sync_to_agent(self, agent_home):
            pass

    r.backend = _FakeBackend()
    orig_propagate = r._propagate_outcome

    def spy_propagate(outcome):
        captured.append(outcome)
        orig_propagate(outcome)

    r._propagate_outcome = spy_propagate  # type: ignore[method-assign]
    asyncio.run(r._refresh_now(expires_in=60, by_agent=True))
    assert captured == [RefreshOutcome.UNCHANGED]


def test_refresh_now_treats_backend_exception_as_failed(tmp_path, monkeypatch):
    """A backend that raises during ``refresh()`` must be counted as a
    non-success outcome (FAILED) so a daemon-side bug or transient
    subprocess error contributes to the refresh_broken streak instead
    of being silently swallowed."""
    r, _aid = _make_refresher_with_agent(tmp_path, monkeypatch)

    captured: list[RefreshOutcome] = []

    class _ExplodingBackend:
        def expires_in_seconds(self):
            return 60
        async def refresh(self):
            raise RuntimeError("backend exploded")
        def sync_to_agent(self, agent_home):
            pass

    r.backend = _ExplodingBackend()
    orig_propagate = r._propagate_outcome

    def spy_propagate(outcome):
        captured.append(outcome)
        orig_propagate(outcome)

    r._propagate_outcome = spy_propagate  # type: ignore[method-assign]
    asyncio.run(r._refresh_now(expires_in=60, by_agent=True))
    assert captured == [RefreshOutcome.FAILED]


def test_filebackend_unchanged_logs_stdout_and_stderr(tmp_path, monkeypatch, caplog):
    """The UNCHANGED branch must dump ``stdout`` + ``stderr`` tails
    (real-fix discrimination prereq: differentiates 2a CLI-stopped-
    refreshing from 2b Linux-writeback-failure on the next in-the-wild
    event without needing another roundtrip)."""
    from puffo_agent.portal.credential_refresh import FileBackend
    _write_creds(tmp_path, expires_in_seconds=3600)  # advance==no-advance both fine; we force the branch
    backend = FileBackend(host_home=tmp_path)

    class _Proc:
        returncode = 0
        async def communicate(self):
            return b"hello-out", b"hello-err"

    async def fake_exec(*argv, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    caplog.set_level("ERROR", logger="puffo_agent.portal.credential_refresh")
    outcome = asyncio.run(backend.refresh())
    assert outcome is RefreshOutcome.UNCHANGED
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "hello-out" in joined
    assert "hello-err" in joined


def test_refresh_broken_flips_all_registered_agents(tmp_path, monkeypatch):
    """When N consecutive non-success outcomes hit, every registered
    agent gets the flip — not just the first one in the set."""
    from puffo_agent.portal.state import RuntimeState, agent_home_dir
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    _write_creds(tmp_path / "host", expires_in_seconds=3600)
    _make_agent_runtime(tmp_path, "agent-alpha")
    _make_agent_runtime(tmp_path, "agent-beta")

    r = CredentialRefresher(host_home=tmp_path / "host")
    r.register_agent(agent_home_dir("agent-alpha"))
    r.register_agent(agent_home_dir("agent-beta"))

    for _ in range(REFRESH_BROKEN_THRESHOLD):
        r._propagate_outcome(RefreshOutcome.UNCHANGED)

    assert RuntimeState.load("agent-alpha").health == "refresh_broken"
    assert RuntimeState.load("agent-beta").health == "refresh_broken"
