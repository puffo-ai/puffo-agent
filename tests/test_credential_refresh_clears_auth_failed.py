"""PUF-258: auth_failed sticky state-machine fix.

PUF-252 + PUF-255 established the symmetric ENTER/EXIT pattern for
``runtime.health = "api_error_abandoned"`` (PUF-252 sets on
kick-retry exhaustion; PUF-255 clears on next successful turn).
PUF-258 extends the same pattern to ``runtime.health = "auth_failed"``:
the daemon's ``CredentialRefresher`` fires registered
``on_refresh_success`` callbacks after a successful refresh OR
external rotation, and ``Worker._clear_auth_failed_if_recoverable``
flips the flag back to ``"ok"``.

Tests cover (1) the helper's policy contract directly + (2) the
CredentialRefresher's register/fire/unregister wiring.
"""

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
)
from puffo_agent.portal.worker import Worker


_LOG = logging.getLogger("test-puf-258")


# ── Helper-policy tests (Worker._clear_auth_failed_if_recoverable) ──────────


class _FakeRuntime:
    """Stand-in for ``RuntimeState`` -- the helper only touches
    ``.health`` + ``.error`` + ``.save(agent_id)``."""

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
    """Operator's scenario: agent in ``auth_failed`` -> credential
    refresh succeeds -> health flips to ``ok`` + error cleared +
    runtime saved (so puffo-server sees the transition via the
    existing heartbeat reporter / next status read)."""
    runtime = _FakeRuntime(health="auth_failed")
    runtime.error = "Worker emitted an auth-error string..."
    _call(runtime)
    assert runtime.health == "ok"
    assert runtime.error == ""
    assert runtime.saved_count == 1
    assert runtime.last_saved_agent == "agent-X"


def test_clear_no_op_on_ok():
    """Steady-state hot-path: most refresh cycles happen while
    ``runtime.health == "ok"`` already (regular token rotations
    pre-expiry). The helper should NOT write to runtime each
    cycle -- skip the save."""
    runtime = _FakeRuntime(health="ok")
    _call(runtime)
    assert runtime.health == "ok"
    assert runtime.saved_count == 0


def test_clear_leaves_api_error_abandoned_alone():
    """PUF-255's ``on_turn_success`` lane owns the
    ``api_error_abandoned`` lifecycle. PUF-258's refresh-success
    hook is for the auth class only -- don't accidentally clear an
    api-error-abandoned just because credentials rotated (the
    abandoned batch is still stuck until a fresh turn succeeds);
    leave it to the turn-success callback. Symmetric partition to
    PUF-255's leaves-auth-failed-alone."""
    runtime = _FakeRuntime(health="api_error_abandoned")
    runtime.error = "Worker abandoned a batch..."
    _call(runtime)
    assert runtime.health == "api_error_abandoned"
    assert runtime.error == "Worker abandoned a batch..."
    assert runtime.saved_count == 0


def test_clear_no_op_on_unknown():
    """Boot-time / uninstrumented state. No refresh-success
    transition is meaningful from ``unknown`` -- bootstrap probe
    will set health to ok on its own once the auth-check runs."""
    runtime = _FakeRuntime(health="unknown")
    _call(runtime)
    assert runtime.health == "unknown"
    assert runtime.saved_count == 0


def test_optimistic_clear_then_re_set_on_next_401():
    """Optimistic-clear semantics: if the refresh succeeds but the
    agent's next request still 401s (e.g., upstream revocation
    beyond the refresh's scope), the auth_failed flag re-sets when
    ``_handle_suppressed_reply`` fires next time. Pin via a
    sequencing test on the runtime alone -- the
    re-set is owned by worker code we don't touch in PUF-258."""
    runtime = _FakeRuntime(health="auth_failed")
    _call(runtime)
    assert runtime.health == "ok"
    # Simulate the worker's next auth-error leak detection re-setting
    # the flag (this is the existing PUF-214 + PUF-221 path).
    runtime.health = "auth_failed"
    runtime.error = "fresh-401-after-refresh"
    # The next refresh-success cycle would clear it again.
    _call(runtime)
    assert runtime.health == "ok"
    assert runtime.error == ""
    assert runtime.saved_count == 2  # initial clear + post-re-set clear


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
    """``register_on_refresh_success`` adds the callback;
    ``unregister_on_refresh_success`` removes it. Both are idempotent
    on unknown identities (mirrors ``register_agent`` shape)."""
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
    assert calls == ["fired"]  # no further fires after unregister

    # Unregistering an already-removed callback is a no-op.
    r.unregister_on_refresh_success(cb)


