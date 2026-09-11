"""MCP transport wedge fixes (2026-09-03 incident): the generation
handshake probe, the hello RPC route, per-spawn config generations, and
refresh flags surviving a failed reload."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from puffo_agent.agent.harness.driver import (
    ProtocolDiagnostics,
    RuntimeLifecycle,
    RuntimeOpened,
    RuntimeRef,
    RuntimeSpec,
    SessionRef,
)
from puffo_agent.agent.harness.drivers.codex import CODEX_CAPABILITIES
from puffo_agent.agent.harness.runtime.runtime_manager import RuntimeManager
from puffo_agent.portal import rpc_service
from puffo_agent.portal.state import (
    AgentConfig,
    DaemonConfig,
    PuffoCoreConfig,
    RuntimeConfig,
    RuntimeState,
)
from puffo_agent.portal.worker import (
    Worker,
    _REFRESH_RELOAD_FAILURE_CAP,
    _process_refresh_flags,
)


def _run(coro):
    return asyncio.run(coro)


# ── refresh flags survive a failed reload ──────────────────────────────


class _FailingAdapter:
    async def reload(self, new_system_prompt, *, with_session=False):
        raise RuntimeError("cannot reload while a turn is active")


def _flags_kwargs(tmp_path: Path, adapter) -> dict:
    return dict(
        agent_id="t",
        harness_name="claude-code",
        shared_path=tmp_path / "shared",
        profile_path=str(tmp_path / "profile.md"),
        memory_path=str(tmp_path / "memory"),
        workspace_path=str(tmp_path),
        puffo=SimpleNamespace(system_prompt="p"),
        adapter=adapter,
        refresh_agent_flag=tmp_path / "refresh_agent.flag",
        refresh_host_sync_flag=tmp_path / "refresh_host_sync.flag",
        refresh_session_flag=tmp_path / "refresh_session.flag",
        refresh_provider_auth_flag=tmp_path / "refresh_provider_auth.flag",
    )


def test_failed_reload_keeps_scheduled_flag_byte_for_byte(tmp_path, monkeypatch):
    """A failed reload must not consume the refresh intent: the flag
    files survive unmodified (their content may carry daemon-owned
    scheduling fields) and the call reports failure."""
    from puffo_agent.portal import worker as worker_mod

    provider_flag = tmp_path / "refresh_provider_auth.flag"
    payload = (
        '{"source":"credential_replaced",'
        '"not_before_unix_ms":500}'
    )
    provider_flag.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(worker_mod.time, "time", lambda: 1.0)

    ok = _run(_process_refresh_flags(**_flags_kwargs(tmp_path, _FailingAdapter())))

    assert ok is False
    assert provider_flag.read_text(encoding="utf-8") == payload


def _seed_worker(health: str = "ok") -> Worker:
    worker = object.__new__(Worker)
    worker.runtime = RuntimeState(status="running", health=health)
    worker._turn_active = False
    worker._mcp_probe_strikes = 0
    worker._refresh_reload_failures = 0
    worker._adapter = None
    return worker


@pytest.fixture
def saved_states(monkeypatch):
    saves: list[tuple[str, str, str]] = []
    def _save(self, agent_id):
        saves.append((agent_id, self.health, self.error))
    monkeypatch.setattr(RuntimeState, "save", _save)
    return saves


def test_reload_failure_cap_abandons_flags_into_health(tmp_path, saved_states):
    worker = _seed_worker()
    flags = tuple(
        tmp_path / name
        for name in ("a.flag", "b.flag", "c.flag", "d.flag")
    )
    for flag in flags:
        flag.write_text("{}", encoding="utf-8")

    for _ in range(_REFRESH_RELOAD_FAILURE_CAP - 1):
        worker._note_refresh_reload(False, flags, "t")
    assert all(flag.exists() for flag in flags)
    assert worker.runtime.health == "ok"

    worker._note_refresh_reload(False, flags, "t")
    assert not any(flag.exists() for flag in flags)
    assert worker.runtime.health == "provider_error"
    assert "reload" in worker.runtime.error
    assert saved_states

    # A success resets the streak so unrelated later failures re-count.
    worker._refresh_reload_failures = 1
    worker._note_refresh_reload(True, flags, "t")
    assert worker._refresh_reload_failures == 0


# ── generation handshake probe ─────────────────────────────────────────


class _FakeManager:
    def __init__(
        self,
        generation: str,
        opened_at: float,
        lifecycle=RuntimeLifecycle.PERSISTENT_CHILD,
        opened: bool = True,
    ):
        self.spec = SimpleNamespace(
            mcp_generation=generation, system_prompt="p",
        )
        self.last_open_monotonic = opened_at
        self._lifecycle = lifecycle
        # Mirrors ``RuntimeManager.opened``: set only after a successful
        # ``driver.open`` and cleared by every close/reload path. It is the
        # only "did it open" signal — see ``current_capabilities`` below.
        self.opened = object() if opened else None

    def current_capabilities(self):
        # Deliberately answers even when ``opened`` is None: every shipped
        # driver returns a capability object unconditionally (module
        # constants on pi/codex, a constructor-time value on acp, a freshly
        # built one on claude). A double that returned None here would let
        # the probe pass a check no real manager ever fails.
        if self._lifecycle is None:
            return None
        return SimpleNamespace(lifecycle=self._lifecycle)


class _RecyclingAdapter:
    """Mimics the adapter-level reload chain the probe must use: the
    spec_reloader rebuilds the spec (minting a fresh generation) and the
    reopen stamps a new open watermark."""

    def __init__(self, mgr: _FakeManager):
        self.mgr = mgr
        self.reload_calls: list[bool] = []

    async def reload(self, new_system_prompt, *, with_session=False):
        self.reload_calls.append(with_session)
        self.mgr.spec = SimpleNamespace(
            mcp_generation=uuid.uuid4().hex,
            system_prompt=new_system_prompt,
        )
        self.mgr.last_open_monotonic = time.monotonic()
        self.mgr.opened = object()


def _wire(worker: Worker, mgr: _FakeManager) -> _RecyclingAdapter:
    adapter = _RecyclingAdapter(mgr)
    worker._adapter = adapter
    return adapter


@pytest.fixture
def registered_manager(monkeypatch):
    def _register(manager):
        import puffo_agent.agent.harness.runtime.runtime_manager as rm

        monkeypatch.setattr(rm, "get_runtime_manager", lambda _aid: manager)
        return manager
    yield _register
    rpc_service.clear_mcp_hello("t")


def test_probe_recycles_then_flips_health(registered_manager, saved_states):
    """No hello past the grace window → one recycle; still none → the
    wedge becomes a visible health state instead of 51 silent minutes."""
    mgr = registered_manager(_FakeManager("g1", time.monotonic() - 120))
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker()
    adapter = _wire(worker, mgr)

    _run(worker.probe_mcp_transport("t"))
    assert adapter.reload_calls == [False]
    assert worker._mcp_probe_strikes == 1
    assert worker.runtime.health == "ok"

    # Freshly recycled: within grace, the probe must not double-punish.
    _run(worker.probe_mcp_transport("t"))
    assert adapter.reload_calls == [False]

    mgr.last_open_monotonic = time.monotonic() - 120
    _run(worker.probe_mcp_transport("t"))
    assert worker.runtime.health == "mcp_unreachable"
    assert ("t", "mcp_unreachable", worker.runtime.error) in saved_states


def test_a_per_turn_harness_is_never_wedged_by_this_probe(
    registered_manager, saved_states,
):
    """A generation is not a promise of a *live* subprocess everywhere.

    A ``PER_TURN_CHILD`` driver takes ``open`` as a logical session and
    spawns on ``start_turn``, so between turns nothing exists to hello with.
    The probe also stands down during a turn, so for such a driver it would
    run only when its premise is false — every idle opencode agent went
    ``mcp_unreachable`` within a minute of start, with no fault injected.
    """
    mgr = registered_manager(
        _FakeManager(
            "g1", time.monotonic() - 600,
            lifecycle=RuntimeLifecycle.PER_TURN_CHILD,
        )
    )
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker("ok")
    adapter = _wire(worker, mgr)

    for _ in range(3):
        _run(worker.probe_mcp_transport("t"))

    assert adapter.reload_calls == []
    assert worker._mcp_probe_strikes == 0
    assert worker.runtime.health == "ok"
    assert saved_states == []


def test_a_failed_open_is_not_a_wedge_though_capabilities_still_answer(
    registered_manager, saved_states,
):
    """Capabilities cannot stand in for "did it open".

    ``last_open_monotonic`` is stamped *before* ``driver.open`` is awaited,
    so a failed open leaves a watermark behind. Capabilities are no help
    either: every shipped driver returns an object unconditionally, so they
    are present even when the open raised. ``mgr.opened`` is the only fact
    set after a successful open, and this fixture is the real shape —
    capabilities answer, ``opened`` does not.
    """
    mgr = registered_manager(
        _FakeManager("g1", time.monotonic() - 600, opened=False)
    )
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker("ok")
    adapter = _wire(worker, mgr)

    assert mgr.current_capabilities() is not None

    for _ in range(3):
        _run(worker.probe_mcp_transport("t"))

    assert adapter.reload_calls == []
    assert worker._mcp_probe_strikes == 0
    assert worker.runtime.health == "ok"


def test_a_closed_runtime_holds_the_strike_count_instead_of_clearing_it(
    registered_manager, saved_states,
):
    """Not-open is momentary; not-covered is standing. Only the second
    withdraws.

    A reload clears ``opened`` while it runs. If a probe landing in that
    window reset the strike count, the escalation would restart on every
    recycle: strike 1 -> recycle -> probe mid-reload zeroes it -> strike 1
    again, and ``mcp_unreachable`` could never be reached however long the
    transport stayed dead.
    """
    mgr = registered_manager(
        _FakeManager("g1", time.monotonic() - 600, opened=False)
    )
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker("mcp_unreachable")
    worker._mcp_probe_strikes = 1
    adapter = _wire(worker, mgr)

    _run(worker.probe_mcp_transport("t"))

    assert worker._mcp_probe_strikes == 1
    assert worker.runtime.health == "mcp_unreachable"
    assert adapter.reload_calls == []


def test_no_shipped_driver_reports_capabilities_only_after_opening():
    """The contract the probe must not lean on, read off the real drivers.

    Each of these answers before any ``open`` has been attempted, so
    ``current_capabilities() is None`` can never mean "the open failed".
    Pinned here so the probe's reliance on ``mgr.opened`` cannot quietly
    regress to a capabilities check.
    """
    from puffo_agent.agent.harness.drivers.codex import CodexDriver
    from puffo_agent.agent.harness.drivers.opencode import OpenCodeDriver
    from puffo_agent.agent.harness.drivers.pi import PiDriver

    for driver in (
        CodexDriver(executable_version="t"),
        OpenCodeDriver(executable_version="t"),
        PiDriver(executable_version="t"),
    ):
        assert driver.current_capabilities() is not None, type(driver).__name__


def test_a_wedge_this_probe_stopped_covering_is_withdrawn(
    registered_manager, saved_states,
):
    """Nothing else in the daemon can clear ``mcp_unreachable``.

    This probe is its only writer, and the batch-top override explicitly
    refuses to overwrite it. So narrowing what the probe covers without
    retracting the reds it already wrote would pin every opencode agent the
    buggy release flagged, for the life of the process. ``unknown``, not
    ``ok``: the claim is withdrawn for want of evidence, which is not the
    same as observing a healthy transport.
    """
    mgr = registered_manager(
        _FakeManager(
            "g1", time.monotonic() - 600,
            lifecycle=RuntimeLifecycle.PER_TURN_CHILD,
        )
    )
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker("mcp_unreachable")
    worker._mcp_probe_strikes = 2
    adapter = _wire(worker, mgr)

    _run(worker.probe_mcp_transport("t"))

    assert worker.runtime.health == "unknown"
    assert worker.runtime.error == ""
    assert worker._mcp_probe_strikes == 0
    assert adapter.reload_calls == []


def test_withdrawal_never_touches_a_red_this_probe_did_not_write(
    registered_manager, saved_states,
):
    """Only ``mcp_unreachable`` is this probe's to retract. A per-turn agent
    that is genuinely ``auth_failed`` must keep saying so."""
    mgr = registered_manager(
        _FakeManager(
            "g1", time.monotonic() - 600,
            lifecycle=RuntimeLifecycle.PER_TURN_CHILD,
        )
    )
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker("auth_failed")

    _run(worker.probe_mcp_transport("t"))

    assert worker.runtime.health == "auth_failed"


def test_exactly_which_shipped_drivers_this_probe_covers():
    """The negative half of the coverage claim, read off the real drivers.

    Widening the generation mint to every harness family is what put a
    promise on a driver that cannot keep it. Pin the roster both ways: any
    new per-turn (or in-process) driver, or a flip of an existing one, must
    land here and force the question of what names *its* wedge — this probe
    does not.
    """
    from puffo_agent.agent.harness.drivers.acp import acp_capabilities
    from puffo_agent.agent.harness.drivers.claude_code import (
        claude_capabilities,
    )
    from puffo_agent.agent.harness.drivers.codex import CODEX_CAPABILITIES
    from puffo_agent.agent.harness.drivers.opencode import (
        OPENCODE_CAPABILITIES,
    )
    from puffo_agent.agent.harness.drivers.pi import PI_CAPABILITIES

    covered = {
        "claude-code": claude_capabilities(),
        "codex": CODEX_CAPABILITIES,
        "pi": PI_CAPABILITIES,
        "acp": acp_capabilities(session_resume=True),
    }
    for name, capabilities in covered.items():
        assert capabilities.lifecycle == RuntimeLifecycle.PERSISTENT_CHILD, name

    assert (
        OPENCODE_CAPABILITIES.lifecycle == RuntimeLifecycle.PER_TURN_CHILD
    ), "opencode is the one harness this probe deliberately declines"


@pytest.mark.parametrize("starting_health", ["ok", "unknown", "in_progress", "no_progress"])
def test_wedge_is_named_from_the_states_a_wedge_actually_leaves_behind(
    starting_health, registered_manager, saved_states,
):
    """A real MCP failure rarely leaves health at ``ok``.

    The turn it breaks is admitted (``in_progress``) or keeps waking and
    consuming nothing (``no_progress``), so gating the flip on ok/unknown
    skipped the diagnosis in exactly the states the fault produces — the
    generic symptom stayed, and its remediation text points at provider
    credentials.
    """
    mgr = registered_manager(_FakeManager("g1", time.monotonic() - 120))
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker(starting_health)
    _wire(worker, mgr)

    _run(worker.probe_mcp_transport("t"))
    mgr.last_open_monotonic = time.monotonic() - 120
    _run(worker.probe_mcp_transport("t"))

    assert worker._mcp_probe_strikes == 2
    assert worker.runtime.health == "mcp_unreachable"


@pytest.mark.parametrize(
    "specific_red",
    ["auth_failed", "provider_error", "refresh_broken", "drained",
     "extra_usage_required", "unhandled_error"],
)
def test_wedge_never_overwrites_a_more_specific_cause(
    specific_red, registered_manager, saved_states,
):
    """The probe knows the transport is down, not that it is the only
    thing wrong. Widening the overwrite set must not turn into "the last
    writer wins" — a credential failure outranks it."""
    mgr = registered_manager(_FakeManager("g1", time.monotonic() - 120))
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker(specific_red)
    _wire(worker, mgr)

    _run(worker.probe_mcp_transport("t"))
    mgr.last_open_monotonic = time.monotonic() - 120
    _run(worker.probe_mcp_transport("t"))

    assert worker.runtime.health == specific_red


