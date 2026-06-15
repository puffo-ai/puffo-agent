"""PUF-303: proactive refresh_broken DM to operator on the
``CredentialRefresher._flip_refresh_broken`` newly-broken transition.
Mirrors PUF-283's auth_failed substrate.

Tests cover:
  1. ``format_refresh_broken`` bilingual copy invariants.
  2. ``CredentialRefresher._flip_refresh_broken`` fires the new
     ``on_refresh_broken_enter`` callback only for agents that newly
     transition (was-ok → refresh_broken), NOT for agents already
     refresh_broken.
  3. ``Worker._on_refresh_broken_enter`` is dedup-gated by
     ``_refresh_broken_notification_sent``.
  4. ``_notify_operator_of_refresh_broken`` skips cleanly on
     no-warm-client and no-operator-slug paths; happy path DMs the
     operator with the bilingual recovery copy.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent._invite_strings import format_refresh_broken


# ── (1) format_refresh_broken bilingual copy ──────────────────────


def test_refresh_broken_copy_includes_english_and_chinese():
    text = format_refresh_broken("planner-1234", "Planner")
    # English strand: distinguishes daemon-side refresh failure from
    # in-flight 401, names `claude auth login` + send-message recovery.
    assert "credential refresh is broken" in text
    assert "claude auth login" in text
    assert "send me a message" in text
    # Chinese strand
    assert "凭证刷新失败" in text
    assert "守护进程无法续期" in text
    # Bold display name format
    assert "**Planner**" in text


def test_refresh_broken_copy_degrades_when_display_name_missing():
    text = format_refresh_broken("agent-5678", "")
    assert "credential refresh is broken" in text
    assert "凭证刷新失败" in text
    assert "`agent-5678`" in text
    # No empty-bold artifact.
    assert "**" not in text or "**`agent-5678`**" in text


# ── (2) CredentialRefresher._flip_refresh_broken transition gate ──


def test_flip_refresh_broken_fires_callback_for_newly_broken_only(
    tmp_path, monkeypatch,
):
    """The fire-once-per-transition invariant: an agent already
    refresh_broken on entry to _flip_refresh_broken must NOT receive
    a second callback. Mirrors the was_ok semantics from
    worker._enter_auth_failed."""
    from puffo_agent.portal import credential_refresh as cr_module
    from puffo_agent.portal.credential_refresh import (
        CredentialRefresher, RefreshOutcome,
    )

    # Stub RuntimeState so we don't need real on-disk agents.
    saved_states: dict[str, str] = {}

    class _StubRuntimeState:
        def __init__(self, health: str):
            self.health = health
            self.error = ""

        @classmethod
        def load(cls, agent_id: str):
            return cls(saved_states.get(agent_id, "ok"))

        def save(self, agent_id: str):
            saved_states[agent_id] = self.health

    monkeypatch.setattr(cr_module, "logger", logging.getLogger("test"))
    # Patch the local import inside _flip_refresh_broken.
    import puffo_agent.portal.state as state_module
    monkeypatch.setattr(state_module, "RuntimeState", _StubRuntimeState)

    refresher = CredentialRefresher.__new__(CredentialRefresher)
    refresher._agent_homes = {tmp_path / "fresh-1", tmp_path / "stuck-2"}
    refresher._on_refresh_broken_enter = []
    refresher._consecutive_non_success = 2

    fired: list[str] = []
    refresher.register_on_refresh_broken_enter(fired.append)

    # ``stuck-2`` is already refresh_broken; ``fresh-1`` is ok.
    saved_states["stuck-2"] = "refresh_broken"

    refresher._flip_refresh_broken(RefreshOutcome.FAILED)

    # Only the newly-broken agent triggers the callback.
    assert fired == ["fresh-1"]
    assert saved_states["fresh-1"] == "refresh_broken"
    # Already-broken agent's state is unchanged (no spurious resave).
    assert saved_states["stuck-2"] == "refresh_broken"


def test_register_unregister_on_refresh_broken_enter():
    """The register/unregister pair is symmetric and unregister is
    a no-op when the callback wasn't registered (mirror
    ``unregister_on_refresh_success``)."""
    from puffo_agent.portal.credential_refresh import CredentialRefresher

    r = CredentialRefresher.__new__(CredentialRefresher)
    r._on_refresh_broken_enter = []

    def cb(_):
        pass

    r.register_on_refresh_broken_enter(cb)
    assert cb in r._on_refresh_broken_enter
    r.unregister_on_refresh_broken_enter(cb)
    assert cb not in r._on_refresh_broken_enter
    # Idempotent unregister.
    r.unregister_on_refresh_broken_enter(cb)


# ── (3) Worker._on_refresh_broken_enter dedup gate ────────────────


class _StubLoop:
    """Minimal asyncio.create_task stand-in to validate dedup
    semantics without spinning a real event loop."""
    def __init__(self):
        self.calls = 0
        self.tasks = []

    def create_task(self, coro):
        self.calls += 1
        self.tasks.append(coro)
        # Close the coro so it doesn't warn "never awaited."
        coro.close()
        return None


def test_worker_dedup_gate_fires_once(monkeypatch):
    from puffo_agent.portal import worker as worker_module

    stub_loop = _StubLoop()
    monkeypatch.setattr(
        worker_module.asyncio, "create_task", stub_loop.create_task,
    )

    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent"})()
        _client = None
        _refresh_broken_notification_sent = False

        _on_refresh_broken_enter = (
            worker_module.Worker._on_refresh_broken_enter
        )
        _notify_operator_of_refresh_broken = (
            worker_module.Worker._notify_operator_of_refresh_broken
        )

    w = _StubWorker()
    w._on_refresh_broken_enter()
    w._on_refresh_broken_enter()
    w._on_refresh_broken_enter()

    assert stub_loop.calls == 1
    assert w._refresh_broken_notification_sent is True


def test_worker_reset_arms_next_notify(monkeypatch):
    """Dedup resets on refresh-success (daemon.on_refresh_success).
    Subsequent ENTER re-fires — symmetric to auth_failed."""
    from puffo_agent.portal import worker as worker_module

    stub_loop = _StubLoop()
    monkeypatch.setattr(
        worker_module.asyncio, "create_task", stub_loop.create_task,
    )

    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent"})()
        _client = None
        _refresh_broken_notification_sent = False

        _on_refresh_broken_enter = (
            worker_module.Worker._on_refresh_broken_enter
        )
        _notify_operator_of_refresh_broken = (
            worker_module.Worker._notify_operator_of_refresh_broken
        )

    w = _StubWorker()
    w._on_refresh_broken_enter()                 # fires 1
    assert stub_loop.calls == 1

    # Simulate refresh-success → daemon.on_refresh_success resets.
    w._refresh_broken_notification_sent = False
    w._on_refresh_broken_enter()                 # fires 2
    assert stub_loop.calls == 2


# ── (4) _notify_operator_of_refresh_broken client guards ─────────


def test_notify_skipped_when_client_not_warm(caplog):
    """No PuffoCoreMessageClient yet → log + return cleanly."""
    from puffo_agent.portal import worker as worker_module

    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent", "display_name": ""})()
        _client = None
        _refresh_broken_notification_sent = True  # gated; clear-and-log

    w = _StubWorker()
    coro = worker_module.Worker._notify_operator_of_refresh_broken(w)
    with caplog.at_level(logging.WARNING, logger="puffo_agent.portal.worker"):
        asyncio.new_event_loop().run_until_complete(coro)
    assert any("client not yet warm" in r.message for r in caplog.records)
    # Dedup re-armed so a later ENTER retries.
    assert w._refresh_broken_notification_sent is False


def test_notify_skipped_when_operator_slug_empty(caplog):
    """Operator-less agents skip cleanly with a warning — red-dot UI
    is the only fallback signal. Dedup stays gated to avoid respin."""
    from puffo_agent.portal import worker as worker_module

    class _StubClient:
        operator_slug = ""

    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent", "display_name": ""})()
        _client = _StubClient()
        _refresh_broken_notification_sent = True

    w = _StubWorker()
    coro = worker_module.Worker._notify_operator_of_refresh_broken(w)
    with caplog.at_level(logging.WARNING, logger="puffo_agent.portal.worker"):
        asyncio.new_event_loop().run_until_complete(coro)
    assert any("no operator_slug" in r.message for r in caplog.records)
    # Dedup stays set — no respin.
    assert w._refresh_broken_notification_sent is True


def test_notify_sends_dm_when_operator_slug_set():
    """Happy path: client.operator_slug populated → _send_dm called
    with the bilingual refresh_broken copy + operator's slug."""
    from puffo_agent.portal import worker as worker_module

    captured: dict = {}

    class _StubClient:
        operator_slug = "@han-0001"

        async def _send_dm(self, recipient, text, root_id):
            captured["recipient"] = recipient
            captured["text"] = text
            captured["root_id"] = root_id
            return {"envelope_id": "env-fake"}

    class _StubWorker:
        agent_cfg = type(
            "A", (), {"id": "t-agent", "display_name": "Planner"},
        )()
        _client = _StubClient()
        _refresh_broken_notification_sent = True

    w = _StubWorker()
    coro = worker_module.Worker._notify_operator_of_refresh_broken(w)
    asyncio.new_event_loop().run_until_complete(coro)

    assert captured["recipient"] == "@han-0001"
    assert "credential refresh is broken" in captured["text"]
    assert "凭证刷新失败" in captured["text"]
    assert "**Planner**" in captured["text"]
    assert captured["root_id"] == ""
    # Dedup remains set so a re-fire requires explicit reset.
    assert w._refresh_broken_notification_sent is True


