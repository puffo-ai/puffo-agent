"""PUF-252 bug-1: state-honesty hook on kick-retry exhaustion.

The pre-fix `_do_api_error_retries` exhausted up to
`MAX_API_ERROR_RETRIES = 3` kicks and then logged + returned --
``runtime.health`` + ``runtime.error`` were NEVER updated, so Sam's
Scout looked ``state=running`` while the consumer had silently
abandoned a batch. PUF-252 adds an `on_api_error_abandon` callback
that fires exactly once per abandoned batch with
``(root_id, batch, channel_meta, attempts)``; the worker uses it to
flip ``runtime.health = "api_error_abandoned"`` so bug-2's
designer-blocked UI affordance has a signal to render.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.agent.core import AgentAPIError


def _make_client(max_retries: int = 3) -> PuffoCoreMessageClient:
    """Bare client harness mirroring tests/test_invite_dedup_persistence.py.

    We bypass ``__init__`` so we don't need a real keystore/identity/
    WS connection. The fields ``_do_api_error_retries`` reaches
    (``_log``, ``MAX_API_ERROR_RETRIES``, ``store``) are stubbed.
    """
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "tester-1234"
    client._log = logging.getLogger("test-puf-252")
    # Override the class-level constant on the instance so the loop
    # runs in a tractable bound for tests.
    client.MAX_API_ERROR_RETRIES = max_retries  # type: ignore[attr-defined]

    class _StubStore:
        async def mark_thread_processed(self, *args, **kwargs):
            return None

    client.store = _StubStore()  # type: ignore[assignment]
    return client


def _make_entry():
    """Tiny stand-in for ``_ThreadEntry``. Only ``dispatching_ids``
    is touched by ``_do_api_error_retries`` (cleared on entry)."""

    class _Entry:
        dispatching_ids: set[str] = set()

    return _Entry()


@pytest.mark.asyncio
async def test_abandon_fires_after_max_retries_exhausted_with_all_failing():
    """All retries raise AgentAPIError -> loop exhausts -> abandon
    callback fires exactly once with ``attempts == MAX_API_ERROR_RETRIES``."""
    client = _make_client(max_retries=2)

    async def always_fail(root_id, batch, channel_meta):
        raise AgentAPIError("still rate-limited")

    abandon_calls: list[tuple[Any, ...]] = []

    async def on_abandon(root_id, batch, channel_meta, attempts):
        abandon_calls.append((root_id, list(batch), dict(channel_meta), attempts))

    # asyncio.sleep is mocked via monkeypatch to keep the test fast.
    import asyncio
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    asyncio.sleep = fast_sleep  # type: ignore[assignment]
    try:
        await client._do_api_error_retries(  # type: ignore[arg-type]
            root_id="root_x",
            entry=_make_entry(),  # type: ignore[arg-type]
            batch=[{"envelope_id": "env_1", "sent_at": 100}],
            channel_meta={"channel_id": "ch_x"},
            on_api_error_retry=always_fail,
            on_api_error_abandon=on_abandon,
            last_envelope="env_1",
        )
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]

    assert len(abandon_calls) == 1
    root_id, batch, channel_meta, attempts = abandon_calls[0]
    assert root_id == "root_x"
    assert batch == [{"envelope_id": "env_1", "sent_at": 100}]
    assert channel_meta == {"channel_id": "ch_x"}
    assert attempts == 2  # == MAX_API_ERROR_RETRIES


@pytest.mark.asyncio
async def test_abandon_does_not_fire_when_retry_callback_is_none():
    """When ``on_api_error_retry`` is None we abandon immediately
    (the existing pre-PUF-252 short-circuit). The new callback still
    fires so the worker hears about the abandon."""
    client = _make_client()

    abandon_calls: list[tuple[Any, ...]] = []

    async def on_abandon(root_id, batch, channel_meta, attempts):
        abandon_calls.append((root_id, attempts))

    await client._do_api_error_retries(  # type: ignore[arg-type]
        root_id="root_no_retry",
        entry=_make_entry(),  # type: ignore[arg-type]
        batch=[{"envelope_id": "env_1"}],
        channel_meta={},
        on_api_error_retry=None,
        on_api_error_abandon=on_abandon,
        last_envelope="env_1",
    )

    assert len(abandon_calls) == 1
    root_id, attempts = abandon_calls[0]
    assert root_id == "root_no_retry"
    assert attempts == 0  # no kicks fired before abandon


@pytest.mark.asyncio
async def test_abandon_fires_when_internal_raise_short_circuits_loop():
    """If the retry callback raises something other than
    AgentAPIError (e.g. a programming error), the loop short-circuits
    and abandons. The abandon callback still fires with the attempt
    number that just raised."""
    client = _make_client(max_retries=3)

    async def boom(root_id, batch, channel_meta):
        raise RuntimeError("test boom")

    abandon_calls: list[tuple[Any, ...]] = []

    async def on_abandon(root_id, batch, channel_meta, attempts):
        abandon_calls.append((root_id, attempts))

    import asyncio
    real_sleep = asyncio.sleep
    asyncio.sleep = (lambda _s: real_sleep(0))  # type: ignore[assignment]
    try:
        await client._do_api_error_retries(  # type: ignore[arg-type]
            root_id="root_boom",
            entry=_make_entry(),  # type: ignore[arg-type]
            batch=[{"envelope_id": "env_1"}],
            channel_meta={},
            on_api_error_retry=boom,
            on_api_error_abandon=on_abandon,
            last_envelope="env_1",
        )
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]

    # First attempt raised RuntimeError -> the except-Exception branch
    # fires the abandon callback with attempts=1 then returns.
    assert len(abandon_calls) == 1
    root_id, attempts = abandon_calls[0]
    assert root_id == "root_boom"
    assert attempts == 1


@pytest.mark.asyncio
async def test_abandon_does_not_fire_when_a_kick_succeeds():
    """The kick-retry succeeded -> no abandon. Same path that
    pre-PUF-252 worked correctly; we're checking we didn't break it."""
    client = _make_client(max_retries=3)

    call_count = {"n": 0}

    async def succeed_on_first_try(root_id, batch, channel_meta):
        call_count["n"] += 1
        # No raise == success
        return None

    abandon_calls: list[tuple[Any, ...]] = []

    async def on_abandon(root_id, batch, channel_meta, attempts):
        abandon_calls.append((root_id, attempts))

    import asyncio
    real_sleep = asyncio.sleep
    asyncio.sleep = (lambda _s: real_sleep(0))  # type: ignore[assignment]
    try:
        await client._do_api_error_retries(  # type: ignore[arg-type]
            root_id="root_recovered",
            entry=_make_entry(),  # type: ignore[arg-type]
            batch=[{"envelope_id": "env_1", "sent_at": 200}],
            channel_meta={},
            on_api_error_retry=succeed_on_first_try,
            on_api_error_abandon=on_abandon,
            last_envelope="env_1",
        )
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]

    assert call_count["n"] == 1
    assert abandon_calls == []  # success path doesn't fire abandon


@pytest.mark.asyncio
async def test_abandon_callback_exception_does_not_propagate():
    """The callback is observational; if it raises, the abandon
    itself still stands and the caller doesn't see the exception.
    Keeps the worker's bookkeeping from cascading into a listen()
    crash."""
    client = _make_client(max_retries=1)

    async def always_fail(root_id, batch, channel_meta):
        raise AgentAPIError("still rate-limited")

    async def callback_raises(root_id, batch, channel_meta, attempts):
        raise RuntimeError("callback boom")

    import asyncio
    real_sleep = asyncio.sleep
    asyncio.sleep = (lambda _s: real_sleep(0))  # type: ignore[assignment]
    try:
        # Should NOT raise -- the helper catches callback exceptions.
        await client._do_api_error_retries(  # type: ignore[arg-type]
            root_id="root_swallow",
            entry=_make_entry(),  # type: ignore[arg-type]
            batch=[{"envelope_id": "env_1"}],
            channel_meta={},
            on_api_error_retry=always_fail,
            on_api_error_abandon=callback_raises,
            last_envelope="env_1",
        )
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_abandon_callback_omitted_does_not_break_existing_callers():
    """Backwards-compat seal: pre-PUF-252 callers don't pass
    ``on_api_error_abandon``. The exhaustion path should silently
    no-op the callback fire."""
    client = _make_client(max_retries=1)

    async def always_fail(root_id, batch, channel_meta):
        raise AgentAPIError("still rate-limited")

    import asyncio
    real_sleep = asyncio.sleep
    asyncio.sleep = (lambda _s: real_sleep(0))  # type: ignore[assignment]
    try:
        # Should NOT raise. The pre-PUF-252 signature only took
        # ``on_api_error_retry``; the new param defaults to None.
        await client._do_api_error_retries(  # type: ignore[arg-type]
            root_id="root_noop",
            entry=_make_entry(),  # type: ignore[arg-type]
            batch=[{"envelope_id": "env_1"}],
            channel_meta={},
            on_api_error_retry=always_fail,
            on_api_error_abandon=None,
            last_envelope="env_1",
        )
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