@pytest.mark.parametrize("starting_health", ["in_progress", "no_progress"])
def test_recovery_clears_the_wedge_from_the_widened_states(
    starting_health, registered_manager, saved_states,
):
    """Only checking that it turns red would miss a red that cannot come
    down: once the subprocess hellos back, health must return to ok."""
    mgr = registered_manager(_FakeManager("g1", time.monotonic() - 120))
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker(starting_health)
    _wire(worker, mgr)

    _run(worker.probe_mcp_transport("t"))
    mgr.last_open_monotonic = time.monotonic() - 120
    _run(worker.probe_mcp_transport("t"))
    assert worker.runtime.health == "mcp_unreachable"

    rpc_service.record_mcp_hello("t", mgr.spec.mcp_generation)
    _run(worker.probe_mcp_transport("t"))

    assert worker.runtime.health == "ok"
    assert worker.runtime.error == ""
    assert worker._mcp_probe_strikes == 0


def test_recycle_mints_new_generation_and_late_old_hello_stays_red(
    registered_manager, saved_states,
):
    """The automatic recycle must rotate the config generation: an old
    CLI's MCP subprocess is not guaranteed to die with its parent, and
    its late hello (same old generation, arriving after the new open
    watermark) must never read as the new runtime's health."""
    mgr = registered_manager(_FakeManager("g1", time.monotonic() - 120))
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker()
    adapter = _wire(worker, mgr)

    _run(worker.probe_mcp_transport("t"))
    assert adapter.reload_calls == [False]
    assert mgr.spec.mcp_generation != "g1"

    # The pre-recycle subprocess reports in late: fresh arrival, old
    # generation. Past the new grace window this is a miss, not health.
    rpc_service.record_mcp_hello("t", "g1")
    mgr.last_open_monotonic = time.monotonic() - 120
    _run(worker.probe_mcp_transport("t"))
    assert worker._mcp_probe_strikes == 2
    assert worker.runtime.health == "mcp_unreachable"


