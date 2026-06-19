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
