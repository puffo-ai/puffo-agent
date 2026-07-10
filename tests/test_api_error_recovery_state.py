"""PUF-255: regression coverage for ``on_turn_success`` recovery-clear."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from puffo_agent.agent.puffo_core_client import (
    DEFAULT_MAX_INPUT_BYTES,
    PuffoCoreMessageClient,
)
from puffo_agent.agent.core import AgentAPIError
from puffo_agent.portal.worker import Worker


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Per-test ``asyncio.sleep`` patch via monkeypatch."""
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)


def _make_client(max_retries: int = 1) -> PuffoCoreMessageClient:
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "tester-1234"
    client._log = logging.getLogger("test-puf-255")
    client.MAX_API_ERROR_RETRIES = max_retries  # type: ignore[attr-defined]
    # Greedy-fill reads this budget; large enough to never split here.
    client._queue_seq = 0
    client._max_input_bytes = DEFAULT_MAX_INPUT_BYTES

    class _StubStore:
        async def mark_thread_processed(self, *args, **kwargs):
            return None

    client.store = _StubStore()  # type: ignore[assignment]
    return client


def _make_entry():
    class _Entry:
        def __init__(self):
            self.dispatching_ids: set[str] = set()

    return _Entry()


@pytest.mark.asyncio
async def test_recovery_callback_fires_on_kick_retry_success():
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
    """``caplog`` pins that the swallow is logged via ``log.exception``."""
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
    """Pin so a future refactor that adds filtering at the helper
    layer surfaces immediately."""
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
    assert batch == sample_batch
    assert channel_meta == sample_meta


@pytest.mark.asyncio
async def test_fresh_dispatch_success_path_fires_recovery_callback():
    """Seals the ``_consume_queue`` success branch (every other
    test exercises only the kick-retry-recovery path)."""
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


# ── Worker callback unit tests (drive the lifted staticmethod) ───


class _FakeRuntime:
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
    runtime = _FakeRuntime(health="api_error_abandoned")
    runtime.error = "Worker abandoned a batch ..."
    _call_recovery_helper(runtime)
    assert runtime.health == "ok"
    assert runtime.error == ""
    assert runtime.saved_count == 1
    assert runtime.last_saved_agent == "agent-X"


def test_worker_callback_no_op_on_ok():
    """Steady-state hot path skips the runtime.save."""
    runtime = _FakeRuntime(health="ok")
    _call_recovery_helper(runtime)
    assert runtime.health == "ok"
    assert runtime.saved_count == 0


def test_worker_callback_leaves_auth_failed_alone():
    """auth_failed belongs to the CredentialRefresher lane."""
    runtime = _FakeRuntime(health="auth_failed")
    runtime.error = "auth-error"
    _call_recovery_helper(runtime)
    assert runtime.health == "auth_failed"
    assert runtime.error == "auth-error"
    assert runtime.saved_count == 0


def test_worker_callback_no_op_on_unknown():
    runtime = _FakeRuntime(health="unknown")
    _call_recovery_helper(runtime)
    assert runtime.health == "unknown"
    assert runtime.saved_count == 0


def test_bidirectional_state_cycle():
    """Regression seal: error -> recovery -> error -> recovery cycles."""
    runtime = _FakeRuntime(health="ok")

    def enter_error(reason: str):
        runtime.health = "api_error_abandoned"
        runtime.error = reason
        runtime.save("agent-X")

    enter_error("first abandon")
    assert runtime.health == "api_error_abandoned"
    _call_recovery_helper(runtime, root_id="root_1")
    assert runtime.health == "ok"
    assert runtime.error == ""

    enter_error("second abandon")
    assert runtime.health == "api_error_abandoned"
    assert runtime.error == "second abandon"
    _call_recovery_helper(runtime, root_id="root_2")
    assert runtime.health == "ok"
    assert runtime.error == ""

    assert runtime.saved_count == 4