def test_recycled_generation_survives_zombie_pressure_before_first_probe(
    registered_manager, saved_states,
):
    """The pin must move at the generation switch, not at the first
    probe: after a recycle the registry would otherwise still pin the
    predecessor, and zombie beacons landing inside the mint->probe
    window would evict the new generation's hello — the next probe
    reads never-seen and a healthy runtime gets recycle-looped."""
    mgr = registered_manager(_FakeManager("g-old", time.monotonic() - 120))
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker()
    adapter = _wire(worker, mgr)

    # Probe 1 pins g-old (query) and recycles the silent runtime; the
    # recycle mints a fresh generation and must re-pin at the switch.
    _run(worker.probe_mcp_transport("t"))
    assert adapter.reload_calls == [False]
    new_gen = mgr.spec.mcp_generation
    assert new_gen != "g-old"

    # The new subprocess says hello, then zombie pressure fills the
    # slots before any probe has asked about the new generation.
    rpc_service.record_mcp_hello("t", new_gen, 60.0)
    for zombie in ("g-old", "g-z1", "g-z2", "g-z3"):
        rpc_service.record_mcp_hello("t", zombie, 60.0)

    # Probe 2: with the switch-time pin the new hello survived the
    # trim and reads healthy — strikes reset, no second reload.
    _run(worker.probe_mcp_transport("t"))
    assert worker._mcp_probe_strikes == 0
    assert adapter.reload_calls == [False]
    assert worker.runtime.health == "ok"
    assert rpc_service.mcp_hello_state("t", new_gen)[0] > 0.0


