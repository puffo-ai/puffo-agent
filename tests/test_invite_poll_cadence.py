"""PUF-272: two-phase ``_invite_poll_loop`` cadence.

10s for the first 5 min of the agent's life (durable
``AgentConfig.created_at``), then 30s steady. Legacy agents with
``created_at == 0`` stay on 30s.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


def _make_client(*, agent_created_at: int) -> PuffoCoreMessageClient:
    """Skip the heavy KeyStore / HttpClient / MessageStore construction
    via MagicMock — ``_next_invite_poll_interval`` only reads
    ``self._agent_created_at`` and ``_invite_poll_loop`` only calls
    ``self._poll_pending_invites`` (also mockable)."""
    return PuffoCoreMessageClient(
        slug="agent-test-1234",
        device_id="dev-1",
        space_id="",
        keystore=MagicMock(),
        http_client=MagicMock(),
        message_store=MagicMock(),
        agent_created_at=agent_created_at,
    )


# ── _next_invite_poll_interval (pure) ────────────────────────────


def test_legacy_zero_created_at_always_steady():
    """Agents written before PUF-272 have ``created_at == 0``. Don't
    surprise the fleet with extra traffic — fall through to 30s."""
    client = _make_client(agent_created_at=0)
    assert client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 30


def test_negative_created_at_treated_as_legacy(monkeypatch):
    """Defensive: malformed agent.yml (e.g., negative timestamp)
    should not be interpreted as "infinity-fast-phase"."""
    client = _make_client(agent_created_at=-1)
    assert client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 30


def test_fast_phase_inside_window(monkeypatch):
    import time

    client = _make_client(agent_created_at=1_000_000)
    # 30s after creation — well inside the 300s fast phase.
    monkeypatch.setattr(time, "time", lambda: 1_000_030.0)
    assert client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 10


def test_fast_phase_at_zero_age(monkeypatch):
    """Agent created exactly now → fast phase."""
    import time

    client = _make_client(agent_created_at=1_000_000)
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    assert client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 10


def test_steady_phase_exactly_at_boundary(monkeypatch):
    """At age == fast_phase_seconds, cadence transitions to steady."""
    import time

    client = _make_client(agent_created_at=1_000_000)
    monkeypatch.setattr(time, "time", lambda: 1_000_300.0)
    assert client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 30


def test_steady_phase_long_after_creation(monkeypatch):
    """Agent created hours ago → steady straight away."""
    import time

    client = _make_client(agent_created_at=1_000_000)
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0 + 3_600.0)
    assert client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 30


def test_clock_skew_negative_age_treated_as_fast(monkeypatch):
    """``time.time()`` running behind agent's recorded creation — e.g.,
    NTP correction landed mid-window. Age becomes negative; still
    inside the fast phase (since negative < 300)."""
    import time

    client = _make_client(agent_created_at=1_000_000)
    monkeypatch.setattr(time, "time", lambda: 999_990.0)
    assert client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 10


# ── _invite_poll_loop (integration) ──────────────────────────────


@pytest.mark.asyncio
async def test_loop_uses_fast_interval_for_new_agent(monkeypatch):
    import time

    # Anchor the wall clock 60s after creation — inside fast phase.
    client = _make_client(agent_created_at=1_000_000)
    monkeypatch.setattr(time, "time", lambda: 1_000_060.0)

    poll_calls: list[int] = []
    client._poll_pending_invites = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda: poll_calls.append(1),
    )

    sleeps: list[float] = []
    original_sleep = asyncio.sleep

    async def capture_sleep(delay: float, *args, **kwargs):
        sleeps.append(delay)
        # After 4 captured sleeps (grace + 3 polls), cancel.
        if len(sleeps) >= 4:
            raise asyncio.CancelledError
        # Yield to the event loop without actually waiting.
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)

    await client._invite_poll_loop()

    # First sleep is the 2s startup grace, then 10s fast cadence.
    assert sleeps[0] == 2
    assert sleeps[1] == 10
    assert sleeps[2] == 10
    # Polls fired once per loop iteration (3 successful polls before
    # the cancellation on the 4th sleep).
    assert len(poll_calls) == 3


@pytest.mark.asyncio
async def test_loop_uses_steady_interval_for_old_agent(monkeypatch):
    import time

    # Agent is 10 minutes old — past the 5-minute fast phase.
    client = _make_client(agent_created_at=1_000_000)
    monkeypatch.setattr(time, "time", lambda: 1_000_600.0)

    client._poll_pending_invites = AsyncMock()  # type: ignore[method-assign]

    sleeps: list[float] = []
    original_sleep = asyncio.sleep

    async def capture_sleep(delay: float, *args, **kwargs):
        sleeps.append(delay)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)

    await client._invite_poll_loop()

    assert sleeps[0] == 2
    assert sleeps[1] == 30
    assert sleeps[2] == 30


@pytest.mark.asyncio
async def test_loop_transitions_at_boundary(monkeypatch):
    """As wall-clock time advances past the fast-phase boundary
    mid-loop, the next sleep flips from 10s to 30s."""
    import time

    client = _make_client(agent_created_at=1_000_000)

    # First two polls are inside fast phase, then time jumps past 300s.
    times = iter([
        1_000_010.0,  # interval pick 1 (fast)
        1_000_020.0,  # interval pick 2 (fast)
        1_000_350.0,  # interval pick 3 (steady — past boundary)
    ])
    monkeypatch.setattr(time, "time", lambda: next(times))

    client._poll_pending_invites = AsyncMock()  # type: ignore[method-assign]

    sleeps: list[float] = []
    original_sleep = asyncio.sleep

    async def capture_sleep(delay: float, *args, **kwargs):
        sleeps.append(delay)
        if len(sleeps) >= 4:  # grace + 3 picks
            raise asyncio.CancelledError
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)

    await client._invite_poll_loop()

    assert sleeps == [2, 10, 10, 30]


@pytest.mark.asyncio
async def test_loop_legacy_zero_created_at_steady_from_start(monkeypatch):
    """Confirms the fleet-wide-defensive default: an agent that
    pre-dates the ``created_at`` field gets steady 30s, not fast 10s."""
    client = _make_client(agent_created_at=0)
    client._poll_pending_invites = AsyncMock()  # type: ignore[method-assign]

    sleeps: list[float] = []
    original_sleep = asyncio.sleep

    async def capture_sleep(delay: float, *args, **kwargs):
        sleeps.append(delay)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)

    await client._invite_poll_loop()

    assert sleeps == [2, 30, 30]


@pytest.mark.asyncio
async def test_loop_cancellation_returns_cleanly(monkeypatch):
    """Cancellation mid-grace (before the first poll) is swallowed,
    same as the existing behavior."""
    client = _make_client(agent_created_at=1_000_000)
    client._poll_pending_invites = AsyncMock()  # type: ignore[method-assign]

    async def cancel_during_grace(delay: float, *args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", cancel_during_grace)

    await client._invite_poll_loop()
    assert client._poll_pending_invites.call_count == 0
