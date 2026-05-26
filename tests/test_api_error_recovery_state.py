"""PUF-255 (recovery-clear matched-pair for PUF-252).

PUF-252 (PR #45) shipped the ENTER-error hook
``on_api_error_abandon`` that flips
``runtime.health = "api_error_abandoned"`` on kick-retry exhaustion.
PUF-255 ships the symmetric EXIT-error hook ``on_turn_success``
fired on every successful turn completion (both fresh-dispatch and
kick-retry-recovery paths). The worker's callback clears the
abandoned state back to ``"ok"`` so puffo-server learns when the
agent has healed -- without this, PUF-252's state-honesty is
one-way and the server permanently believes the agent is broken.

Tests mirror PUF-252's ``test_api_error_abandon_state.py`` shape
for symmetry + add the bidirectional cycle test Solution flagged
at PR #45 QA gap-3 + the ticket's validation-plan "synthetic edge."
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.agent.core import AgentAPIError


def _make_client(max_retries: int = 1) -> PuffoCoreMessageClient:
    """Bare client harness — bypass ``__init__`` so we don't need
    keystore / identity / WS. Only the fields ``_fire_turn_success``
    + ``_do_api_error_retries`` actually touch are stubbed."""
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "tester-1234"
    client._log = logging.getLogger("test-puf-255")
    client.MAX_API_ERROR_RETRIES = max_retries  # type: ignore[attr-defined]

    class _StubStore:
        async def mark_thread_processed(self, *args, **kwargs):
            return None

    client.store = _StubStore()  # type: ignore[assignment]
    return client


def _make_entry():
    class _Entry:
        dispatching_ids: set[str] = set()

    return _Entry()


@pytest.mark.asyncio
async def test_recovery_callback_fires_on_kick_retry_success():
    """When a kick-retry succeeds, the recovery callback fires
    alongside ``mark_thread_processed``. Mirrors operator's
    verbatim scenario: agent rate-limited → new message arrives →
    kick-retry recovers → recovery callback fires."""
    client = _make_client(max_retries=2)

    retry_calls = {"n": 0}

    async def kick_first_fails_then_succeeds(root_id, batch, channel_meta):
        retry_calls["n"] += 1
        if retry_calls["n"] == 1:
            raise AgentAPIError("still rate-limited")
        return None

    success_calls: list[tuple[Any, ...]] = []

    async def on_success(root_id, batch, channel_meta):
        success_calls.append((root_id, list(batch), dict(channel_meta)))

    real_sleep = asyncio.sleep
    asyncio.sleep = (lambda _s: real_sleep(0))  # type: ignore[assignment]
    try:
        await client._do_api_error_retries(  # type: ignore[arg-type]
            root_id="root_rec",
            entry=_make_entry(),  # type: ignore[arg-type]
            batch=[{"envelope_id": "env_1", "sent_at": 100}],
            channel_meta={"channel_id": "ch_x"},
            on_api_error_retry=kick_first_fails_then_succeeds,
            on_api_error_abandon=None,
            on_turn_success=on_success,
            last_envelope="env_1",
        )
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]

    assert len(success_calls) == 1
    root_id, batch, channel_meta = success_calls[0]
    assert root_id == "root_rec"
    assert batch == [{"envelope_id": "env_1", "sent_at": 100}]
    assert channel_meta == {"channel_id": "ch_x"}


@pytest.mark.asyncio
async def test_recovery_callback_does_not_fire_on_kick_retry_exhaustion():
    """The recovery callback is for the SUCCESS path. When every
    kick-retry fails and the batch is abandoned, recovery doesn't
    fire (the abandon callback does — that's PUF-252's lane)."""
    client = _make_client(max_retries=1)

    async def always_fail(root_id, batch, channel_meta):
        raise AgentAPIError("still rate-limited")

    success_calls: list[tuple[Any, ...]] = []

    async def on_success(root_id, batch, channel_meta):
        success_calls.append((root_id,))

    real_sleep = asyncio.sleep
    asyncio.sleep = (lambda _s: real_sleep(0))  # type: ignore[assignment]
    try:
        await client._do_api_error_retries(  # type: ignore[arg-type]
            root_id="root_exhaust",
            entry=_make_entry(),  # type: ignore[arg-type]
            batch=[{"envelope_id": "env_1"}],
            channel_meta={},
            on_api_error_retry=always_fail,
            on_api_error_abandon=None,
            on_turn_success=on_success,
            last_envelope="env_1",
        )
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]

    assert success_calls == []


@pytest.mark.asyncio
async def test_recovery_callback_exception_does_not_propagate():
    """Observational hook — if the callback raises, the turn
    itself stands. Same robustness invariant as
    ``_fire_api_error_abandon``."""
    client = _make_client(max_retries=1)

    async def kick_succeeds(root_id, batch, channel_meta):
        return None

    async def on_success_raises(root_id, batch, channel_meta):
        raise RuntimeError("callback boom")

    real_sleep = asyncio.sleep
    asyncio.sleep = (lambda _s: real_sleep(0))  # type: ignore[assignment]
    try:
        # Should NOT raise.
        await client._do_api_error_retries(  # type: ignore[arg-type]
            root_id="root_swallow",
            entry=_make_entry(),  # type: ignore[arg-type]
            batch=[{"envelope_id": "env_1"}],
            channel_meta={},
            on_api_error_retry=kick_succeeds,
            on_api_error_abandon=None,
            on_turn_success=on_success_raises,
            last_envelope="env_1",
        )
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_recovery_callback_omitted_does_not_break_backwards_compat():
    """Pre-PUF-255 callers don't pass ``on_turn_success``. The
    kick-recovery path should silently no-op the callback fire."""
    client = _make_client(max_retries=1)

    async def kick_succeeds(root_id, batch, channel_meta):
        return None

    real_sleep = asyncio.sleep
    asyncio.sleep = (lambda _s: real_sleep(0))  # type: ignore[assignment]
    try:
        # Should NOT raise.
        await client._do_api_error_retries(  # type: ignore[arg-type]
            root_id="root_noop",
            entry=_make_entry(),  # type: ignore[arg-type]
            batch=[{"envelope_id": "env_1"}],
            channel_meta={},
            on_api_error_retry=kick_succeeds,
            on_api_error_abandon=None,
            on_turn_success=None,
            last_envelope="env_1",
        )
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_fire_turn_success_passes_batch_and_meta_unchanged():
    """The helper just relays the tuple to the callback — no
    mutation, no filtering. Pin so a future refactor that adds
    filtering at the helper layer surfaces immediately."""
    client = _make_client()

    captured: list[Any] = []

    async def on_success(root_id, batch, channel_meta):
        captured.append((root_id, batch, channel_meta))

    sample_batch = [{"envelope_id": "env_a"}, {"envelope_id": "env_b"}]
    sample_meta = {"channel_id": "ch_q", "channel_name": "qa"}
    await client._fire_turn_success(  # type: ignore[arg-type]
        on_turn_success=on_success,
        root_id="root_relay",
        batch=sample_batch,
        channel_meta=sample_meta,
    )

    assert len(captured) == 1
    root_id, batch, channel_meta = captured[0]
    assert root_id == "root_relay"
    # Identity NOT preserved is the right contract (callers can
    # mutate freely); but content should match.
    assert batch == sample_batch
    assert channel_meta == sample_meta


# ── Worker callback unit tests ──────────────────────────────────────────────
#
# The worker defines ``on_turn_success`` as a local closure inside
# ``Worker.run()``. To test it standalone we replicate the closure's
# behavior via a tiny fake worker. Tests pin the
# "clears api_error_abandoned, leaves auth_failed alone, no-op on ok"
# contract that the comment block in worker.py documents.


class _FakeRuntime:
    """In-memory stand-in for ``RuntimeState`` -- the worker's
    closure only touches ``.health`` + ``.error`` + ``.save(agent_id)``."""

    def __init__(self, health: str = "ok"):
        self.health = health
        self.error = ""
        self.saved_count = 0

    def save(self, _agent_id: str):
        self.saved_count += 1


def _make_worker_recovery_callback(runtime: _FakeRuntime):
    """Replicates the worker's ``on_turn_success`` closure shape
    so we can test it without standing up the full Worker."""

    async def on_turn_success(root_id, batch, channel_meta):
        if runtime.health != "api_error_abandoned":
            return
        runtime.health = "ok"
        runtime.error = ""
        runtime.save("agent-X")

    return on_turn_success


@pytest.mark.asyncio
async def test_worker_callback_clears_api_error_abandoned_to_ok():
    """Operator's verbatim scenario: agent in
    ``api_error_abandoned`` → new turn succeeds → health flips to
    ``ok`` + error cleared + runtime saved (so puffo-server sees the
    transition via the existing heartbeat reporter)."""
    runtime = _FakeRuntime(health="api_error_abandoned")
    runtime.error = "Worker abandoned a batch ..."
    callback = _make_worker_recovery_callback(runtime)
    await callback("root_x", [], {})
    assert runtime.health == "ok"
    assert runtime.error == ""
    assert runtime.saved_count == 1


@pytest.mark.asyncio
async def test_worker_callback_no_op_on_ok():
    """Steady-state hot-path: most turns happen while
    ``runtime.health == "ok"`` already. The callback should NOT
    write to runtime each turn -- skip the save."""
    runtime = _FakeRuntime(health="ok")
    callback = _make_worker_recovery_callback(runtime)
    await callback("root_x", [], {})
    assert runtime.health == "ok"
    assert runtime.saved_count == 0


@pytest.mark.asyncio
async def test_worker_callback_leaves_auth_failed_alone():
    """PUF-221's CredentialRefresher owns the ``auth_failed``
    lifecycle. PUF-255's recovery hook is for the api-error class
    only -- don't accidentally clear a 401 just because a turn
    succeeded (the auth failure may persist after a single lucky
    success); leave it to the refresh-success-ping."""
    runtime = _FakeRuntime(health="auth_failed")
    runtime.error = "auth-error"
    callback = _make_worker_recovery_callback(runtime)
    await callback("root_x", [], {})
    assert runtime.health == "auth_failed"
    assert runtime.error == "auth-error"
    assert runtime.saved_count == 0


@pytest.mark.asyncio
async def test_worker_callback_no_op_on_unknown():
    """Boot-time / uninstrumented state. No turn-success transition
    is meaningful from ``unknown`` -- the worker's other hooks
    (health-ping, etc.) own that bootstrap transition."""
    runtime = _FakeRuntime(health="unknown")
    callback = _make_worker_recovery_callback(runtime)
    await callback("root_x", [], {})
    assert runtime.health == "unknown"
    assert runtime.saved_count == 0


@pytest.mark.asyncio
async def test_bidirectional_state_cycle():
    """The matched-pair regression seal Solution flagged at PR #45
    QA gap-3 + the ticket's validation-plan "synthetic edge":
    error → recovery → error → recovery cycles correctly with the
    last-write-wins semantics implied by ``runtime.save`` per
    transition."""
    runtime = _FakeRuntime(health="ok")
    recovery_callback = _make_worker_recovery_callback(runtime)

    # Simulate PUF-252's enter-error path setting state.
    def enter_error(reason: str):
        runtime.health = "api_error_abandoned"
        runtime.error = reason
        runtime.save("agent-X")

    # Cycle 1: enter error, recover, verify
    enter_error("first abandon")
    assert runtime.health == "api_error_abandoned"
    await recovery_callback("root_1", [], {})
    assert runtime.health == "ok"
    assert runtime.error == ""

    # Cycle 2: enter error again, recover again
    enter_error("second abandon")
    assert runtime.health == "api_error_abandoned"
    assert runtime.error == "second abandon"
    await recovery_callback("root_2", [], {})
    assert runtime.health == "ok"
    assert runtime.error == ""

    # Sanity: saves fired 4 times (2 enters + 2 recoveries).
    assert runtime.saved_count == 4