def test_reload_pins_minted_generation_before_reopen():
    """The post-reload pins land only after ``adapter.reload``
    returns, but the reopened subprocess hellos immediately — zombie
    beacon pressure inside that window can evict the new hello while
    the pin still names the predecessor. The adapter must push the
    minted generation through its generation sink after the spec
    reloader and BEFORE ``reload_resources`` reopens."""
    from puffo_agent.agent.harness.runtime.runtime_manager import (
        RuntimeManagerAdapter,
    )

    rpc_service.clear_mcp_hello("t")
    try:
        rpc_service.pin_mcp_generation("t", "g-old")
        pin_at_reopen: list[str | None] = []

        class _Mgr:
            spec = SimpleNamespace(mcp_generation="g-old", system_prompt="p")

            async def reload_resources(self, *, preserve_session, spec):
                pin_at_reopen.append(rpc_service._MCP_HELLO_PROBED.get("t"))
                self.spec = spec

        async def reloader(prompt):
            return SimpleNamespace(mcp_generation="g-new", system_prompt=prompt)

        adapter = RuntimeManagerAdapter(
            _Mgr(),
            spec_reloader=reloader,
            generation_sink=lambda gen: rpc_service.pin_mcp_generation(
                "t", gen
            ),
        )
        _run(adapter.reload("p2", with_session=False))

        assert pin_at_reopen == ["g-new"]
    finally:
        rpc_service.clear_mcp_hello("t")


def test_initial_prepare_pins_generation_before_first_probe(
    tmp_path, monkeypatch,
):
    """The third pin seam: binding a freshly prepared runtime must
    re-pin its minted generation immediately. A daemon-internal
    restart can leave the previous run's generation pinned; without
    the bind-time pin, zombie pressure between warm and the first
    probe evicts the new hello and the probe reads never-seen.
    Removing only the ``worker_run`` pin turns this red."""
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime
    from puffo_agent.agent.harness.driver import RuntimeSpec
    from puffo_agent.agent.harness.runtime.local_runtime import (
        PreparedLocalRuntime,
    )
    from puffo_agent.portal.worker_run import StandardWorkerRun

    rpc_service.clear_mcp_hello("t")
    try:
        # The previous worker run's generation is still pinned.
        rpc_service.record_mcp_hello("t", "g-old", 60.0)
        assert rpc_service.mcp_hello_state("t", "g-old")[0] > 0.0

        class _StubPreparer:
            agent_id = "t"

        prepared = PreparedLocalRuntime(
            harness_name="codex",
            spec=RuntimeSpec(str(tmp_path), mcp_generation="g-new"),
            native_session_id="",
            migration_source="fresh",
            legacy_session_path=tmp_path / "legacy.json",
            preparer=_StubPreparer(),
        )
        captured_kwargs: dict = {}

        def fake_builder(prepared, **kw):
            captured_kwargs.update(kw)
            return SimpleNamespace()

        monkeypatch.setattr(
            local_runtime, "build_local_runtime_adapter", fake_builder,
        )
        runner = StandardWorkerRun(SimpleNamespace())
        outbox = SimpleNamespace(set_active_turn=lambda *a, **kw: None)

        _run(runner._bind_driver_runtime(outbox, prepared, {}))

        # New hello lands, then zombie pressure fills the slots before
        # any probe has asked about the new generation.
        rpc_service.record_mcp_hello("t", "g-new", 60.0)
        for zombie in ("g-z1", "g-z2", "g-z3", "g-z4"):
            rpc_service.record_mcp_hello("t", zombie, 60.0)

        seen_at, interval = rpc_service.mcp_hello_state("t", "g-new")
        assert seen_at > 0.0
        assert interval == 60.0

        # The bind also hands the adapter a generation sink wired to
        # this agent, so reload-minted generations pin the same way.
        captured_kwargs["generation_sink"]("g-reloaded")
        assert rpc_service._MCP_HELLO_PROBED["t"] == "g-reloaded"
    finally:
        rpc_service.clear_mcp_hello("t")


def test_refresh_reload_pins_the_new_generation(
    tmp_path, registered_manager,
):
    """The refresh-flag reload also rebuilds the spec and mints a
    fresh generation; the pin must move with it (same window as the
    probe-driven recycle)."""
    mgr = registered_manager(_FakeManager("g-before", time.monotonic()))
    rpc_service.clear_mcp_hello("t")
    adapter = _RecyclingAdapter(mgr)
    (tmp_path / "refresh_session.flag").write_text("{}", encoding="utf-8")
    try:
        ok = _run(_process_refresh_flags(**_flags_kwargs(tmp_path, adapter)))
        assert ok is True
        assert mgr.spec.mcp_generation != "g-before"
        assert (
            rpc_service._MCP_HELLO_PROBED["t"] == mgr.spec.mcp_generation
        )
    finally:
        rpc_service.clear_mcp_hello("t")


def test_runtime_open_watermark_precedes_current_generation_hello():
    """A hello emitted during driver.open belongs to the runtime being opened."""
    class _HelloDuringOpenDriver:
        def __init__(self):
            self.queue: asyncio.Queue = asyncio.Queue()

        async def open(self, _spec, _resume=None):
            rpc_service.record_mcp_hello("opening", "g-current")
            return RuntimeOpened(
                RuntimeRef("runtime"),
                SessionRef("session"),
                "native",
                False,
                CODEX_CAPABILITIES,
                ProtocolDiagnostics(),
            )

        def events(self):
            async def iterate():
                while True:
                    event = await self.queue.get()
                    if event is None:
                        return
                    yield event
            return iterate()

        async def close(self):
            await self.queue.put(None)

    async def scenario():
        rpc_service.clear_mcp_hello("opening")
        manager = RuntimeManager(
            _HelloDuringOpenDriver(),
            RuntimeSpec("/tmp", mcp_generation="g-current"),
        )
        try:
            await manager.open()
            seen_at, _ = rpc_service.mcp_hello_state("opening", "g-current")
            assert seen_at > 0.0
            assert seen_at >= manager.last_open_monotonic
        finally:
            await manager.close()
            rpc_service.clear_mcp_hello("opening")

    _run(scenario())


def test_probe_hello_clears_wedge_state(registered_manager, saved_states):
    opened_at = time.monotonic() - 120
    mgr = registered_manager(_FakeManager("g1", opened_at))
    rpc_service.record_mcp_hello("t", "g1")
    worker = _seed_worker(health="mcp_unreachable")
    _wire(worker, mgr)
    worker._mcp_probe_strikes = 2

    _run(worker.probe_mcp_transport("t"))

    assert worker._mcp_probe_strikes == 0
    assert worker.runtime.health == "ok"


@pytest.fixture
def pinned_clock(monkeypatch):
    """Fixed monotonic value for tests that fabricate large past
    offsets: on a freshly booted CI runner ``time.monotonic()`` is
    small, so ``monotonic() - 4000`` goes negative and reads as
    never-seen."""
    from puffo_agent.portal import worker as worker_mod

    now = 1_000_000.0
    monkeypatch.setattr(worker_mod.time, "monotonic", lambda: now)
    return now