# ── (5) Daemon-side wiring: agent_id filter + unregister + codex ──


def _make_daemon_stub_pair(monkeypatch):
    """Build (StubDaemon, claude_refresher, codex_refresher) with the
    real ``_register_with_refresher`` and ``_stop_worker`` bound so we
    can pin the load-bearing wiring without spinning a real refresher.
    """
    from puffo_agent.portal import daemon as daemon_module

    captured = {"agent_homes": [], "refresh_success_cbs": [], "refresh_broken_cbs": []}

    class _StubRefresher:
        def __init__(self, name):
            self.name = name
            self.success_cbs: list = []
            self.broken_cbs: list = []
            self.agents: set = set()

        def register_agent(self, path):
            self.agents.add(path)

        def unregister_agent(self, path):
            self.agents.discard(path)

        def register_on_refresh_success(self, cb):
            self.success_cbs.append(cb)

        def unregister_on_refresh_success(self, cb):
            try:
                self.success_cbs.remove(cb)
            except ValueError:
                pass

        def register_on_refresh_broken_enter(self, cb):
            self.broken_cbs.append(cb)

        def unregister_on_refresh_broken_enter(self, cb):
            try:
                self.broken_cbs.remove(cb)
            except ValueError:
                pass

    claude_r = _StubRefresher("claude")
    codex_r = _StubRefresher("codex")

    class _StubDaemon:
        refresher = claude_r
        codex_refresher = codex_r
        workers: dict = {}

        def _refresher_for(self, cfg):
            if (getattr(cfg.runtime, "harness", "") or "claude-code") == "codex":
                return self.codex_refresher
            return self.refresher

        _register_with_refresher = daemon_module.Daemon._register_with_refresher

    # ``agent_home_dir`` is called inside _register_with_refresher.
    monkeypatch.setattr(
        daemon_module, "agent_home_dir", lambda agent_id: f"/tmp/agents/{agent_id}",
    )
    return _StubDaemon(), claude_r, codex_r, captured


