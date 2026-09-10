"""A refresher must only speak for the harness whose credentials it owns.

#337: ``Daemon._refresher_for`` routed every non-codex harness to the Claude
refresher. That refresher has no Pi or OpenCode backend, so it could not help
those agents — but it could stop them. Its ``ensure_fresh`` is the worker's
pre-delivery gate, so an expired *Claude* token on the host blocked Pi and
OpenCode turns from ever reaching their own provider, and its failure fan-out
marked them red with Claude's recovery steps.

Measured on a QA host: ``turn.admitted`` at 18:33:41,431 -> the gate refreshed
an expired Claude token for 6.4s -> ``turn.failed`` at 18:33:47,867 with zero
provider events in between. The Pi agent's own credential was valid and never
used.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from puffo_agent.portal.daemon import Daemon

# The harnesses a daemon refresher actually has a backend for. Everything else
# carries its own credentials and must not be gated on someone else's.
OWNED = ["claude-code", "codex"]
UNOWNED = ["pi", "opencode", "gemini", "some-future-harness"]


class _StubRefresher:
    def __init__(self) -> None:
        self.registered: list = []
        self.success_callbacks: list = []

    def register_agent(self, home) -> None:
        self.registered.append(home)

    def register_on_refresh_success(self, cb) -> None:
        self.success_callbacks.append(cb)

    def ensure_fresh(self, *_a, **_k) -> bool:  # pragma: no cover - identity
        return True

    def notify_refresh_needed(self, *_a, **_k) -> None:  # pragma: no cover
        return None


def _daemon() -> Daemon:
    d = Daemon.__new__(Daemon)
    d.refresher = _StubRefresher()
    d.codex_refresher = _StubRefresher()
    d.daemon_cfg = SimpleNamespace()
    return d


def _cfg(harness: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="agent-1",
        runtime=SimpleNamespace(
            harness=harness, llm_base_url="", kind="cli-local",
        ),
    )


def _worker() -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(health="ok"),
        _auth_failed_notification_sent=False,
        _refresh_success_callback=None,
    )


# ── the pre-delivery gate ──

@pytest.mark.parametrize("harness", UNOWNED)
def test_unowned_harness_has_no_pre_delivery_gate(harness):
    """The blocker itself: no gate means an unrelated provider's expired
    credential can no longer stop this agent's turn."""
    assert _daemon()._ensure_fresh_for(_cfg(harness)) is None


def test_claude_code_keeps_its_gate():
    d = _daemon()
    assert d._ensure_fresh_for(_cfg("claude-code")) == d.refresher.ensure_fresh


def test_codex_keeps_its_gate():
    d = _daemon()
    assert (
        d._ensure_fresh_for(_cfg("codex")) == d.codex_refresher.ensure_fresh
    )


# ── registration / failure fan-out ──

@pytest.mark.parametrize("harness", UNOWNED)
def test_unowned_harness_is_registered_with_no_refresher(harness):
    """Registration is what subscribes an agent to the failure fan-out."""
    d = _daemon()
    d._register_with_refresher(_cfg(harness), _worker())
    assert d.refresher.registered == []
    assert d.codex_refresher.registered == []
    assert d.refresher.success_callbacks == []
    assert d.codex_refresher.success_callbacks == []


def test_claude_code_is_still_registered():
    d = _daemon()
    d._register_with_refresher(_cfg("claude-code"), _worker())
    assert len(d.refresher.registered) == 1
    assert d.codex_refresher.registered == []


def test_codex_is_still_registered():
    d = _daemon()
    d._register_with_refresher(_cfg("codex"), _worker())
    assert len(d.codex_refresher.registered) == 1
    assert d.refresher.registered == []


@pytest.mark.parametrize("harness", UNOWNED)
def test_unowned_harness_gets_no_refresh_wakeup(harness):
    """A Pi 401 must not wake the Claude refresher to probe Anthropic."""
    assert _daemon()._notify_refresh_for(_cfg(harness)) is None


