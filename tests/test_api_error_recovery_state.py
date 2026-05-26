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
at PR #45 QA gap-3 + the ticket's validation-plan "synthetic edge".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.agent.core import AgentAPIError
from puffo_agent.portal.worker import Worker


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Mirror PR #45 polish: monkeypatch ``asyncio.sleep`` for the
    test's lifetime via pytest's fixture so the patch is per-test
    and reverts cleanly. Process-global ``asyncio.sleep = ...``
    assignment + try/finally was brittle under pytest-xdist /
    concurrent event-loop tests."""
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)


def _make_client(max_retries: int = 1) -> PuffoCoreMessageClient:
    """Bare client harness -- bypass ``__init__`` so we don't need
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
    """Tiny stand-in for ``_ThreadEntry``. Only ``dispatching_ids``
    is touched by ``_do_api_error_retries`` (cleared on entry).
    Initialised in ``__init__`` so each entry owns its own set --
    mirrors PR #45 polish on the analogous PUF-252 test fixture."""

    class _Entry:
        def __init__(self):
            self.dispatching_ids: set[str] = set()

    return _Entry()


@pytest.mark.asyncio
async def test_recovery_callback_fires_on_kick_retry_success():
    """When a kick-retry succeeds, the recovery callback fires
    alongside ``mark_thread_processed``. Mirrors operator's
    verbatim scenario: agent rate-limited -> new message arrives ->
    kick-retry recovers -> recovery callback fires."""
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

    assert len(success_calls) == 1
    root_id, batch, channel_meta = success_calls[0]
    assert root_id == "root_rec"
    assert batch == [{"envelope_id": "env_1", "sent_at": 100}]
    assert channel_meta == {"channel_id": "ch_x"}


@pytest.mark.asyncio
async def test_recovery_callback_does_not_fire_on_kick_retry_exhaustion():
    """The recovery callback is for the SUCCESS path. When every
    kick-retry fails and the batch is abandoned, recovery doesn't
    fire (the abandon callback does -- that's PUF-252's lane)."""
    client = _make_client(max_retries=1)

    async def always_fail(root_id, batch, channel_meta):
        raise AgentAPIError("still rate-limited")

    success_calls: list[tuple[Any, ...]] = []

    async def on_success(root_id, batch, channel_meta):
        success_calls.append((root_id,))

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

    assert success_calls == []


@pytest.mark.asyncio
async def test_recovery_callback_exception_does_not_propagate(caplog):
    """Observational hook -- if the callback raises, the turn
    itself stands. Same robustness invariant as
    ``_fire_api_error_abandon``. ``caplog`` pins that the swallow
    is logged via ``log.exception`` so a future regression that
    silently passes instead of logging breaks the test."""
    client = _make_client(max_retries=1)

    async def kick_succeeds(root_id, batch, channel_meta):
        return None

    async def on_success_raises(root_id, batch, channel_meta):
        raise RuntimeError("callback boom")

    with caplog.at_level(logging.ERROR, logger="test-puf-255"):
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

    matching = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and r.exc_info is not None
    ]
    assert matching, (
        "expected log.exception() to fire when the recovery callback "
        "raised; caplog saw no ERROR record with exc_info"
    )


@pytest.mark.asyncio
async def test_recovery_callback_omitted_does_not_break_backwards_compat():
    """Pre-PUF-255 callers don't pass ``on_turn_success``. The
    kick-recovery path should silently no-op the callback fire."""
    client = _make_client(max_retries=1)

    async def kick_succeeds(root_id, batch, channel_meta):
        return None

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