def test_beacon_silence_recycles(registered_manager, pinned_clock):
    """A subprocess that declared a re-hello cadence and then went
    silent is a wedge (the incident's 51-min RPC silence signature),
    even though its startup hello matched this generation."""
    now = pinned_clock
    mgr = registered_manager(_FakeManager("g1", now - 400))
    rpc_service._MCP_HELLO_SEEN["t"] = {"g1": (now - 400, 60.0)}
    worker = _seed_worker()
    adapter = _wire(worker, mgr)

    _run(worker.probe_mcp_transport("t"))

    assert adapter.reload_calls == [False]
    assert worker._mcp_probe_strikes == 1


def test_startup_only_hello_never_goes_stale(registered_manager, pinned_clock):
    """No declared cadence (older package, e.g. a lagging Docker image)
    keeps handshake semantics: an aged hello stays valid and the probe
    must not recycle-loop the runtime for silence."""
    now = pinned_clock
    mgr = registered_manager(_FakeManager("g1", now - 5000))
    rpc_service._MCP_HELLO_SEEN["t"] = {"g1": (now - 4000, None)}
    worker = _seed_worker()
    adapter = _wire(worker, mgr)

    _run(worker.probe_mcp_transport("t"))

    assert adapter.reload_calls == []
    assert worker._mcp_probe_strikes == 0


def test_surviving_old_beacon_cannot_evict_new_generation_health(
    registered_manager,
):
    """Hello state is keyed per (agent, generation): a surviving
    pre-recycle subprocess that keeps beaconing its old generation must
    not overwrite the current generation's healthy evidence — a single
    per-agent slot here recycled healthy runtimes in a loop."""
    mgr = registered_manager(_FakeManager("g-new", time.monotonic() - 120))
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker()
    adapter = _wire(worker, mgr)

    rpc_service.record_mcp_hello("t", "g-new", 60.0)
    rpc_service.record_mcp_hello("t", "g-old", 60.0)
    _run(worker.probe_mcp_transport("t"))

    assert adapter.reload_calls == []
    assert worker._mcp_probe_strikes == 0
    assert worker.runtime.health == "ok"


def test_hello_state_bounds_generations_per_agent():
    """Dead generations stop re-recording, so trimming by oldest
    arrival keeps the live ones and a leaking predecessor cannot grow
    the per-agent map without bound."""
    rpc_service.clear_mcp_hello("t")
    try:
        for n in range(rpc_service._MCP_HELLO_MAX_GENERATIONS + 1):
            rpc_service.record_mcp_hello("t", f"g{n}", 60.0)
        slots = rpc_service._MCP_HELLO_SEEN["t"]
        assert len(slots) == rpc_service._MCP_HELLO_MAX_GENERATIONS
        assert "g0" not in slots
        assert rpc_service.mcp_hello_state("t", "g1")[0] > 0.0
    finally:
        rpc_service.clear_mcp_hello("t")


def test_probed_generation_survives_zombie_beacon_pressure():
    """More live senders than slots must never evict the probed
    generation. With four surviving zombie subprocesses beaconing
    alongside the current one, a pure least-recently-heard trim
    periodically dropped the current generation (whichever beaconed
    longest ago), so the next probe read never-seen and recycled a
    healthy runtime."""
    rpc_service.clear_mcp_hello("t")
    try:
        rpc_service.record_mcp_hello("t", "g-current", 60.0)
        # The worker probe only ever queries the generation it minted;
        # that query pins it against the trim.
        assert rpc_service.mcp_hello_state("t", "g-current")[0] > 0.0
        # Four zombies beacon after the current generation, making it
        # the least recently heard entry when the trim fires.
        for n in range(4):
            rpc_service.record_mcp_hello("t", f"g-zombie{n}", 60.0)
        slots = rpc_service._MCP_HELLO_SEEN["t"]
        assert len(slots) == rpc_service._MCP_HELLO_MAX_GENERATIONS
        seen_at, interval = rpc_service.mcp_hello_state("t", "g-current")
        assert seen_at > 0.0
        assert interval == 60.0
    finally:
        rpc_service.clear_mcp_hello("t")


def test_empty_turn_cannot_clear_mcp_unreachable(saved_states):
    """Only a current-generation hello proves that the MCP lane recovered."""
    worker = _seed_worker(health="mcp_unreachable")
    worker.runtime.error = "puffo MCP subprocess never reached the daemon RPC service"

    log = logging.getLogger(__name__)
    Worker._flip_health_in_progress(worker.runtime, "t", log)
    Worker._resolve_health_on_success(worker.runtime, "t", log)

    assert worker.runtime.health == "mcp_unreachable"
    assert "never reached" in worker.runtime.error
    assert saved_states == []


def test_probe_ignores_stale_generation_hello(registered_manager):
    """A hello from the previous config generation is not proof the
    current spawn's transport works."""
    mgr = registered_manager(_FakeManager("g2", time.monotonic() - 120))
    rpc_service.record_mcp_hello("t", "g1")
    worker = _seed_worker()
    adapter = _wire(worker, mgr)

    _run(worker.probe_mcp_transport("t"))

    assert adapter.reload_calls == [False]


def test_probe_defers_while_turn_active(registered_manager):
    mgr = registered_manager(_FakeManager("g1", time.monotonic() - 120))
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker()
    adapter = _wire(worker, mgr)
    worker._turn_active = True

    _run(worker.probe_mcp_transport("t"))

    assert adapter.reload_calls == []
    assert worker._mcp_probe_strikes == 0


# ── hello plumbing ─────────────────────────────────────────────────────


def test_mcp_hello_route_records_generation():
    from aiohttp.test_utils import TestServer
    from aiohttp.test_utils import TestClient as AiohttpTestClient

    from puffo_agent.portal.local_service_auth import (
        issue_local_service_token,
        local_service_headers,
    )

    async def _exercise():
        cfg = rpc_service.RpcServiceConfig(enabled=True, port=0)
        app = rpc_service.build_app(cfg)
        client = AiohttpTestClient(TestServer(app))
        await client.start_server()
        try:
            headers = local_service_headers(issue_local_service_token("t"))
            resp = await client.post(
                "/v1/rpc/t/mcp-hello",
                json={"generation": "gen-42"},
                headers=headers,
            )
            assert resp.status == 200
            assert rpc_service.mcp_hello_state("t", "gen-42")[1] is None
            beacon = await client.post(
                "/v1/rpc/t/mcp-hello",
                json={"generation": "gen-42", "beacon_interval": 60},
                headers=headers,
            )
            assert beacon.status == 200
            bad = await client.post(
                "/v1/rpc/t/mcp-hello", json={}, headers=headers,
            )
            assert bad.status == 400
            for bad_value in (0, float("inf"), float("nan")):
                bad_interval = await client.post(
                    "/v1/rpc/t/mcp-hello",
                    json={"generation": "gen-42", "beacon_interval": bad_value},
                    headers=headers,
                )
                assert bad_interval.status == 400, bad_value
        finally:
            await client.close()

    rpc_service.clear_mcp_hello("t")
    _run(_exercise())
    seen_at, interval = rpc_service.mcp_hello_state("t", "gen-42")
    assert seen_at > 0.0
    assert interval == 60.0
    rpc_service.clear_mcp_hello("t")