def _make_agent_cfg(agent_id: str, harness: str = "claude-code"):
    cfg = type("Cfg", (), {})()
    cfg.id = agent_id
    cfg.runtime = type("R", (), {"harness": harness})()
    return cfg


def _make_worker_for_daemon_test():
    from puffo_agent.portal.state import RuntimeState

    fired: list[int] = []

    class W:
        runtime = RuntimeState(status="running", started_at=0, msg_count=0)
        _auth_failed_notification_sent = True
        _refresh_broken_notification_sent = True
        _refresh_success_callback = None
        _refresh_broken_callback = None

        def _on_refresh_broken_enter(self):
            fired.append(1)

    return W(), fired


def test_daemon_filter_only_dms_own_agent(monkeypatch):
    """The load-bearing fan-out scope: worker-A's refresh-broken-enter
    handler must NOT fire when CredentialRefresher reports agent-B
    broke. Without the ``flipped_agent_id == agent_id`` filter at
    daemon.py, every worker would DM the operator for every other
    agent's break."""
    d, claude_r, _, _ = _make_daemon_stub_pair(monkeypatch)

    w_a, fired_a = _make_worker_for_daemon_test()
    w_b, fired_b = _make_worker_for_daemon_test()
    cfg_a = _make_agent_cfg("agent-a")
    cfg_b = _make_agent_cfg("agent-b")

    d._register_with_refresher(cfg_a, w_a)
    d._register_with_refresher(cfg_b, w_b)
    assert len(claude_r.broken_cbs) == 2

    # CredentialRefresher fires the agent-A break.
    for cb in claude_r.broken_cbs:
        cb("agent-a")

    assert fired_a == [1]
    assert fired_b == []  # The load-bearing assert.