@pytest.mark.asyncio
async def test_fire_turn_success_passes_batch_and_meta_unchanged():
    """The helper just relays the tuple to the callback -- no
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


@pytest.mark.asyncio
async def test_fresh_dispatch_success_path_fires_recovery_callback():
    """The fresh-dispatch happy path through ``_consume_queue``
    fires ``_fire_turn_success`` after ``mark_thread_processed``.

    Without this test, removing the ``_fire_turn_success(...)``
    call at the success branch of ``_consume_queue`` (line ~1334)
    would slip past CI -- every other test exercises the
    kick-retry-recovery path via ``_do_api_error_retries``. This
    test stands up a minimal queue + thread_state + store mock and
    drives one happy-path turn through the consumer.
    """
    from puffo_agent.agent.puffo_core_client import _ThreadEntry

    client = _make_client(max_retries=1)
    client._queue = asyncio.PriorityQueue()
    client._thread_state = {}

    root_id = "root_fresh"
    batch = [{"envelope_id": "env_1", "sent_at": 100}]
    entry = _ThreadEntry(
        messages=list(batch),
        channel_meta={"channel_id": "ch_x"},
        current_priority=0,
        current_seq=1,
        in_queue=True,
    )
    client._thread_state[root_id] = entry
    await client._queue.put((0, 1, root_id))

    async def on_message_batch(_root_id, _batch, _meta):
        return None

    success_calls: list[tuple[Any, ...]] = []
    done = asyncio.Event()

    async def on_turn_success(_root_id, _batch, _meta):
        success_calls.append((_root_id, list(_batch), dict(_meta)))
        done.set()

    task = asyncio.create_task(
        client._consume_queue(  # type: ignore[arg-type]
            on_message_batch=on_message_batch,
            on_turn_success=on_turn_success,
        )
    )
    try:
        await asyncio.wait_for(done.wait(), timeout=2.0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(success_calls) == 1
    fired_root, fired_batch, fired_meta = success_calls[0]
    assert fired_root == root_id
    assert fired_batch == batch
    assert fired_meta == {"channel_id": "ch_x"}


# ── Worker callback unit tests ──────────────────────────────────────────────
#
# Tests call ``Worker._clear_api_error_abandoned_if_recoverable``
# directly (the lifted staticmethod) so they pin the production
# function's contract -- a change to the closure body actually
# breaks the test rather than drifting against a re-implemented
# stub. This is per operator's PR #46 review item 3 (path b
# refactor).


class _FakeRuntime:
    """In-memory stand-in for ``RuntimeState`` -- the recovery
    helper only touches ``.health`` + ``.error`` + ``.save(agent_id)``."""

    def __init__(self, health: str = "ok"):
        self.health = health
        self.error = ""
        self.saved_count = 0
        self.last_saved_agent: str | None = None

    def save(self, agent_id: str):
        self.saved_count += 1
        self.last_saved_agent = agent_id


_WORKER_LOG = logging.getLogger("test-puf-255-worker")


def _call_recovery_helper(runtime: _FakeRuntime, root_id: str = "root_x") -> None:
    Worker._clear_api_error_abandoned_if_recoverable(
        runtime, "agent-X", root_id, _WORKER_LOG,  # type: ignore[arg-type]
    )


def test_worker_callback_clears_api_error_abandoned_to_ok():
    """Operator's verbatim scenario: agent in
    ``api_error_abandoned`` -> new turn succeeds -> health flips to
    ``ok`` + error cleared + runtime saved."""
    runtime = _FakeRuntime(health="api_error_abandoned")
    runtime.error = "Worker abandoned a batch ..."
    _call_recovery_helper(runtime)
    assert runtime.health == "ok"
    assert runtime.error == ""
    assert runtime.saved_count == 1
    assert runtime.last_saved_agent == "agent-X"


def test_worker_callback_no_op_on_ok():
    """Steady-state hot-path: most turns happen while
    ``runtime.health == "ok"`` already. The helper should NOT
    write to runtime each turn -- skip the save."""
    runtime = _FakeRuntime(health="ok")
    _call_recovery_helper(runtime)
    assert runtime.health == "ok"
    assert runtime.saved_count == 0


def test_worker_callback_leaves_auth_failed_alone():
    """PUF-221's CredentialRefresher owns the ``auth_failed``
    lifecycle. PUF-255's recovery hook is for the api-error class
    only -- don't accidentally clear a 401 just because a turn
    succeeded; leave it to the refresh-success-ping."""
    runtime = _FakeRuntime(health="auth_failed")
    runtime.error = "auth-error"
    _call_recovery_helper(runtime)
    assert runtime.health == "auth_failed"
    assert runtime.error == "auth-error"
    assert runtime.saved_count == 0


def test_worker_callback_no_op_on_unknown():
    """Boot-time / uninstrumented state. No turn-success transition
    is meaningful from ``unknown`` -- the worker's other hooks own
    that bootstrap transition."""
    runtime = _FakeRuntime(health="unknown")
    _call_recovery_helper(runtime)
    assert runtime.health == "unknown"
    assert runtime.saved_count == 0


def test_bidirectional_state_cycle():
    """The matched-pair regression seal Solution flagged at PR #45
    QA gap-3 + the ticket's validation-plan "synthetic edge":
    error -> recovery -> error -> recovery cycles correctly with
    the last-write-wins semantics implied by ``runtime.save`` per
    transition."""
    runtime = _FakeRuntime(health="ok")

    # Simulate PUF-252's enter-error path setting state.
    def enter_error(reason: str):
        runtime.health = "api_error_abandoned"
        runtime.error = reason
        runtime.save("agent-X")

    # Cycle 1: enter error, recover, verify
    enter_error("first abandon")
    assert runtime.health == "api_error_abandoned"
    _call_recovery_helper(runtime, root_id="root_1")
    assert runtime.health == "ok"
    assert runtime.error == ""

    # Cycle 2: enter error again, recover again
    enter_error("second abandon")
    assert runtime.health == "api_error_abandoned"
    assert runtime.error == "second abandon"
    _call_recovery_helper(runtime, root_id="root_2")
    assert runtime.health == "ok"
    assert runtime.error == ""

    # Sanity: saves fired 4 times (2 enters + 2 recoveries).
    assert runtime.saved_count == 4
