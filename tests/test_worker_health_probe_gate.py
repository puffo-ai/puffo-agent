"""PUF-311 worker-side reassertion + adapter dispatch.

Two seams that the round-trip-Codex test doesn't cover:

1. ``LocalCli.health_probe`` must delegate to the live
   ``CodexSession`` when one exists and short-circuit to True
   otherwise (Claude / hermes / gemini-cli inherit the no-probe
   default).

2. ``Worker._reassert_auth_failed_after_failed_probe`` is the
   state-mutation helper the worker calls when ``health_probe``
   returns False. It must only re-flip when the runtime is
   currently ``ok`` (the eager-clear case) and otherwise no-op so
   sticky-red states aren't clobbered.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace


# ── (1) LocalCli.health_probe dispatch ─────────────────────────────────────


def test_local_cli_probe_delegates_to_codex_session(monkeypatch):
    """Codex harness path: when the adapter has built a CodexSession,
    its probe is the source of truth. LocalCli is a thin shim."""
    from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter

    class _StubCodex:
        async def health_probe(self):
            return False

    stub = SimpleNamespace.__new__(SimpleNamespace)
    # Build a minimal LocalCLIAdapter without going through __init__
    # — the only attribute the method reads is _codex_session.
    adapter = LocalCLIAdapter.__new__(LocalCLIAdapter)
    adapter._session = None
    adapter._codex_session = _StubCodex()

    assert asyncio.run(adapter.health_probe()) is False


def test_local_cli_probe_returns_true_without_codex_session():
    """Non-Codex agents (no _codex_session built) inherit the safe
    True default — their next-message path surfaces a real failure
    via the existing leak filter; the probe doesn't need to
    pre-empt them."""
    from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter

    adapter = LocalCLIAdapter.__new__(LocalCLIAdapter)
    adapter._session = None
    adapter._codex_session = None

    assert asyncio.run(adapter.health_probe()) is True


# ── (2) Worker._reassert_auth_failed_after_failed_probe ────────────────────


class _Runtime:
    """Stand-in for portal.state.RuntimeState — just the fields the
    reassert helper touches. The real class hits disk via save();
    here we record the calls instead."""
    def __init__(self, health="ok", error=""):
        self.health = health
        self.error = error
        self.saved = []

    def save(self, agent_id):
        self.saved.append((agent_id, self.health, self.error))


def test_reassert_flips_ok_back_to_auth_failed():
    """The load-bearing case: eager-clear set health=ok before the
    worker even respawned; post-warm probe returned False; the helper
    re-asserts auth_failed so the next refresh cycle retries."""
    from puffo_agent.portal.worker import Worker

    rt = _Runtime(health="ok", error="")
    Worker._reassert_auth_failed_after_failed_probe(
        rt, "agent-a", logging.getLogger("puf311-test"),
    )

    assert rt.health == "auth_failed"
    assert "health probe failed" in rt.error
    assert rt.saved == [("agent-a", "auth_failed",
                         "post-recovery health probe failed — provider "
                         "still unreachable; waiting for next credential "
                         "refresh")]


def test_reassert_noop_when_already_auth_failed():
    """No second flip / second DM. Worker's existing
    _auth_failed_notification_sent dedup gates the DM separately;
    this helper just refuses to clobber the existing sticky-red
    state. Specifically: no save() call, so the existing error
    message survives."""
    from puffo_agent.portal.worker import Worker

    rt = _Runtime(health="auth_failed", error="original reason")
    Worker._reassert_auth_failed_after_failed_probe(
        rt, "agent-b", logging.getLogger("puf311-test"),
    )

    assert rt.health == "auth_failed"
    assert rt.error == "original reason"
    assert rt.saved == []


def test_reassert_noop_for_other_sticky_health_values():
    """Don't clobber api_error_abandoned, codex_thread_wedged,
    refresh_broken, or any other sticky-red state — the probe only
    speaks to auth/transport reachability, and other red states have
    their own clear-on-recover paths that shouldn't be hijacked."""
    from puffo_agent.portal.worker import Worker

    for sticky in (
        "api_error_abandoned",
        "codex_thread_wedged",
        "refresh_broken",
        "unhandled_error",
    ):
        rt = _Runtime(health=sticky, error=f"due to {sticky}")
        Worker._reassert_auth_failed_after_failed_probe(
            rt, "agent-c", logging.getLogger("puf311-test"),
        )
        assert rt.health == sticky
        assert rt.saved == []


def test_reassert_noop_when_in_progress():
    """``in_progress`` is the active-batch marker. The probe runs
    pre-batch, so this should never trigger in practice — but pin
    it anyway: don't clobber an in-progress turn."""
    from puffo_agent.portal.worker import Worker

    rt = _Runtime(health="in_progress", error="")
    Worker._reassert_auth_failed_after_failed_probe(
        rt, "agent-d", logging.getLogger("puf311-test"),
    )

    assert rt.health == "in_progress"
    assert rt.saved == []


# ── (3) Base Adapter health_probe default ──────────────────────────────────


def test_base_adapter_health_probe_defaults_true():
    """The default at the Adapter base — non-Codex adapters must NOT
    need any new code to opt out of the probe. Returning True keeps
    the eager-clear behaviour intact for Claude / hermes / etc."""
    from puffo_agent.agent.adapters.base import Adapter

    class _Concrete(Adapter):
        async def run_turn(self, ctx):  # noqa: ARG002
            raise NotImplementedError

    adapter = _Concrete()
    assert asyncio.run(adapter.health_probe()) is True