@pytest.mark.parametrize("harness", OWNED)
def test_owned_harness_keeps_its_refresh_wakeup(harness):
    assert _daemon()._notify_refresh_for(_cfg(harness)) is not None


# ── the behaviour the wiring exists for ──
#
# The tests above assert what `_ensure_fresh_for` returns. This one drives the
# real gate in `StandardWorkerRun._execute_global_turn` to prove the outcome
# Peter asked for: with the Claude refresher failing, a Pi turn still reaches
# its own provider, and a genuine auth failure is still reported.

class _FailingGate:
    """Stands in for an expired host Claude credential: ensure_fresh False."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return False


def _turn_worker(ensure_fresh_token):
    reached = {"provider": False}

    class _Puffo:
        async def handle_global_inbox_turn(self, planned):
            reached["provider"] = True
            return "reply"

    worker = SimpleNamespace(
        _turn_active=False,
        _reload_lock=asyncio.Lock(),
        _ensure_fresh_token=ensure_fresh_token,
        agent_cfg=SimpleNamespace(runtime=SimpleNamespace(kind="cli-local")),
        # save(): _flip_health_in_progress persists the transition, so the
        # stub needs it. Reaching that call is itself evidence the gate was
        # skipped rather than short-circuited.
        runtime=SimpleNamespace(
            health="ok", error="", save=lambda _agent_id: None,
        ),
        _maybe_wake_refresher_if_auth_failed=lambda _agent_id: None,
        _entered_auth_failed=False,
    )

    def _enter_auth_failed(_agent_id):
        worker._entered_auth_failed = True
    worker._enter_auth_failed = _enter_auth_failed

    context = SimpleNamespace(
        paths=SimpleNamespace(agent_id="agent-1"),
        puffo=_Puffo(),
    )
    return worker, context, reached


def _run_turn(worker, context):
    from puffo_agent.portal import worker_run as wr

    run = wr.StandardWorkerRun.__new__(wr.StandardWorkerRun)
    run.worker = worker

    async def _noop_refresh(_ctx):
        return None
    run._apply_refresh = _noop_refresh

    return asyncio.new_event_loop().run_until_complete(
        run._execute_global_turn(context, planned=object())
    )


@pytest.mark.parametrize("harness", UNOWNED)
def test_unowned_turn_reaches_its_provider_while_claude_refresh_fails(harness):
    """The blocker, end to end, at the point where it actually bit.

    The gate handed to the worker is the one the *daemon* chooses, so this
    drives ``_ensure_fresh_for`` rather than hardcoding ``None`` — otherwise
    reverting the daemon fix would leave the test green while the bug is back.
    The Claude refresher here fails every ``ensure_fresh`` call, standing in
    for the expired host token measured on the QA host.
    """
    d = _daemon()
    d.refresher.ensure_fresh = _FailingGate()
    d.codex_refresher.ensure_fresh = _FailingGate()

    gate = d._ensure_fresh_for(_cfg(harness))
    worker, context, reached = _turn_worker(ensure_fresh_token=gate)

    assert _run_turn(worker, context) == "reply"
    assert reached["provider"] is True, (
        "the turn must reach its own provider; before #337 it died at a gate "
        "belonging to a provider this agent does not use"
    )
    assert worker._entered_auth_failed is False
    assert d.refresher.ensure_fresh.calls == 0, (
        "the Claude refresher must not even be consulted for this harness"
    )


def test_a_gated_harness_with_a_failing_refresh_still_reports_auth_failure():
    """The other half: scoping must not silence real auth failures.

    A claude-code agent keeps its gate, so a failing refresh still enters
    auth_failed and never reaches the provider.
    """
    from puffo_agent.portal import worker as worker_module

    gate = _FailingGate()
    worker, context, reached = _turn_worker(ensure_fresh_token=gate)
    with pytest.raises(worker_module.AgentAPIError) as excinfo:
        _run_turn(worker, context)

    assert excinfo.value.is_auth is True
    assert gate.calls == 1
    assert worker._entered_auth_failed is True
    assert reached["provider"] is False
