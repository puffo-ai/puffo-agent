"""Worker-side reassertion + adapter dispatch — the seams the round-trip
probe test doesn't cover: ``LocalCli.health_probe`` delegates to a live
``CodexSession`` (else True), and ``_reassert_auth_failed_after_failed_probe``
only re-flips the eager-cleared ``ok`` state (no clobber of sticky-red)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace


# ── (1) LocalCli.health_probe dispatch ─────────────────────────────────────


def test_local_cli_probe_delegates_to_codex_session(monkeypatch):
    """With a CodexSession built, LocalCli delegates to its probe."""
    from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter

    class _StubCodex:
        async def health_probe(self):
            return False

    stub = SimpleNamespace.__new__(SimpleNamespace)
    # __new__ to skip __init__ — the method only reads _codex_session.
    adapter = LocalCLIAdapter.__new__(LocalCLIAdapter)
    adapter._session = None
    adapter._codex_session = _StubCodex()

    assert asyncio.run(adapter.health_probe()) is False


def test_local_cli_probe_returns_true_without_codex_session():
    """No _codex_session → inherit the True default (non-Codex agents
    surface a real failure via the existing leak filter)."""
    from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter

    adapter = LocalCLIAdapter.__new__(LocalCLIAdapter)
    adapter._session = None
    adapter._codex_session = None

    assert asyncio.run(adapter.health_probe()) is True


# ── (2) Worker._reassert_auth_failed_after_failed_probe ────────────────────


class _Runtime:
    """Stand-in for RuntimeState — records save() calls instead of
    hitting disk."""
    def __init__(self, health="ok", error=""):
        self.health = health
        self.error = error
        self.saved = []

    def save(self, agent_id):
        self.saved.append((agent_id, self.health, self.error))


def test_reassert_flips_ok_back_to_auth_failed():
    """eager-clear left health=ok; probe-fail → re-assert auth_failed."""
    from puffo_agent.portal.worker import Worker

    rt = _Runtime(health="ok", error="")
    Worker._reassert_auth_failed_after_failed_probe(
        rt, "agent-a", logging.getLogger("puf311-test"),
    )

    assert rt.health == "auth_failed"
    # PUF-343: reassertion path shares the same user-facing shape as the
    # rest of the auth-failed surfaces so the web pane / CLI status /
    # DM copy stay aligned. The "post-warm probe failed" diagnostic lives
    # in the daemon log (log.warning at the call site).
    assert "Claude Code sign-in expired" in rt.error
    assert "claude auth login" in rt.error
    assert rt.saved == [(
        "agent-a", "auth_failed",
        "Claude Code sign-in expired. On the computer running "
        "puffo-agent, open a terminal and run `claude auth "
        "login`, then send this agent a message.",
    )]


def test_reassert_noop_when_already_auth_failed():
    """Already auth_failed → no re-flip, no save(); the existing error
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
    """Other sticky-red states (api_error_abandoned, codex_thread_wedged,
    refresh_broken, …) have their own recovery paths — don't clobber them."""
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
    """``in_progress`` (active-batch marker) must not be clobbered."""
    from puffo_agent.portal.worker import Worker

    rt = _Runtime(health="in_progress", error="")
    Worker._reassert_auth_failed_after_failed_probe(
        rt, "agent-d", logging.getLogger("puf311-test"),
    )

    assert rt.health == "in_progress"
    assert rt.saved == []


# ── (3) Base Adapter health_probe default ──────────────────────────────────


def test_base_adapter_health_probe_defaults_true():
    """Base default is True — non-Codex adapters need no opt-out code."""
    from puffo_agent.agent.adapters.base import Adapter

    class _Concrete(Adapter):
        async def run_turn(self, ctx):  # noqa: ARG002
            raise NotImplementedError

    adapter = _Concrete()
    assert asyncio.run(adapter.health_probe()) is True


# ── Polish round-1 folds ───────────────────────────────────────────────────


def _make_gate_worker(runtime, adapter):
    """Minimal Worker stub with just the attributes _run_post_warm_gate
    reads."""
    from puffo_agent.portal.worker import Worker

    w = Worker.__new__(Worker)
    w.runtime = runtime
    w._adapter = adapter
    w._warm_done = asyncio.Event()
    return w


def test_warm_done_only_flips_after_probe_completes():
    """Ordering invariant: ``_warm_done`` stays clear until the probe
    finishes (else a queued message could dispatch against an unprobed
    runtime). A slow stubbed probe makes the order observable."""

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
    """Probe-fail path: the reassert must ALSO finish before
    ``_warm_done`` flips, so no message fires mid-reassert."""

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
    """A probe that raises is caught by the gate's own try/except and
    treated as probe-fail (reassert + release warm), not a crash."""

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
    """Probe-fail re-asserts state but must NOT fire a second operator
    DM (the operator already got the original auth_failed DM before
    eager-clear) — pinned via a stub that records any DM sent."""

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
