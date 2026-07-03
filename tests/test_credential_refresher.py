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
    RATE_LIMIT_FAST_RETRY_MAX_SECONDS,
    RATE_LIMIT_FAST_RETRY_MIN_SECONDS,
    REFRESH_BROKEN_THRESHOLD,
    REFRESH_PROBE_MODEL,
    REFRESH_SAFETY_MARGIN_SECONDS,
    CredentialRefresher,
    FileBackend,
    RefreshOutcome,
    _build_probe_cmd,
    _looks_like_model_not_found,
    _looks_like_rate_limit,
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

    monkeypatch.setattr(credential_refresh, "sync_host_credentials_view", fake_link)

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
        return "view"

    monkeypatch.setattr(credential_refresh, "sync_host_credentials_view", fake_link)
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
    from puffo_agent.portal.state import RuntimeState
    rs = RuntimeState(status="running", started_at=int(time.time()))
    rs.save(agent_id)
    return home_root / "agents" / agent_id / "runtime.json"


def _make_refresher_with_agent(
    tmp_path: Path, monkeypatch, *, agent_id: str = "agent-puf265",
) -> tuple[CredentialRefresher, str]:
    from puffo_agent.portal.state import agent_home_dir

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
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


def test_refresh_broken_flips_after_threshold_consecutive(tmp_path, monkeypatch, caplog):
    from puffo_agent.portal.state import RuntimeState
    import logging
    r, aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    r._propagate_outcome(RefreshOutcome.UNCHANGED)
    rs = RuntimeState.load(aid)
    assert rs is not None
    assert rs.health != "refresh_broken"
    assert REFRESH_BROKEN_THRESHOLD == 2
    with caplog.at_level(logging.WARNING, logger="puffo_agent.portal.credential_refresh"):
        r._propagate_outcome(RefreshOutcome.UNCHANGED)
    rs = RuntimeState.load(aid)
    assert rs is not None
    assert rs.health == "refresh_broken"
    assert "Claude Code sign-in couldn't be refreshed" in rs.error
    assert "claude auth login" in rs.error
    # Outcome-class debug stays in the daemon log, not in runtime.error.
    assert any(
        "flipping refresh_broken" in rec.getMessage() and "unchanged" in rec.getMessage()
        for rec in caplog.records
    )


def test_refresh_broken_clears_on_next_refreshed(tmp_path, monkeypatch):
    from puffo_agent.portal.state import RuntimeState
    r, aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    for _ in range(REFRESH_BROKEN_THRESHOLD):
        r._propagate_outcome(RefreshOutcome.UNCHANGED)
    assert RuntimeState.load(aid).health == "refresh_broken"
    r._propagate_outcome(RefreshOutcome.REFRESHED)
    rs = RuntimeState.load(aid)
    assert rs is not None
    assert rs.health == "ok"
    assert rs.error == ""
    assert r._consecutive_non_success == 0


def test_refresh_broken_cleared_after_daemon_restart(tmp_path, monkeypatch):
    from puffo_agent.portal.state import RuntimeState
    r, aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    rs = RuntimeState.load(aid)
    rs.health = "refresh_broken"
    rs.error = "left over from previous daemon"
    rs.save(aid)
    assert r._consecutive_non_success == 0
    r._propagate_outcome(RefreshOutcome.REFRESHED)
    rs = RuntimeState.load(aid)
    assert rs.health == "ok"
    assert rs.error == ""


def test_refresh_broken_does_not_overwrite_auth_failed(tmp_path, monkeypatch):
    from puffo_agent.portal.state import RuntimeState
    r, aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    rs = RuntimeState.load(aid)
    rs.health = "auth_failed"
    rs.error = "401 from a real turn"
    rs.save(aid)
    for _ in range(REFRESH_BROKEN_THRESHOLD):
        r._propagate_outcome(RefreshOutcome.UNCHANGED)
    rs = RuntimeState.load(aid)
    assert rs is not None
    assert rs.health == "auth_failed"
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
    from puffo_agent.portal.credential_refresh import FileBackend
    _write_creds(tmp_path, expires_in_seconds=3600)
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


def test_refresh_broken_flip_is_idempotent_past_threshold(tmp_path, monkeypatch):
    from puffo_agent.portal.state import RuntimeState
    r, aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    r._propagate_outcome(RefreshOutcome.UNCHANGED)
    r._propagate_outcome(RefreshOutcome.UNCHANGED)
    rs_after_flip = RuntimeState.load(aid)
    assert rs_after_flip.health == "refresh_broken"
    initial_error = rs_after_flip.error
    # Drive 3 more non-success ticks. In-memory counter climbs but the
    # already-refresh_broken agent's disk state must not be re-written
    # (avoids log spam + redundant disk writes once flipped).
    r._propagate_outcome(RefreshOutcome.FAILED)
    r._propagate_outcome(RefreshOutcome.UNCHANGED)
    r._propagate_outcome(RefreshOutcome.FAILED)
    rs_later = RuntimeState.load(aid)
    assert rs_later.health == "refresh_broken"
    # Error message still reports "saw 2 consecutive ..." not 5 — the
    # inner-loop guard `health == "refresh_broken": continue` blocked
    # the re-write.
    assert rs_later.error == initial_error
    assert r._consecutive_non_success == 5


