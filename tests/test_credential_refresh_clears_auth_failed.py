"""PUF-258: clear runtime.health=auth_failed on credential refresh-success."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import pytest

from puffo_agent.portal.credential_refresh import (
    REFRESH_SAFETY_MARGIN_SECONDS,
    CredentialRefresher,
    RefreshOutcome,
)
from puffo_agent.portal.worker import Worker


_LOG = logging.getLogger("test-puf-258")


# ── Helper-policy tests (Worker._clear_auth_failed_if_recoverable) ──────────


class _FakeRuntime:
    def __init__(self, health: str = "ok"):
        self.health = health
        self.error = ""
        self.saved_count = 0
        self.last_saved_agent: str | None = None

    def save(self, agent_id: str):
        self.saved_count += 1
        self.last_saved_agent = agent_id


def _call(runtime: _FakeRuntime) -> None:
    Worker._clear_auth_failed_if_recoverable(
        runtime, "agent-X", _LOG,  # type: ignore[arg-type]
    )


def test_clear_flips_auth_failed_to_ok():
    runtime = _FakeRuntime(health="auth_failed")
    runtime.error = "Worker emitted an auth-error string..."
    _call(runtime)
    assert runtime.health == "ok"
    assert runtime.error == ""
    assert runtime.saved_count == 1
    assert runtime.last_saved_agent == "agent-X"


def test_clear_no_op_on_ok():
    runtime = _FakeRuntime(health="ok")
    _call(runtime)
    assert runtime.health == "ok"
    assert runtime.saved_count == 0


def test_clear_leaves_api_error_abandoned_alone():
    # PUF-255's on_turn_success lane owns api_error_abandoned. PUF-258
    # must not over-step into it.
    runtime = _FakeRuntime(health="api_error_abandoned")
    runtime.error = "Worker abandoned a batch..."
    _call(runtime)
    assert runtime.health == "api_error_abandoned"
    assert runtime.error == "Worker abandoned a batch..."
    assert runtime.saved_count == 0


def test_clear_no_op_on_unknown():
    runtime = _FakeRuntime(health="unknown")
    _call(runtime)
    assert runtime.health == "unknown"
    assert runtime.saved_count == 0


def test_optimistic_clear_then_re_set_on_next_401():
    runtime = _FakeRuntime(health="auth_failed")
    _call(runtime)
    assert runtime.health == "ok"
    # Simulate worker's _handle_suppressed_reply re-flipping on next 401.
    runtime.health = "auth_failed"
    runtime.error = "fresh-401-after-refresh"
    _call(runtime)
    assert runtime.health == "ok"
    assert runtime.error == ""
    assert runtime.saved_count == 2


# ── CredentialRefresher wiring tests ────────────────────────────────────────


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


def test_register_and_unregister_on_refresh_success(tmp_path):
    _write_creds(tmp_path, expires_in_seconds=600)
    r = CredentialRefresher(host_home=tmp_path)

    calls: list[str] = []

    def cb() -> None:
        calls.append("fired")

    r.register_on_refresh_success(cb)
    r._fire_refresh_success()
    assert calls == ["fired"]

    r.unregister_on_refresh_success(cb)
    r._fire_refresh_success()
    assert calls == ["fired"]

    # Unregistering an already-removed callback is a no-op.
    r.unregister_on_refresh_success(cb)


def test_fire_refresh_success_dispatches_to_multiple_subscribers(tmp_path):
    _write_creds(tmp_path, expires_in_seconds=600)
    r = CredentialRefresher(host_home=tmp_path)

    fired: list[int] = []
    r.register_on_refresh_success(lambda: fired.append(1))
    r.register_on_refresh_success(lambda: fired.append(2))
    r.register_on_refresh_success(lambda: fired.append(3))
    r._fire_refresh_success()
    assert fired == [1, 2, 3]


def test_callback_exception_does_not_break_refresh_loop(tmp_path, caplog):
    _write_creds(tmp_path, expires_in_seconds=600)
    r = CredentialRefresher(host_home=tmp_path)

    fired: list[str] = []

    def good_cb() -> None:
        fired.append("good")

    def bad_cb() -> None:
        raise RuntimeError("subscriber boom")

    # Double-register good_cb to verify the loop continues PAST the bad one.
    r.register_on_refresh_success(good_cb)
    r.register_on_refresh_success(bad_cb)
    r.register_on_refresh_success(good_cb)
    with caplog.at_level(logging.WARNING, logger="puffo_agent"):
        r._fire_refresh_success()
    assert fired == ["good", "good"]
    assert any(
        "refresh-success callback raised" in rec.message
        for rec in caplog.records
    )


def test_fire_is_safe_under_concurrent_register(tmp_path):
    # Defensive copy via list(...) means a callback that registers
    # another callback mid-dispatch doesn't IndexError or cause the
    # new callback to fire in the same dispatch.
    _write_creds(tmp_path, expires_in_seconds=600)
    r = CredentialRefresher(host_home=tmp_path)

    fired: list[str] = []

    def first_cb() -> None:
        fired.append("first")
        r.register_on_refresh_success(lambda: fired.append("late"))

    r.register_on_refresh_success(first_cb)
    r._fire_refresh_success()
    # "late" not in fired — registered after dispatch copy was taken.
    assert fired == ["first"]
    # But it IS in the list for the next fire.
    r._fire_refresh_success()
    assert fired == ["first", "first", "late"]


@pytest.mark.asyncio
async def test_refresh_now_fires_on_success(tmp_path, monkeypatch):
    _write_creds(tmp_path, expires_in_seconds=10)
    r = CredentialRefresher(host_home=tmp_path)

    refresh_called = {"n": 0}

    async def fake_refresh() -> RefreshOutcome:
        refresh_called["n"] += 1
        _write_creds(tmp_path, expires_in_seconds=3600)
        return RefreshOutcome.REFRESHED

    monkeypatch.setattr(r.backend, "refresh", fake_refresh)

    fired: list[str] = []
    r.register_on_refresh_success(lambda: fired.append("ok"))

    await r._refresh_now(expires_in=10, by_agent=True)
    assert refresh_called["n"] == 1
    assert fired == ["ok"]


@pytest.mark.asyncio
async def test_refresh_now_does_not_fire_on_unchanged(tmp_path, monkeypatch):
    # PR #48 review: UNCHANGED (PUF-265 case) must not fire — would
    # oscillate auth_failed → ok → auth_failed each 2-min poll.
    _write_creds(tmp_path, expires_in_seconds=10)
    r = CredentialRefresher(host_home=tmp_path)

    async def unchanged_refresh() -> RefreshOutcome:
        return RefreshOutcome.UNCHANGED

    monkeypatch.setattr(r.backend, "refresh", unchanged_refresh)

    fired: list[str] = []
    r.register_on_refresh_success(lambda: fired.append("ok"))

    await r._refresh_now(expires_in=10, by_agent=True)
    assert fired == []


@pytest.mark.asyncio
async def test_refresh_now_does_not_fire_on_failed_outcome(tmp_path, monkeypatch):
    _write_creds(tmp_path, expires_in_seconds=10)
    r = CredentialRefresher(host_home=tmp_path)

    async def failed_refresh() -> RefreshOutcome:
        return RefreshOutcome.FAILED

    monkeypatch.setattr(r.backend, "refresh", failed_refresh)

    fired: list[str] = []
    r.register_on_refresh_success(lambda: fired.append("ok"))

    await r._refresh_now(expires_in=10, by_agent=True)
    assert fired == []


@pytest.mark.asyncio
async def test_refresh_now_does_not_fire_on_failure(tmp_path, monkeypatch):
    # backend.refresh raises (network down, subprocess crash, etc).
    _write_creds(tmp_path, expires_in_seconds=10)
    r = CredentialRefresher(host_home=tmp_path)

    async def failing_refresh() -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr(r.backend, "refresh", failing_refresh)

    fired: list[str] = []
    r.register_on_refresh_success(lambda: fired.append("ok"))

    await r._refresh_now(expires_in=10, by_agent=True)
    assert fired == []


@pytest.mark.asyncio
async def test_refresh_now_skip_branch_does_not_fire(tmp_path, monkeypatch):
    _write_creds(tmp_path, expires_in_seconds=REFRESH_SAFETY_MARGIN_SECONDS + 600)
    r = CredentialRefresher(host_home=tmp_path)

    refresh_called = {"n": 0}

    async def fake_refresh() -> None:
        refresh_called["n"] += 1

    monkeypatch.setattr(r.backend, "refresh", fake_refresh)

    fired: list[str] = []
    r.register_on_refresh_success(lambda: fired.append("ok"))

    await r._refresh_now(
        expires_in=REFRESH_SAFETY_MARGIN_SECONDS + 600,
        by_agent=False,
    )
    assert refresh_called["n"] == 0
    assert fired == []


@pytest.mark.asyncio
async def test_external_rotation_loop_fires_on_detected_rotation(
    tmp_path, monkeypatch,
):
    _write_creds(tmp_path, expires_in_seconds=600)
    r = CredentialRefresher(host_home=tmp_path)

    from puffo_agent.macos import keychain as _kc
    monkeypatch.setattr(_kc, "KEYCHAIN_POLL_INTERVAL_SECONDS", 0.01)

    poll_calls = {"n": 0}

    async def fake_poll() -> bool:
        poll_calls["n"] += 1
        return poll_calls["n"] == 1

    monkeypatch.setattr(
        r.backend, "poll_external_rotation", fake_poll, raising=False,
    )
    sync_calls = {"n": 0}
    monkeypatch.setattr(
        r, "_sync_views", lambda: sync_calls.__setitem__("n", sync_calls["n"] + 1),
    )

    fired: list[str] = []
    r.register_on_refresh_success(lambda: fired.append("ok"))

    stop = asyncio.Event()
    loop_task = asyncio.create_task(r._external_rotation_loop(stop))
    await asyncio.sleep(0.05)
    stop.set()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except asyncio.TimeoutError:
        loop_task.cancel()
        try:
            await loop_task
        except (asyncio.CancelledError, Exception):
            pass

    assert poll_calls["n"] >= 1
    assert sync_calls["n"] >= 1
    assert fired == ["ok"]


# ── Daemon-side wiring tests ────────────────────────────────────────────────


class _FakeAgentCfg:
    def __init__(self, agent_id: str, harness: str = "claude-code"):
        self.id = agent_id

        class _R:
            def __init__(self_inner, h):
                self_inner.harness = h

        self.runtime = _R(harness)


def _make_fake_worker():
    worker = type("FakeWorker", (), {})()
    worker.runtime = _FakeRuntime(health="auth_failed")
    worker.runtime.error = "pre-refresh auth error"
    return worker


def _make_daemon_stub(tmp_path):
    from puffo_agent.portal.daemon import Daemon

    daemon = Daemon.__new__(Daemon)
    _write_creds(tmp_path, expires_in_seconds=600)
    daemon.refresher = CredentialRefresher(host_home=tmp_path)
    daemon.codex_refresher = CredentialRefresher(host_home=tmp_path)
    daemon.workers = {}
    return daemon


def test_daemon_register_with_refresher_binds_callback_to_worker_runtime(tmp_path):
    daemon = _make_daemon_stub(tmp_path)
    agent_cfg = _FakeAgentCfg("agent-A", harness="claude-code")
    worker = _make_fake_worker()
    daemon.workers["agent-A"] = worker

    daemon._register_with_refresher(agent_cfg, worker)

    assert hasattr(worker, "_refresh_success_callback")
    assert len(daemon.refresher._on_refresh_success) == 1
    assert len(daemon.codex_refresher._on_refresh_success) == 0

    daemon.refresher._fire_refresh_success()
    assert worker.runtime.health == "ok"
    assert worker.runtime.error == ""


def test_daemon_register_codex_harness_routes_to_codex_refresher(tmp_path):
    daemon = _make_daemon_stub(tmp_path)
    agent_cfg = _FakeAgentCfg("agent-codex-A", harness="codex")
    worker = _make_fake_worker()
    daemon.workers["agent-codex-A"] = worker

    daemon._register_with_refresher(agent_cfg, worker)

    assert len(daemon.refresher._on_refresh_success) == 0
    assert len(daemon.codex_refresher._on_refresh_success) == 1


@pytest.mark.asyncio
async def test_daemon_stop_worker_unregisters_callback_from_both_refreshers(
    tmp_path,
):
    daemon = _make_daemon_stub(tmp_path)
    agent_cfg = _FakeAgentCfg("agent-A", harness="claude-code")
    worker = _make_fake_worker()

    async def fake_stop():
        return None
    worker.stop = fake_stop

    daemon.workers["agent-A"] = worker
    daemon._register_with_refresher(agent_cfg, worker)
    assert len(daemon.refresher._on_refresh_success) == 1

    await daemon._stop_worker("agent-A")
    assert len(daemon.refresher._on_refresh_success) == 0
    assert len(daemon.codex_refresher._on_refresh_success) == 0
    assert "agent-A" not in daemon.workers


@pytest.mark.asyncio
async def test_daemon_stop_worker_no_callback_does_not_crash(tmp_path):
    # Edge: worker was registered as agent but somehow lacks the callback
    # attribute (e.g., partial init failure). _stop_worker must not raise.
    daemon = _make_daemon_stub(tmp_path)
    worker = _make_fake_worker()

    async def fake_stop():
        return None
    worker.stop = fake_stop

    daemon.workers["agent-A"] = worker
    # No _register_with_refresher → no _refresh_success_callback attr.
    assert not hasattr(worker, "_refresh_success_callback")

    await daemon._stop_worker("agent-A")
    assert "agent-A" not in daemon.workers


def test_two_workers_both_get_auth_failed_cleared_on_one_refresh(tmp_path):
    # End-to-end fan-out: two workers registered on the same refresher
    # both clear their auth_failed flags from a single _fire_refresh_success.
    daemon = _make_daemon_stub(tmp_path)
    worker_a = _make_fake_worker()
    worker_b = _make_fake_worker()
    daemon.workers["agent-A"] = worker_a
    daemon.workers["agent-B"] = worker_b

    daemon._register_with_refresher(
        _FakeAgentCfg("agent-A", harness="claude-code"), worker_a,
    )
    daemon._register_with_refresher(
        _FakeAgentCfg("agent-B", harness="claude-code"), worker_b,
    )

    assert worker_a.runtime.health == "auth_failed"
    assert worker_b.runtime.health == "auth_failed"

    daemon.refresher._fire_refresh_success()

    assert worker_a.runtime.health == "ok"
    assert worker_b.runtime.health == "ok"
