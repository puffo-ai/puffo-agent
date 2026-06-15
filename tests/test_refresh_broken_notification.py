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