def test_hello_startup_absent_without_generation(monkeypatch):
    from puffo_agent.mcp import puffo_core_server

    monkeypatch.delenv("PUFFO_MCP_GENERATION", raising=False)
    assert puffo_core_server._make_hello_startup(object()) is None
    monkeypatch.setenv("PUFFO_MCP_GENERATION", "g")
    assert puffo_core_server._make_hello_startup(None) is None


async def _drive_beacon(startup, calls, target: int, ticks: int = 500):
    task = asyncio.ensure_future(startup())
    for _ in range(ticks):
        if len(calls) >= target:
            break
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_hello_startup_retries_then_succeeds(monkeypatch):
    from puffo_agent.mcp import puffo_core_server

    monkeypatch.setenv("PUFFO_MCP_GENERATION", "g7")
    monkeypatch.setattr(
        puffo_core_server, "_HELLO_RETRY_DELAY_SECONDS", 0.0,
    )
    calls: list[tuple[str, float]] = []

    class _Client:
        async def hello(self, generation, *, beacon_interval=None):
            calls.append((generation, beacon_interval))
            if len(calls) < 3:
                raise RuntimeError("rpc mcp-hello transport error")
            return "ok"

    startup = puffo_core_server._make_hello_startup(_Client())
    _run(_drive_beacon(startup, calls, target=3))
    expected = ("g7", puffo_core_server._HELLO_BEACON_INTERVAL_SECONDS)
    assert calls == [expected, expected, expected]


def test_hello_beacon_refires_after_interval(monkeypatch):
    """After the startup burst the sender keeps re-helloing on its
    declared cadence — the daemon side reads sustained silence as a
    wedge, so a one-shot sender would defeat mid-life detection."""
    from puffo_agent.mcp import puffo_core_server

    monkeypatch.setenv("PUFFO_MCP_GENERATION", "g8")
    monkeypatch.setattr(
        puffo_core_server, "_HELLO_BEACON_INTERVAL_SECONDS", 0.0,
    )
    calls: list[tuple[str, float]] = []

    class _Client:
        async def hello(self, generation, *, beacon_interval=None):
            calls.append((generation, beacon_interval))
            return "ok"

    startup = puffo_core_server._make_hello_startup(_Client())
    _run(_drive_beacon(startup, calls, target=4))
    assert len(calls) >= 4
    assert all(call == ("g8", 0.0) for call in calls)


# ── per-spawn config generation ────────────────────────────────────────


def _local_preparer(tmp_path, monkeypatch, *, harness, agent_id, command=()):
    """A preparer wired for one harness family, with only the host lookups
    that would leave the sandbox stubbed out."""
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime
    from puffo_agent.agent.harness.runtime.local_runtime import (
        LocalRuntimePreparer,
    )

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "host"))
    monkeypatch.setattr(local_runtime, "is_macos", lambda: False)
    for name, value in (
        ("resolve_claude_bin", "/bin/claude"),
        ("resolve_codex_bin", "/bin/codex"),
        ("resolve_pi_bin", "/bin/pi"),
        ("resolve_opencode_bin", "/bin/opencode"),
    ):
        monkeypatch.setattr(local_runtime, name, lambda _v=value: _v)

    async def _no_install(**_kwargs):
        return {}

    monkeypatch.setattr(local_runtime, "run_spawn_install", _no_install)
    provider = {
        "claude-code": "anthropic",
        "codex": "openai",
    }.get(harness, "")
    # codex refuses to build a spec without auth; the gateway branch is the
    # one that needs no host credential file.
    gateway = (
        {"llm_base_url": "http://gateway.invalid", "api_key": "k"}
        if harness == "codex" else {}
    )
    config = AgentConfig(
        id=agent_id,
        runtime=RuntimeConfig(
            kind="cli-local",
            provider=provider,
            harness=harness,
            harness_command=list(command),
            **gateway,
        ),
        puffo_core=PuffoCoreConfig(
            slug="bot-gen", device_id="d1", space_id="sp1",
        ),
    )
    return LocalRuntimePreparer(DaemonConfig(), config)


# Every harness family the daemon can spawn a Puffo MCP subprocess for.
# ``acp`` needs an explicit command; the rest resolve a binary.
_MCP_HARNESS_FAMILIES = [
    ("claude-code", ()),
    ("codex", ()),
    ("pi", ()),
    ("opencode", ()),
    ("acp", ("lingtai-agent", "acp")),
]


@pytest.mark.parametrize("harness,command", _MCP_HARNESS_FAMILIES)
def test_every_harness_family_mints_a_generation(
    harness, command, tmp_path, monkeypatch,
):
    """The probe is keyed on ``spec.mcp_generation``; an empty one makes
    ``Worker.probe_mcp_transport`` return early, so a family that does not
    mint has *no* wedge detection at all — and nothing logs that.

    This is the assertion that was missing when the mint lived only in the
    claude-code branch: pi, opencode, acp and codex shipped with the
    detector silently disabled.
    """
    preparer = _local_preparer(
        tmp_path, monkeypatch,
        harness=harness, agent_id=f"gen-{harness}", command=command,
    )

    first = asyncio.run(preparer.refresh_spec("prompt"))
    second = asyncio.run(preparer.refresh_spec("prompt"))

    assert first.mcp_generation, f"{harness} spec carries no mcp_generation"
    # A recycle must not be able to accept the predecessor's hello.
    assert first.mcp_generation != second.mcp_generation


@pytest.mark.parametrize("harness,command", _MCP_HARNESS_FAMILIES)
def test_every_harness_family_hands_the_generation_to_the_subprocess(
    harness, command, tmp_path, monkeypatch,
):
    """Minting is only half of it: the subprocess must receive the value,
    because ``_make_hello_startup`` returns ``None`` on an empty
    ``PUFFO_MCP_GENERATION`` and then never sends a hello at all."""
    preparer = _local_preparer(
        tmp_path, monkeypatch,
        harness=harness, agent_id=f"env-{harness}", command=command,
    )

    spec = asyncio.run(preparer.refresh_spec("prompt"))
    delivered = _delivered_generations(preparer, spec, tmp_path, harness)

    assert delivered, f"{harness} spawns no Puffo MCP subprocess environment"
    for value in delivered:
        assert value == spec.mcp_generation