def test_daemon_stop_worker_unregisters_refresh_broken_callback(monkeypatch):
    """``_stop_worker`` must unregister the refresh-broken-enter
    callback from BOTH refreshers so a stopped worker stops receiving
    events. Mirrors the existing ``_refresh_success_callback``
    unregister pattern."""
    from puffo_agent.portal import daemon as daemon_module

    d, claude_r, codex_r, _ = _make_daemon_stub_pair(monkeypatch)
    w, _ = _make_worker_for_daemon_test()
    cfg = _make_agent_cfg("agent-x")
    d._register_with_refresher(cfg, w)

    assert len(claude_r.broken_cbs) == 1
    assert w._refresh_broken_callback is not None

    # Inline the unregister body from _stop_worker (the rest is
    # async + touches files; we just need the callback teardown).
    rb_cb = getattr(w, "_refresh_broken_callback", None)
    assert rb_cb is not None
    d.refresher.unregister_on_refresh_broken_enter(rb_cb)
    d.codex_refresher.unregister_on_refresh_broken_enter(rb_cb)
    assert claude_r.broken_cbs == []
    assert codex_r.broken_cbs == []


def test_daemon_codex_agent_registers_on_codex_refresher(monkeypatch):
    """A codex-harness agent must register its refresh-broken
    callback on the codex_refresher, not the Claude refresher.
    ``_refresher_for`` routes based on ``runtime.harness``."""
    d, claude_r, codex_r, _ = _make_daemon_stub_pair(monkeypatch)

    w, _ = _make_worker_for_daemon_test()
    cfg = _make_agent_cfg("agent-codex", harness="codex")
    d._register_with_refresher(cfg, w)

    assert len(codex_r.broken_cbs) == 1
    assert len(claude_r.broken_cbs) == 0


# ── (6) Cross-state dedup re-arm + post-re-arm-fires ──────────────


def test_on_refresh_success_rearms_refresh_broken_flag_even_when_auth_failed(
    monkeypatch,
):
    """``on_refresh_success`` re-arms BOTH flags unconditionally,
    even when the prior state was ``auth_failed`` (not refresh_broken).
    The re-arm is benign (the flag is a gate, not a trigger) but worth
    pinning so a future "only re-arm if was-broken" optimization
    doesn't silently break the dedup invariants."""
    from puffo_agent.portal.state import RuntimeState

    d, claude_r, _, _ = _make_daemon_stub_pair(monkeypatch)

    class W:
        runtime = RuntimeState(status="running", started_at=0, msg_count=0)
        _auth_failed_notification_sent = True
        _refresh_broken_notification_sent = True
        _refresh_success_callback = None
        _refresh_broken_callback = None

        def _on_refresh_broken_enter(self):
            pass

    w = W()
    cfg = _make_agent_cfg("agent-mixed")
    # Prior state was auth_failed, not refresh_broken.
    w.runtime.health = "auth_failed"
    d._register_with_refresher(cfg, w)
    assert claude_r.success_cbs, "daemon should register on_refresh_success"

    # Fire the success callback as the daemon would.
    claude_r.success_cbs[0]()

    # BOTH flags re-armed even though only auth_failed was active.
    assert w._auth_failed_notification_sent is False
    assert w._refresh_broken_notification_sent is False


def test_dedup_rearm_then_subsequent_fire_actually_schedules(monkeypatch):
    """The transient-recovery contract: after a no-warm-client (or
    failed-send) re-arms the dedup, the NEXT ENTER must actually
    schedule the DM. Pin the 2-step fail→succeed sequence so the
    re-arm path's recovery value isn't silently lost."""
    from puffo_agent.portal import worker as worker_module

    stub_loop = _StubLoop()
    monkeypatch.setattr(
        worker_module.asyncio, "create_task", stub_loop.create_task,
    )

    class W:
        agent_cfg = type("A", (), {"id": "t-agent"})()
        _client = None  # not warm yet
        _refresh_broken_notification_sent = False

        _on_refresh_broken_enter = (
            worker_module.Worker._on_refresh_broken_enter
        )
        _notify_operator_of_refresh_broken = (
            worker_module.Worker._notify_operator_of_refresh_broken
        )

    w = W()
    # First ENTER schedules; the async body sees _client=None and
    # re-arms the dedup flag back to False.
    w._on_refresh_broken_enter()
    assert stub_loop.calls == 1
    # Simulate the no-warm-client async re-arm.
    asyncio.new_event_loop().run_until_complete(
        worker_module.Worker._notify_operator_of_refresh_broken(w)
    )
    assert w._refresh_broken_notification_sent is False

    # Second ENTER (after re-arm) MUST schedule again — load-bearing
    # for the transient-recovery path.
    w._on_refresh_broken_enter()
    assert stub_loop.calls == 2
    assert w._refresh_broken_notification_sent is True