def test_refresh_broken_does_not_touch_unregistered_agents(tmp_path, monkeypatch):
    from puffo_agent.portal.state import RuntimeState, agent_home_dir
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    _write_creds(tmp_path / "host", expires_in_seconds=3600)
    _make_agent_runtime(tmp_path, "agent-stays")
    _make_agent_runtime(tmp_path, "agent-leaves")

    r = CredentialRefresher(host_home=tmp_path / "host")
    r.register_agent(agent_home_dir("agent-stays"))
    r.register_agent(agent_home_dir("agent-leaves"))
    r.unregister_agent(agent_home_dir("agent-leaves"))

    for _ in range(REFRESH_BROKEN_THRESHOLD):
        r._propagate_outcome(RefreshOutcome.UNCHANGED)

    assert RuntimeState.load("agent-stays").health == "refresh_broken"
    assert RuntimeState.load("agent-leaves").health != "refresh_broken"


def test_refreshed_outcome_does_not_lift_unrelated_health_to_ok(
    tmp_path, monkeypatch,
):
    # _clear_refresh_broken runs on every REFRESHED. An agent currently
    # at "unknown" (no probe yet) must NOT be silently lifted to "ok" —
    # the inner-loop guard `health != "refresh_broken": continue` is
    # what prevents that.
    from puffo_agent.portal.state import RuntimeState
    r, aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    rs_before = RuntimeState.load(aid)
    assert rs_before.health == "unknown"
    r._propagate_outcome(RefreshOutcome.REFRESHED)
    rs_after = RuntimeState.load(aid)
    assert rs_after.health == "unknown"
    assert rs_after.error == ""


def test_refresh_broken_streak_mixes_unchanged_and_failed(tmp_path, monkeypatch, caplog):
    from puffo_agent.portal.state import RuntimeState
    import logging
    r, aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="puffo_agent.portal.credential_refresh"):
        r._propagate_outcome(RefreshOutcome.UNCHANGED)
        r._propagate_outcome(RefreshOutcome.FAILED)
    rs = RuntimeState.load(aid)
    assert rs.health == "refresh_broken"
    assert "Claude Code sign-in couldn't be refreshed" in rs.error
    assert "claude auth login" in rs.error
    # Latest-outcome class is logged, not written into runtime.error.
    assert any(
        "flipping refresh_broken" in rec.getMessage() and "failed" in rec.getMessage()
        for rec in caplog.records
    )


# ── PUF-265 v2: Haiku probe model + rate-limit fast retry ────────────


def test_refresh_probe_passes_model_flag(tmp_path, monkeypatch):
    _write_creds(tmp_path, expires_in_seconds=5 * 60)
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
    asyncio.run(r._tick())
    assert len(spawned) == 1
    argv = list(spawned[0])
    assert REFRESH_PROBE_MODEL == "claude-haiku-4-5"
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == REFRESH_PROBE_MODEL


def test_looks_like_rate_limit_matches_canonical_signatures():
    canonicals = [
        "API Error: Request rejected (429)",
        "Server is temporarily limiting requests, please wait a moment...",
        'message: {"type":"rate_limit_error","message":"slow down"}',
        "rate_limit_error",
        "You've hit your 5-hour usage limit. Please try again later.",
        "Repeated 529 Overloaded errors",
    ]
    for s in canonicals:
        assert _looks_like_rate_limit("", s), f"missed canonical: {s!r}"
        assert _looks_like_rate_limit(s, ""), f"missed in stdout: {s!r}"
    benign = [
        "ok\n",
        "Hello world",
        "Some refresh log line",
        "",
    ]
    for s in benign:
        assert not _looks_like_rate_limit(s, "")
        assert not _looks_like_rate_limit("", s)


def test_looks_like_rate_limit_is_case_insensitive():
    # All patterns use re.IGNORECASE — pin so a future "drop the flag"
    # refactor breaks the test (Anthropic surfaces casing varies).
    assert _looks_like_rate_limit("", "API ERROR: REQUEST REJECTED (429)")
    assert _looks_like_rate_limit("", "Rate_Limit_Error")
    assert _looks_like_rate_limit("", "server is TEMPORARILY limiting requests")