def _delivered_generations(preparer, spec, tmp_path, harness):
    """Every PUFFO_MCP_GENERATION this spec actually hands a subprocess.

    Each family carries the server differently — CLI config file, TOML,
    protocol projection, inline JSON, or the pi bridge — so the value has
    to be read back out of the shape that family really uses.
    """
    found = [
        server.environment.get("PUFFO_MCP_GENERATION", "")
        for server in spec.mcp_servers
    ]
    if harness == "claude-code":
        document = json.loads(
            (tmp_path / "puffo" / "agents" / f"env-{harness}"
             / "mcp-config.json").read_text(encoding="utf-8")
        )
        found.append(
            document["mcpServers"]["puffo"]["env"]["PUFFO_MCP_GENERATION"]
        )
    elif harness == "codex":
        from puffo_agent.portal.state import agent_codex_user_dir

        config = (
            agent_codex_user_dir(preparer.agent_id) / "config.toml"
        ).read_text(encoding="utf-8")
        found.extend(
            line.split("=", 1)[1].strip().strip('"')
            for line in config.splitlines()
            if line.strip().startswith("PUFFO_MCP_GENERATION")
        )
    elif harness == "opencode":
        inline = json.loads(spec.environment["OPENCODE_CONFIG_CONTENT"])
        found.append(
            inline["mcp"]["puffo"]["environment"]["PUFFO_MCP_GENERATION"]
        )
    elif harness == "pi":
        # Pi has no MCP client; the attested bridge carries the whole server
        # spec as JSON, so the generation has to survive that encoding too.
        from puffo_agent.agent.harness.drivers.pi_bridge import (
            BRIDGE_CONFIG_ENV,
        )

        bridge = json.loads(spec.environment[BRIDGE_CONFIG_ENV])
        found.append(bridge["environment"]["PUFFO_MCP_GENERATION"])
    return [value for value in found if value]


def _docker_preparer(tmp_path, monkeypatch, *, harness, agent_id):
    from puffo_agent.agent.harness.runtime.docker_runtime import (
        DockerRuntimePreparer,
    )

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "host"))
    gateway = (
        {"llm_base_url": "http://gateway.invalid", "api_key": "k"}
        if harness == "codex" else {}
    )
    config = AgentConfig(
        id=agent_id,
        runtime=RuntimeConfig(
            kind="cli-docker",
            provider="anthropic" if harness == "claude-code" else "openai",
            harness=harness,
            **gateway,
        ),
        puffo_core=PuffoCoreConfig(
            slug="bot-gen-d", device_id="d1", space_id="sp1",
        ),
    )
    return DockerRuntimePreparer(DaemonConfig(), config)


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_docker_spec_mints_fresh_generation(harness, tmp_path, monkeypatch):
    """Both container harnesses need the per-build generation: without it
    the transport probe exits early and the agent has no wedge recovery.

    codex shipped without one while claude had it — the same per-harness
    mint that left cli-local pi/opencode/acp unwired.
    """
    preparer = _docker_preparer(
        tmp_path, monkeypatch, harness=harness, agent_id=f"gen-docker-{harness}",
    )

    first = asyncio.run(preparer.refresh_spec("prompt"))
    second = asyncio.run(preparer.refresh_spec("prompt"))

    assert first.mcp_generation, f"docker {harness} carries no mcp_generation"
    assert first.mcp_generation != second.mcp_generation


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_docker_hands_the_generation_to_the_subprocess(
    harness, tmp_path, monkeypatch,
):
    preparer = _docker_preparer(
        tmp_path, monkeypatch, harness=harness, agent_id=f"env-docker-{harness}",
    )

    spec = asyncio.run(preparer.refresh_spec("prompt"))

    if harness == "claude-code":
        document = json.loads(
            (preparer.workspace_dir / ".puffo-agent" / "mcp-config.json")
            .read_text(encoding="utf-8")
        )
        delivered = [
            document["mcpServers"]["puffo"]["env"]["PUFFO_MCP_GENERATION"]
        ]
    else:
        config = (preparer.codex_home / "config.toml").read_text(
            encoding="utf-8"
        )
        delivered = [
            line.split("=", 1)[1].strip().strip('"')
            for line in config.splitlines()
            if line.strip().startswith("PUFFO_MCP_GENERATION")
        ]

    assert delivered, f"docker {harness} hands the subprocess no generation"
    for value in delivered:
        assert value == spec.mcp_generation


def test_no_puffo_core_means_no_generation_to_wait_for(tmp_path, monkeypatch):
    """A generation is a promise that some subprocess will hello back.

    An agent with no Puffo MCP configured has nobody to make that promise,
    so it must stay empty — otherwise the probe would recycle it forever
    waiting for a hello that cannot come.
    """
    from puffo_agent.agent.harness.runtime.docker_runtime import (
        DockerRuntimePreparer,
    )

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "host"))
    config = AgentConfig(
        id="no-core-docker",
        runtime=RuntimeConfig(
            kind="cli-docker", provider="anthropic", harness="claude-code",
        ),
    )
    preparer = DockerRuntimePreparer(DaemonConfig(), config)

    spec = asyncio.run(preparer.refresh_spec("prompt"))

    assert spec.mcp_generation == ""


# ── health reaches the server without waiting out the heartbeat ─────────


def test_health_change_wakes_the_heartbeat(tmp_path, monkeypatch):
    """``runtime.health`` only travels on heartbeats, so a red written
    just after a turn settles used to wait out the whole interval before
    the server heard about it — long enough to read as "never reported".

    The periodic tick stays: this only shortens the wait.
    """
    from puffo_agent.agent.status_reporter import StatusReporter
    from puffo_agent.portal.state import (
        set_runtime_health_listener,
        _RUNTIME_LAST_HEALTH,
    )

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    _RUNTIME_LAST_HEALTH.pop("hb", None)
    sent: list[str] = []

    class _Http:
        keyless = False

        async def post(self, path, body):
            sent.append(body.get("health", ""))
            return {}

    async def _drive():
        reporter = StatusReporter(
            _Http(),
            heartbeat_interval_s=3600.0,  # never fires on its own in this test
            runtime_health_provider=lambda: runtime.health,
        )
        set_runtime_health_listener("hb", reporter.request_immediate_heartbeat)
        loop_task = spawn_task(reporter.run_heartbeat_loop())
        try:
            await _settle()
            assert sent == ["ok"], sent

            runtime.health = "mcp_unreachable"
            runtime.save("hb")
            await _settle()
            assert sent == ["ok", "mcp_unreachable"], sent

            # An unchanged health must not add wire traffic.
            runtime.save("hb")
            await _settle()
            assert sent == ["ok", "mcp_unreachable"], sent

            # Recovery travels on the same path, not just the failure.
            runtime.health = "ok"
            runtime.save("hb")
            await _settle()
            assert sent == ["ok", "mcp_unreachable", "ok"], sent
        finally:
            set_runtime_health_listener("hb", None)
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

    runtime = RuntimeState(status="running", health="ok")
    runtime.save("hb")  # seed the baseline; no listener registered yet
    asyncio.run(_drive())