def test_fire_refresh_success_dispatches_to_multiple_subscribers(tmp_path):
    """Multiple workers registered: all get the callback. Order
    matches registration order (list semantics)."""
    _write_creds(tmp_path, expires_in_seconds=600)
    r = CredentialRefresher(host_home=tmp_path)

    fired: list[int] = []
    r.register_on_refresh_success(lambda: fired.append(1))
    r.register_on_refresh_success(lambda: fired.append(2))
    r.register_on_refresh_success(lambda: fired.append(3))
    r._fire_refresh_success()
    assert fired == [1, 2, 3]


def test_callback_exception_does_not_break_refresh_loop(tmp_path, caplog):
    """A buggy subscriber raising must not cascade into the refresh
    loop. The exception is caught + logged at WARNING."""
    _write_creds(tmp_path, expires_in_seconds=600)
    r = CredentialRefresher(host_home=tmp_path)

    fired: list[str] = []

    def good_cb() -> None:
        fired.append("good")

    def bad_cb() -> None:
        raise RuntimeError("subscriber boom")

    r.register_on_refresh_success(good_cb)
    r.register_on_refresh_success(bad_cb)
    r.register_on_refresh_success(good_cb)  # double-register for the
                                            # post-bad ordering check
    with caplog.at_level(logging.WARNING, logger="puffo_agent"):
        r._fire_refresh_success()
    # Good callback fires before AND after the bad one -- exception
    # didn't short-circuit the loop.
    assert fired == ["good", "good"]
    # And the warning was logged.
    assert any(
        "refresh-success callback raised" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_refresh_now_fires_on_success(tmp_path, monkeypatch):
    """End-to-end: ``_refresh_now`` -> backend.refresh succeeds ->
    callbacks fire after the lock is released. Pins the wiring
    between the rotation success path and the new event."""
    _write_creds(tmp_path, expires_in_seconds=10)
    r = CredentialRefresher(host_home=tmp_path)

    refresh_called = {"n": 0}

    async def fake_refresh() -> None:
        refresh_called["n"] += 1
        # Simulate the backend writing a fresh expiry.
        _write_creds(tmp_path, expires_in_seconds=3600)

    monkeypatch.setattr(r.backend, "refresh", fake_refresh)

    fired: list[str] = []
    r.register_on_refresh_success(lambda: fired.append("ok"))

    await r._refresh_now(expires_in=10, by_agent=True)
    assert refresh_called["n"] == 1
    assert fired == ["ok"]


@pytest.mark.asyncio
async def test_refresh_now_does_not_fire_on_failure(tmp_path, monkeypatch):
    """If ``backend.refresh`` raises, no callbacks fire -- the
    auth_failed flag rightly persists until the next successful
    refresh attempt."""
    _write_creds(tmp_path, expires_in_seconds=10)
    r = CredentialRefresher(host_home=tmp_path)

    async def failing_refresh() -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr(r.backend, "refresh", failing_refresh)

    fired: list[str] = []
    r.register_on_refresh_success(lambda: fired.append("ok"))

    # Should NOT raise -- _refresh_now catches the exception.
    await r._refresh_now(expires_in=10, by_agent=True)
    assert fired == []


@pytest.mark.asyncio
async def test_refresh_now_skip_branch_does_not_fire(tmp_path, monkeypatch):
    """Non-agent-triggered refresh that finds ``before`` still
    within the safety margin returns early without rotating
    credentials. No callbacks fire (the credentials didn't actually
    change)."""
    # Plenty of TTL so the skip-branch fires.
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
    """When ``backend.poll_external_rotation`` returns True (Keychain
    rotated externally), the loop fires ``_fire_refresh_success`` after
    syncing views. Pins the second ``_fire_refresh_success`` site so a
    future regression that drops it from the rotated-branch breaks the
    test rather than silently regressing the macOS Keychain-rotation
    path."""
    _write_creds(tmp_path, expires_in_seconds=600)
    r = CredentialRefresher(host_home=tmp_path)

    # Shrink the poll interval so wait_for times out promptly. The
    # constant lives in ``..macos.keychain`` and is imported lazily
    # inside ``_external_rotation_loop`` -- patch at the module path
    # the loop reads from.
    from puffo_agent.macos import keychain as _kc
    monkeypatch.setattr(_kc, "KEYCHAIN_POLL_INTERVAL_SECONDS", 0.01)

    # Stub poll_external_rotation to return True once then False.
    poll_calls = {"n": 0}

    async def fake_poll() -> bool:
        poll_calls["n"] += 1
        return poll_calls["n"] == 1

    # FileBackend doesn't natively have poll_external_rotation (it's
    # KeychainBackend-only); attach it for the test via raising=False.
    monkeypatch.setattr(
        r.backend, "poll_external_rotation", fake_poll, raising=False,
    )
    # Stub _sync_views to a no-op (the disk-mirror logic isn't what
    # we're pinning here -- the fire-after-sync ordering is).
    sync_calls = {"n": 0}
    monkeypatch.setattr(
        r, "_sync_views", lambda: sync_calls.__setitem__("n", sync_calls["n"] + 1),
    )

    fired: list[str] = []
    r.register_on_refresh_success(lambda: fired.append("ok"))

    stop = asyncio.Event()
    loop_task = asyncio.create_task(r._external_rotation_loop(stop))
    # Let the loop iterate at least once on the rotated-branch.
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


# ── Daemon-side wiring tests (register/unregister roundtrip) ────────────────


class _FakeAgentCfg:
    """Minimal stand-in for ``AgentConfig`` -- daemon's
    ``_refresher_for`` reads ``runtime.harness`` to route to the
    claude vs codex refresher."""

    def __init__(self, agent_id: str, harness: str = "claude-code"):
        self.id = agent_id

        class _R:
            def __init__(self_inner, h):
                self_inner.harness = h

        self.runtime = _R(harness)


def _make_fake_worker():
    """Stand-in Worker -- only the ``runtime`` attribute is touched
    when the bound callback fires. ``_clear_auth_failed_if_recoverable``
    accepts a fake runtime since it's a staticmethod."""
    worker = type("FakeWorker", (), {})()
    worker.runtime = _FakeRuntime(health="auth_failed")
    worker.runtime.error = "pre-refresh auth error"
    return worker


def _make_daemon_stub(tmp_path):
    """Build a Daemon-shaped stub by bypassing ``__init__`` (which
    reads ``Path.home()`` + builds real backends) and stitching in
    minimal attributes the wiring methods touch."""
    from puffo_agent.portal.daemon import Daemon

    daemon = Daemon.__new__(Daemon)
    _write_creds(tmp_path, expires_in_seconds=600)
    daemon.refresher = CredentialRefresher(host_home=tmp_path)
    daemon.codex_refresher = CredentialRefresher(host_home=tmp_path)
    daemon.workers = {}
    return daemon


def test_daemon_register_with_refresher_binds_callback_to_worker_runtime(tmp_path):
    """``_register_with_refresher`` registers an
    ``on_refresh_success`` callback that, when fired, clears the
    Worker's ``runtime.health = "auth_failed"`` flag. Pins the
    binding so a future refactor that forgets to wire
    worker.runtime breaks the test."""
    daemon = _make_daemon_stub(tmp_path)
    agent_cfg = _FakeAgentCfg("agent-A", harness="claude-code")
    worker = _make_fake_worker()
    daemon.workers["agent-A"] = worker

    daemon._register_with_refresher(agent_cfg, worker)

    # Callback stashed on worker for unregister symmetry.
    assert hasattr(worker, "_refresh_success_callback")
    # Callback registered on the claude refresher (not codex).
    assert len(daemon.refresher._on_refresh_success) == 1
    assert len(daemon.codex_refresher._on_refresh_success) == 0

    # Firing it clears the worker's auth_failed flag.
    daemon.refresher._fire_refresh_success()
    assert worker.runtime.health == "ok"
    assert worker.runtime.error == ""


def test_daemon_register_codex_harness_routes_to_codex_refresher(tmp_path):
    """Codex-harness agents route to ``codex_refresher`` per
    ``_refresher_for``'s branch. Pins the codex side has independent
    callback registration -- if a refactor accidentally always uses
    ``self.refresher``, this breaks."""
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
    """``_stop_worker`` unregisters the on_refresh_success callback
    from both claude AND codex refreshers (idempotent on both, same
    pattern as ``unregister_agent``). This defends the cross-refresher
    cleanup against a future single-side regression."""
    daemon = _make_daemon_stub(tmp_path)
    agent_cfg = _FakeAgentCfg("agent-A", harness="claude-code")
    worker = _make_fake_worker()

    # Hand-build the worker.stop coroutine so _stop_worker can await it.
    async def fake_stop():
        return None
    worker.stop = fake_stop

    daemon.workers["agent-A"] = worker
    daemon._register_with_refresher(agent_cfg, worker)
    assert len(daemon.refresher._on_refresh_success) == 1

    await daemon._stop_worker("agent-A")
    # Callback removed from claude refresher.
    assert len(daemon.refresher._on_refresh_success) == 0
    # Codex refresher unchanged (was already empty, unregister is no-op).
    assert len(daemon.codex_refresher._on_refresh_success) == 0
    # Worker popped from registry.
    assert "agent-A" not in daemon.workers