def test_filebackend_returns_rate_limited_on_canonical_stderr(tmp_path, monkeypatch):
    _write_creds(tmp_path, expires_in_seconds=5 * 60)
    backend = FileBackend(host_home=tmp_path)

    class _Proc:
        returncode = 1
        async def communicate(self):
            return b"", b"API Error: Request rejected (429)\n"

    async def fake_exec(*argv, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    outcome = asyncio.run(backend.refresh())
    assert outcome is RefreshOutcome.RATE_LIMITED


def test_filebackend_returns_failed_on_non_rate_limit_stderr(tmp_path, monkeypatch):
    _write_creds(tmp_path, expires_in_seconds=5 * 60)
    backend = FileBackend(host_home=tmp_path)

    class _Proc:
        returncode = 1
        async def communicate(self):
            return b"", b"some other error\n"

    async def fake_exec(*argv, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    outcome = asyncio.run(backend.refresh())
    assert outcome is RefreshOutcome.FAILED


def test_filebackend_rate_limit_pattern_matches_in_stdout_too(tmp_path, monkeypatch):
    # _classify_failed_refresh joins stdout+stderr — make sure the
    # detector doesn't only check stderr (claude can emit the rate-limit
    # error as an assistant text block on stdout, not stderr).
    _write_creds(tmp_path, expires_in_seconds=5 * 60)
    backend = FileBackend(host_home=tmp_path)

    class _Proc:
        returncode = 1
        async def communicate(self):
            return b'{"type":"rate_limit_error"}\n', b""

    async def fake_exec(*argv, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    outcome = asyncio.run(backend.refresh())
    assert outcome is RefreshOutcome.RATE_LIMITED


def test_propagate_outcome_rate_limited_counts_toward_streak(tmp_path, monkeypatch):
    r, _aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    r._propagate_outcome(RefreshOutcome.RATE_LIMITED)
    assert r._consecutive_non_success == 1
    r._propagate_outcome(RefreshOutcome.RATE_LIMITED)
    assert r._consecutive_non_success >= REFRESH_BROKEN_THRESHOLD


def test_propagate_outcome_rate_limited_schedules_fast_retry(tmp_path, monkeypatch):
    r, _aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(credential_refresh, "RATE_LIMIT_FAST_RETRY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(credential_refresh, "RATE_LIMIT_FAST_RETRY_MAX_SECONDS", 0.0)

    async def go():
        assert not r._refresh_request.is_set()
        r._propagate_outcome(RefreshOutcome.RATE_LIMITED)
        assert r._rate_limit_retry_task is not None
        await asyncio.sleep(0.05)
        assert r._refresh_request.is_set()

    asyncio.run(go())


def test_rate_limit_retry_tasks_coalesce(tmp_path, monkeypatch):
    r, _aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(credential_refresh, "RATE_LIMIT_FAST_RETRY_MIN_SECONDS", 5.0)
    monkeypatch.setattr(credential_refresh, "RATE_LIMIT_FAST_RETRY_MAX_SECONDS", 5.0)

    async def go():
        r._propagate_outcome(RefreshOutcome.RATE_LIMITED)
        task1 = r._rate_limit_retry_task
        assert task1 is not None
        r._propagate_outcome(RefreshOutcome.RATE_LIMITED)
        task2 = r._rate_limit_retry_task
        assert task2 is task1, "expected coalesce; got a new task"
        task1.cancel()
        try:
            await task1
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(go())


def test_rate_limit_retry_reschedules_after_first_task_done(tmp_path, monkeypatch):
    # Once the first retry task completes, a subsequent RATE_LIMITED
    # outcome must spawn a NEW task (not be permanently blocked).
    r, _aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(credential_refresh, "RATE_LIMIT_FAST_RETRY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(credential_refresh, "RATE_LIMIT_FAST_RETRY_MAX_SECONDS", 0.0)

    async def go():
        r._propagate_outcome(RefreshOutcome.RATE_LIMITED)
        task1 = r._rate_limit_retry_task
        await asyncio.sleep(0.05)
        assert task1.done()
        r._propagate_outcome(RefreshOutcome.RATE_LIMITED)
        task2 = r._rate_limit_retry_task
        assert task2 is not task1, "expected new task after first completed"

    asyncio.run(go())


def test_refreshed_outcome_does_not_schedule_fast_retry(tmp_path, monkeypatch):
    r, _aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    r._propagate_outcome(RefreshOutcome.REFRESHED)
    assert r._rate_limit_retry_task is None


def test_propagate_outcome_rate_limited_does_not_crash_without_event_loop(
    tmp_path, monkeypatch,
):
    # The _schedule_rate_limit_retry's RuntimeError fallback path: in
    # a sync test (no running loop) the create_task call would raise.
    # The function must catch it and leave _rate_limit_retry_task=None
    # rather than propagate.
    r, _aid = _make_refresher_with_agent(tmp_path, monkeypatch)
    r._propagate_outcome(RefreshOutcome.RATE_LIMITED)
    assert r._rate_limit_retry_task is None
    # Streak counter still advanced.
    assert r._consecutive_non_success == 1


# ── model_not_found fallback latch ──────────────────────────────────────


def test_looks_like_model_not_found_matches_canonical_signatures():
    canonicals = [
        '{"type":"not_found_error","message":"model: claude-haiku-4-5"}',
        "API Error: model not found",
        "API Error: model_not_found",
        "Invalid model 'claude-haiku-4-5'",
        "Model 'claude-haiku-4-5' does not exist",
        "Model claude-haiku-4-5 is not available",
    ]
    for s in canonicals:
        assert _looks_like_model_not_found("", s), f"missed canonical: {s!r}"
    # Generic "not found" elsewhere (e.g. message 404) must NOT match.
    benign = [
        "404 not found",
        "Message not found",
        "API Error: Request rejected (429)",  # rate-limit, not model
        "rate_limit_error",
        "",
    ]
    for s in benign:
        assert not _looks_like_model_not_found("", s), f"false positive: {s!r}"


def test_build_probe_cmd_includes_model_by_default(monkeypatch):
    monkeypatch.setattr(credential_refresh, "_probe_model_disabled", False)
    cmd = _build_probe_cmd()
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == REFRESH_PROBE_MODEL


def test_build_probe_cmd_drops_model_when_latched(monkeypatch):
    monkeypatch.setattr(credential_refresh, "_probe_model_disabled", True)
    cmd = _build_probe_cmd()
    assert "--model" not in cmd
    # Other args unaffected.
    assert "--dangerously-skip-permissions" in cmd
    assert "--print" in cmd


def test_filebackend_model_not_found_stderr_latches_probe(tmp_path, monkeypatch):
    # The first model_not_found in stderr must (a) set the module latch
    # so subsequent probes drop --model, AND (b) still classify the
    # outcome as FAILED (counts toward streak; next tick uses default
    # and should succeed, resetting the streak).
    monkeypatch.setattr(credential_refresh, "_probe_model_disabled", False)
    _write_creds(tmp_path, expires_in_seconds=5 * 60)
    backend = FileBackend(host_home=tmp_path)

    class _Proc:
        returncode = 1
        async def communicate(self):
            return (
                b"",
                b'API Error: {"type":"not_found_error","message":"model: claude-haiku-4-5 not found"}\n',
            )

    async def fake_exec(*argv, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    outcome = asyncio.run(backend.refresh())
    assert outcome is RefreshOutcome.FAILED
    assert credential_refresh._probe_model_disabled is True


def test_rate_limit_stderr_does_not_latch_probe(tmp_path, monkeypatch):
    # A rate-limit response must NOT latch model-disabled — the model
    # is fine, just throttled.
    monkeypatch.setattr(credential_refresh, "_probe_model_disabled", False)
    _write_creds(tmp_path, expires_in_seconds=5 * 60)
    backend = FileBackend(host_home=tmp_path)

    class _Proc:
        returncode = 1
        async def communicate(self):
            return b"", b"API Error: Request rejected (429)\n"

    async def fake_exec(*argv, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(backend.refresh())
    assert credential_refresh._probe_model_disabled is False


def test_probe_uses_default_model_after_latch_persists_across_calls(
    tmp_path, monkeypatch,
):
    # End-to-end through _tick: after the latch trips on call 1, call 2's
    # subprocess argv must not include --model.
    monkeypatch.setattr(credential_refresh, "_probe_model_disabled", False)
    _write_creds(tmp_path, expires_in_seconds=5 * 60)
    r = CredentialRefresher(host_home=tmp_path)

    call_count = {"n": 0}
    spawned_argvs: list = []

    class _ProcLatch:
        returncode = 1
        async def communicate(self):
            return b"", b"API Error: invalid model\n"

    class _ProcOk:
        returncode = 0
        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        spawned_argvs.append(list(argv))
        call_count["n"] += 1
        return _ProcLatch() if call_count["n"] == 1 else _ProcOk()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(r._tick())
    asyncio.run(r._tick())
    assert len(spawned_argvs) == 2
    assert "--model" in spawned_argvs[0]
    assert "--model" not in spawned_argvs[1]


def test_refresh_probe_model_honors_env_var_override(monkeypatch):
    # Reload the module with the env var set so the module-level
    # constant picks it up. Verifies the operator escape hatch works.
    import importlib
    monkeypatch.setenv("PUFFO_AGENT_REFRESH_MODEL", "claude-sonnet-4-6-fake")
    reloaded = importlib.reload(credential_refresh)
    try:
        assert reloaded.REFRESH_PROBE_MODEL == "claude-sonnet-4-6-fake"
    finally:
        # Restore the original module state for downstream tests.
        monkeypatch.delenv("PUFFO_AGENT_REFRESH_MODEL", raising=False)
        importlib.reload(credential_refresh)
