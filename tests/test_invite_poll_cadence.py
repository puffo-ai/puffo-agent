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


# ── PR #60 round-1: 5 coverage gaps Solution flagged ─────────────


def test_exact_boundary_at_300s_falls_to_steady(monkeypatch):
    """Pin the ``< vs ≤`` choice. Operator's "first 5 minutes" maps
    to the half-open interval ``[0, 300)``: at age == 300 the agent
    has just finished its 5th minute, so the next poll uses the
    steady cadence. Locks the intentional ``<`` so a future PR that
    flips to ``<=`` (extending fast phase by one tick) does so
    explicitly."""
    import time

    client = _make_client(agent_created_at=1_000_000)
    # age = exactly 300.0 — pins the strict-less-than boundary.
    monkeypatch.setattr(time, "time", lambda: 1_000_300.0)
    assert client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 30
    # age = 299.999 — last microsecond of fast phase.
    monkeypatch.setattr(time, "time", lambda: 1_000_299.999)
    assert client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 10


def test_uses_wall_clock_not_monotonic(monkeypatch):
    """The helper deliberately uses ``time.time()`` (wall clock),
    NOT ``time.monotonic()``. Wall clock matches the agent.yml
    ``created_at`` semantics (Unix seconds — also wall clock).
    A future PR swapping to monotonic would silently shift the
    age-zero reference to "daemon startup" rather than "agent
    creation". This test pins the intent."""
    import time

    client = _make_client(agent_created_at=1_000_000)
    time_calls = {"time": 0, "monotonic": 0}

    def fake_time() -> float:
        time_calls["time"] += 1
        return 1_000_060.0

    def fake_monotonic() -> float:
        time_calls["monotonic"] += 1
        return 0.0

    monkeypatch.setattr(time, "time", fake_time)
    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    )
    assert time_calls["time"] >= 1, "_next_invite_poll_interval must call time.time()"
    assert time_calls["monotonic"] == 0, "must NOT use time.monotonic()"


def test_wall_clock_backward_jump_stays_in_fast_phase(monkeypatch):
    """Contract corner case: NTP correction lands during fast phase
    and shifts ``time.time()`` backward by 60s. The age becomes
    smaller, so the agent stays in fast phase — and may stay longer
    than 5 wall-clock minutes total. Operator spec didn't request
    monotonic semantics; this test documents that the wall-clock
    drift is acceptable. Switching to ``time.monotonic()`` would
    flip this and require breaking the test deliberately."""
    import time

    client = _make_client(agent_created_at=1_000_000)
    # First tick: age = 250s, fast.
    monkeypatch.setattr(time, "time", lambda: 1_000_250.0)
    assert client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 10
    # NTP correction lands — wall clock jumps back 60s. Age = 190s,
    # still fast.
    monkeypatch.setattr(time, "time", lambda: 1_000_190.0)
    assert client._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 10


def test_multi_agent_clients_compute_independent_intervals(monkeypatch):
    """Two PuffoCoreMessageClient instances on the same daemon at
    different ages should compute different intervals concurrently.
    Each instance owns its own ``_agent_created_at`` — no module-
    level state to leak."""
    import time

    young = _make_client(agent_created_at=1_000_000)
    old = _make_client(agent_created_at=999_000)
    legacy = _make_client(agent_created_at=0)
    # Wall clock: young is 30s old, old is ~16 min old, legacy is N/A.
    monkeypatch.setattr(time, "time", lambda: 1_000_030.0)
    assert young._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 10
    assert old._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 30
    assert legacy._next_invite_poll_interval(
        fast=10, steady=30, fast_phase_seconds=300,
    ) == 30


def test_build_puffo_core_client_threads_agent_created_at(monkeypatch, tmp_path):
    """End-to-end wiring: AgentConfig.created_at → _build_puffo_core_client
    → PuffoCoreMessageClient.__init__ → self._agent_created_at. Locks
    the worker.py threading so a future refactor that drops the kwarg
    fails here loudly."""
    from puffo_agent.portal import worker
    from puffo_agent.portal.state import AgentConfig, PuffoCoreConfig, RuntimeConfig

    cfg = AgentConfig(
        id="agent-test-1234",
        puffo_core=PuffoCoreConfig(
            server_url="https://example.test",
            slug="agent-test-1234",
            device_id="dev-1",
            space_id="",
            operator_slug="",
        ),
        runtime=RuntimeConfig(
            kind="chat-local",
            harness="claude-code",
        ),
        created_at=1_700_000_000,
    )

    # Stub the heavy side-effects: identity import + KeyStore +
    # MessageStore. We only care about which kwargs the constructor
    # receives.
    monkeypatch.setattr(
        worker, "_ensure_agent_identity_imported", lambda *_a, **_k: None,
    )
    captured: dict[str, object] = {}

    class DummyClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "puffo_agent.agent.puffo_core_client.PuffoCoreMessageClient",
        DummyClient,
    )
    monkeypatch.setattr(
        "puffo_agent.crypto.keystore.KeyStore",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        "puffo_agent.crypto.http_client.PuffoCoreHttpClient",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        "puffo_agent.agent.message_store.MessageStore",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        AgentConfig, "resolve_workspace_dir", lambda self: tmp_path,
    )

    worker._build_puffo_core_client(cfg, "agent-test-1234")

    assert captured.get("agent_created_at") == 1_700_000_000, (
        "AgentConfig.created_at must thread through to "
        "PuffoCoreMessageClient(agent_created_at=...) "
        f"— got {captured.get('agent_created_at')!r}"
    )