# ── Polish round-1 folds ───────────────────────────────────────────────────


def _make_gate_worker(runtime, adapter):
    """Build a minimal worker stub the post-warm gate can run against.
    The real Worker constructor does a lot of setup we don't need here;
    we just need the attributes _run_post_warm_gate reads."""
    from puffo_agent.portal.worker import Worker

    w = Worker.__new__(Worker)
    w.runtime = runtime
    w._adapter = adapter
    w._warm_done = asyncio.Event()
    return w


def test_warm_done_only_flips_after_probe_completes():
    """PUF-311 ordering invariant: ``_warm_done`` MUST stay clear
    until the probe (+ reassert if probe-fail) finishes. Reversing
    the order would let a queued message dispatch against an
    unprobed runtime. Stub the adapter probe with an Event so we can
    observe the order from outside."""

    started = asyncio.Event()
    release = asyncio.Event()
    captured = {}

    class _SlowProbe:
        async def health_probe(self):
            started.set()
            await release.wait()
            return True

    async def _run():
        w = _make_gate_worker(_Runtime("ok"), _SlowProbe())
        gate_task = asyncio.create_task(w._run_post_warm_gate("agent-a"))
        await started.wait()
        captured["mid_probe"] = w._warm_done.is_set()
        release.set()
        await gate_task
        captured["post_gate"] = w._warm_done.is_set()

    asyncio.run(_run())
    assert captured["mid_probe"] is False
    assert captured["post_gate"] is True


def test_warm_done_only_flips_after_reassert_when_probe_fails():
    """Tighter ordering: the reassert helper must ALSO finish before
    ``_warm_done`` flips, otherwise a queued message could fire while
    runtime.health is mid-reassert. Probe returns False → reassert
    runs → _warm_done set. Asserted via a runtime stub whose save()
    is observable from the test."""

    started = asyncio.Event()
    release = asyncio.Event()
    captured = {}

    class _SlowProbe:
        async def health_probe(self):
            started.set()
            await release.wait()
            return False

    async def _run():
        rt = _Runtime("ok")
        w = _make_gate_worker(rt, _SlowProbe())
        gate_task = asyncio.create_task(w._run_post_warm_gate("agent-b"))
        await started.wait()
        # Mid-probe: nothing has flipped yet.
        captured["mid_probe_health"] = rt.health
        captured["mid_probe_warm_done"] = w._warm_done.is_set()
        release.set()
        await gate_task
        captured["post_gate_health"] = rt.health
        captured["post_gate_warm_done"] = w._warm_done.is_set()

    asyncio.run(_run())
    assert captured["mid_probe_health"] == "ok"
    assert captured["mid_probe_warm_done"] is False
    assert captured["post_gate_health"] == "auth_failed"
    assert captured["post_gate_warm_done"] is True


def test_gate_treats_adapter_probe_exception_as_failure():
    """A future adapter override that forgets the internal try/except
    must NOT crash the worker; the gate's own try/except catches and
    treats as probe-fail, reasserting auth_failed. Otherwise the
    warm path would die and the agent would stay stuck-spawning."""

    class _RaisingProbe:
        async def health_probe(self):
            raise RuntimeError("simulated forgot-to-except in adapter")

    async def _run():
        rt = _Runtime("ok")
        w = _make_gate_worker(rt, _RaisingProbe())
        await w._run_post_warm_gate("agent-c")
        return rt, w

    rt, w = asyncio.run(_run())
    # Reassert fired (worker treated the raise as probe-fail).
    assert rt.health == "auth_failed"
    # Warm gate still released so the daemon's wait_warm completes.
    assert w._warm_done.is_set()


def test_reassert_does_not_fire_a_second_operator_dm():
    """The reassert helper must NOT call ``_on_auth_failed_enter`` /
    schedule a new DM. The operator already got the original
    auth_failed DM before eager-clear; probe-fail is a "still broken"
    signal, not a new fault. A future change wiring the ENTER hook
    into the reassert path would surface as silent operator-spam —
    pin the no-DM contract via a stub that would fail if any DM is
    sent."""

    sent: list = []

    class _Probe:
        async def health_probe(self):
            return False

    class _StubClient:
        operator_slug = "@han-0001"

        async def _send_dm(self, recipient, text, root_id):
            sent.append((recipient, text))

    async def _run():
        from puffo_agent.portal.worker import Worker

        rt = _Runtime("ok")
        w = Worker.__new__(Worker)
        w.runtime = rt
        w._adapter = _Probe()
        w._warm_done = asyncio.Event()
        # Wire the DM substrate path so a regression would catch it.
        w._client = _StubClient()
        w._auth_failed_notification_sent = False
        w.agent_cfg = SimpleNamespace(
            id="t-agent",
            display_name="Tester",
            runtime=SimpleNamespace(harness="codex"),
        )

        await w._run_post_warm_gate("agent-d")
        # Yield once in case a regression scheduled a task on the loop.
        await asyncio.sleep(0)
        return rt

    rt = asyncio.run(_run())
    assert rt.health == "auth_failed"
    assert sent == [], (
        "reassert path must not fire a second operator DM; "
        f"got {sent!r}"
    )