def test_a_failed_publish_never_blocks_the_local_health_write(tmp_path, monkeypatch):
    """The local file is authoritative. If the listener raises, health must
    still be persisted — the daemon cannot lose its own diagnosis because
    the server was unreachable."""
    from puffo_agent.portal.state import (
        set_runtime_health_listener,
        _RUNTIME_LAST_HEALTH,
        runtime_json_path,
    )

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    _RUNTIME_LAST_HEALTH.pop("boom", None)

    def _explode():
        raise RuntimeError("transport is down")

    runtime = RuntimeState(status="running", health="ok")
    runtime.save("boom")
    set_runtime_health_listener("boom", _explode)
    try:
        runtime.health = "mcp_unreachable"
        runtime.save("boom")
    finally:
        set_runtime_health_listener("boom", None)

    written = json.loads(
        runtime_json_path("boom").read_text(encoding="utf-8")
    )
    assert written["health"] == "mcp_unreachable"


def test_the_listener_is_told_a_health_it_can_already_read(tmp_path, monkeypatch):
    """The listener publishes the health the daemon just decided, so it must
    fire after the file lands, not before.

    Notifying first makes the announced value and the stored value two
    independent reads of a half-finished write, and lets a save that never
    lands announce itself anyway.
    """
    from puffo_agent.portal.state import (
        set_runtime_health_listener,
        _RUNTIME_LAST_HEALTH,
        runtime_json_path,
    )

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    _RUNTIME_LAST_HEALTH.pop("order", None)
    observed: list[str] = []

    def _read_the_file():
        observed.append(
            json.loads(
                runtime_json_path("order").read_text(encoding="utf-8")
            )["health"]
        )

    runtime = RuntimeState(status="running", health="ok")
    runtime.save("order")
    set_runtime_health_listener("order", _read_the_file)
    try:
        runtime.health = "mcp_unreachable"
        runtime.save("order")
    finally:
        set_runtime_health_listener("order", None)

    assert observed == ["mcp_unreachable"]


def test_a_write_that_never_lands_announces_nothing_and_stays_pending(
    tmp_path, monkeypatch
):
    """A failed save must not fire the listener — and must not consume the
    change either. The next successful save still has to report it, or a
    transient disk error would silently drop a red for good."""
    from puffo_agent.portal import state as state_module
    from puffo_agent.portal.state import (
        set_runtime_health_listener,
        _RUNTIME_LAST_HEALTH,
    )

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    _RUNTIME_LAST_HEALTH.pop("nodisk", None)
    fired: list[str] = []

    runtime = RuntimeState(status="running", health="ok")
    runtime.save("nodisk")
    set_runtime_health_listener("nodisk", lambda: fired.append(runtime.health))
    real_replace = state_module.os.replace
    try:
        def _fail(src, dst):
            raise OSError("no space left on device")

        monkeypatch.setattr(state_module.os, "replace", _fail)
        runtime.health = "mcp_unreachable"
        with pytest.raises(OSError):
            runtime.save("nodisk")
        assert fired == []

        monkeypatch.setattr(state_module.os, "replace", real_replace)
        runtime.save("nodisk")
        assert fired == ["mcp_unreachable"]
    finally:
        set_runtime_health_listener("nodisk", None)


@pytest.mark.asyncio
async def test_the_health_listener_lives_exactly_as_long_as_the_loop():
    """The registry is a module global, so a reporter that stops must not
    stay reachable through it — and a reporter that starts again must be
    reachable again.

    Both halves are load-bearing. ws-local reuses one reporter across
    attaches, spawning a fresh heartbeat loop each time and calling ``stop``
    on every detach: binding at construction and releasing at ``stop`` would
    unbind on the first detach and never rebind, silently retiring the
    immediate push for the rest of the agent's life.
    """
    from puffo_agent.portal.state import _RUNTIME_HEALTH_LISTENERS

    class _Bridge:
        async def send_status(self, status, **kwargs):
            return None

        def add_connected_callback(self, callback):
            return None

    worker = Worker(
        DaemonConfig(),
        AgentConfig(
            id="teardown-agent",
            runtime=RuntimeConfig(kind="cli-local", harness="codex"),
        ),
    )
    reporter = worker._build_status_reporter(
        SimpleNamespace(http=SimpleNamespace(keyless=True), _bridge=_Bridge())
    )
    try:
        assert "teardown-agent" not in _RUNTIME_HEALTH_LISTENERS

        for _attach in range(2):
            loop_task = spawn_task(reporter.run_heartbeat_loop())
            await _settle()
            assert "teardown-agent" in _RUNTIME_HEALTH_LISTENERS

            reporter.stop()
            await loop_task
            assert "teardown-agent" not in _RUNTIME_HEALTH_LISTENERS
    finally:
        _RUNTIME_HEALTH_LISTENERS.pop("teardown-agent", None)


def test_a_late_unbind_never_silences_the_reporter_that_replaced_it():
    """A cancelled loop runs its ``finally`` whenever the event loop next
    gets to it, which can be after a rebuilt reporter has claimed the slot.
    Releasing by identity keeps the live one registered."""
    from puffo_agent.portal.state import (
        _RUNTIME_HEALTH_LISTENERS,
        clear_runtime_health_listener,
        set_runtime_health_listener,
    )

    def _old():
        pass

    def _new():
        pass

    try:
        set_runtime_health_listener("succeeded-agent", _old)
        set_runtime_health_listener("succeeded-agent", _new)
        clear_runtime_health_listener("succeeded-agent", _old)
        assert _RUNTIME_HEALTH_LISTENERS.get("succeeded-agent") is _new
        clear_runtime_health_listener("succeeded-agent", _new)
        assert "succeeded-agent" not in _RUNTIME_HEALTH_LISTENERS
    finally:
        _RUNTIME_HEALTH_LISTENERS.pop("succeeded-agent", None)


def spawn_task(coro):
    return asyncio.ensure_future(coro)


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)
