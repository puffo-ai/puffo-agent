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
