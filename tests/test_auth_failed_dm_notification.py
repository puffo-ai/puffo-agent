"""PUF-283: proactive OAuth-expired DM to operator on
``runtime.health = auth_failed`` ENTER. Fire-once-per-agent-per-
session, reset on auth_failed CLEAR.

Tests four invariants:
  1. The bilingual ``format_oauth_expired`` copy contains both
     English + Chinese strands + concrete recovery instructions.
  2. ``_handle_suppressed_reply`` fires ``on_auth_failed_enter``
     only on was-ok → auth_failed transition (NOT re-entry).
  3. ``Worker._on_auth_failed_enter`` is dedup-gated by
     ``_auth_failed_notification_sent``.
  4. ``daemon.on_refresh_success`` reset arms the next ENTER.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent._invite_strings import format_oauth_expired
from puffo_agent.portal.state import RuntimeState
from puffo_agent.portal.worker import _handle_suppressed_reply


# ── (1) format_oauth_expired bilingual copy ────────────────────────


def test_oauth_copy_includes_english_and_chinese():
    text = format_oauth_expired("planner-1234", "Planner")
    # English strand: `claude auth login` + send-a-message (auto-resume)
    assert "Claude OAuth expired" in text
    assert "claude auth login" in text
    assert "send me a message" in text
    assert "agent resume" not in text   # no manual resume step anymore
    # Chinese strand
    assert "Claude OAuth 已过期" in text
    assert "发条消息" in text
    # Bold display name format
    assert "**Planner**" in text


def test_oauth_copy_degrades_when_display_name_missing():
    text = format_oauth_expired("agent-5678", "")
    # Both strands still present
    assert "Claude OAuth expired" in text
    assert "Claude OAuth 已过期" in text
    # Backtick-id (no bold display name) — degrades cleanly
    assert "`agent-5678`" in text
    # No empty-bold artifact
    assert "**" not in text or "**`agent-5678`**" in text


# ── (2) _handle_suppressed_reply on_auth_failed_enter edge ─────────


def _make_runtime(health: str = "ok") -> RuntimeState:
    rt = RuntimeState(status="running", started_at=0, msg_count=0)
    rt.health = health
    return rt


def test_on_auth_failed_enter_fires_on_fresh_transition(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rt = _make_runtime("ok")
    fired: list[int] = []

    def cb():
        fired.append(1)

    suppressed, _ = _handle_suppressed_reply(
        "Not logged in · Please run /login",
        rt,
        "t-agent",
        scope="fallback",
        on_auth_failed_enter=cb,
    )
    assert suppressed is True
    assert rt.health == "auth_failed"
    assert fired == [1]


def test_on_auth_failed_enter_does_NOT_fire_on_re_entry(tmp_path, monkeypatch):
    """Second 401 on an already auth_failed runtime should NOT fire
    the ENTER callback. This is the operator's load-bearing
    "no message storm" invariant."""
    monkeypatch.chdir(tmp_path)
    rt = _make_runtime("auth_failed")
    fired: list[int] = []

    def cb():
        fired.append(1)

    suppressed, _ = _handle_suppressed_reply(
        "OAuth token revoked",
        rt,
        "t-agent",
        scope="fallback",
        on_auth_failed_enter=cb,
    )
    assert suppressed is True
    assert rt.health == "auth_failed"
    assert fired == []  # dedup: no notification on re-entry


def test_on_auth_failed_enter_silent_when_clean_reply(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rt = _make_runtime("ok")
    fired: list[int] = []

    def cb():
        fired.append(1)

    suppressed, _ = _handle_suppressed_reply(
        "Hello, world.",
        rt,
        "t-agent",
        scope="fallback",
        on_auth_failed_enter=cb,
    )
    assert suppressed is False
    assert rt.health == "ok"
    assert fired == []


def test_on_auth_failed_enter_callback_exception_does_not_crash(
    tmp_path, monkeypatch,
):
    """If the DM-task-create callback raises, the suppression flow
    still completes — operator DM is best-effort, runtime state
    update is load-bearing."""
    monkeypatch.chdir(tmp_path)
    rt = _make_runtime("ok")

    def cb():
        raise RuntimeError("loop closed")

    suppressed, _ = _handle_suppressed_reply(
        "Not logged in · Please run /login",
        rt,
        "t-agent",
        scope="fallback",
        on_auth_failed_enter=cb,
    )
    assert suppressed is True
    assert rt.health == "auth_failed"


# ── (3) Worker._on_auth_failed_enter dedup gate ────────────────────


class _StubLoop:
    """Stand-in for asyncio.create_task that records the call but
    doesn't actually schedule. Used to verify the dedup gate
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
        _auth_failed_notification_sent = False

        _on_auth_failed_enter = worker_module.Worker._on_auth_failed_enter
        _notify_operator_of_auth_failed_oauth = (
            worker_module.Worker._notify_operator_of_auth_failed_oauth
        )

    w = _StubWorker()
    w._on_auth_failed_enter()
    w._on_auth_failed_enter()
    w._on_auth_failed_enter()

    assert stub_loop.calls == 1
    assert w._auth_failed_notification_sent is True


def test_worker_reset_arms_next_notify(monkeypatch):
    """The intake's load-bearing semantics: dedup resets on
    auth_failed CLEAR (daemon.on_refresh_success). Subsequent ENTER
    re-fires."""
    from puffo_agent.portal import worker as worker_module

    stub_loop = _StubLoop()
    monkeypatch.setattr(
        worker_module.asyncio, "create_task", stub_loop.create_task,
    )

    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent"})()
        _client = None
        _auth_failed_notification_sent = False

        _on_auth_failed_enter = worker_module.Worker._on_auth_failed_enter
        _notify_operator_of_auth_failed_oauth = (
            worker_module.Worker._notify_operator_of_auth_failed_oauth
        )

    w = _StubWorker()
    w._on_auth_failed_enter()                 # fires 1
    assert stub_loop.calls == 1

    # Simulate refresh-success → daemon.on_refresh_success resets.
    w._auth_failed_notification_sent = False
    w._on_auth_failed_enter()                 # fires 2
    assert stub_loop.calls == 2


# ── (4) _notify_operator_of_auth_failed_oauth client guards ────────


import asyncio


def test_notify_skipped_when_client_not_warm(tmp_path, monkeypatch, caplog):
    """No PuffoCoreMessageClient yet (warm() hasn't completed) →
    log + return cleanly."""
    import logging
    from puffo_agent.portal import worker as worker_module

    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent", "display_name": ""})()
        _client = None

    w = _StubWorker()
    coro = worker_module.Worker._notify_operator_of_auth_failed_oauth(w)
    with caplog.at_level(logging.WARNING, logger="puffo_agent.portal.worker"):
        asyncio.new_event_loop().run_until_complete(coro)
    assert any("client not yet warm" in r.message for r in caplog.records)


def test_notify_skipped_when_operator_slug_empty(tmp_path, monkeypatch, caplog):
    """Operator-less agents (early provisioning) skip cleanly with a
    warning — red-dot UI is the only fallback signal."""
    import logging
    from puffo_agent.portal import worker as worker_module

    class _StubClient:
        operator_slug = ""

    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent", "display_name": ""})()
        _client = _StubClient()

    w = _StubWorker()
    coro = worker_module.Worker._notify_operator_of_auth_failed_oauth(w)
    with caplog.at_level(logging.WARNING, logger="puffo_agent.portal.worker"):
        asyncio.new_event_loop().run_until_complete(coro)
    assert any("no operator_slug" in r.message for r in caplog.records)


def test_notify_sends_dm_when_operator_slug_set(tmp_path, monkeypatch):
    """Happy path: client.operator_slug populated → _send_dm called
    with the bilingual copy + the operator's slug."""
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

    w = _StubWorker()
    coro = worker_module.Worker._notify_operator_of_auth_failed_oauth(w)
    asyncio.new_event_loop().run_until_complete(coro)

    assert captured["recipient"] == "@han-0001"
    assert captured["root_id"] == ""
    assert "Claude OAuth expired" in captured["text"]
    assert "Claude OAuth 已过期" in captured["text"]
    assert "claude auth login" in captured["text"]


def test_notify_swallows_send_dm_exception(monkeypatch, caplog):
    """A crashing _send_dm must not bubble out of the
    auth-failed-notify path — the daemon stays up."""
    import logging
    from puffo_agent.portal import worker as worker_module

    class _StubClient:
        operator_slug = "@han-0001"

        async def _send_dm(self, recipient, text, root_id):
            raise RuntimeError("network down")

    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent", "display_name": ""})()
        _client = _StubClient()

    w = _StubWorker()
    coro = worker_module.Worker._notify_operator_of_auth_failed_oauth(w)
    with caplog.at_level(logging.ERROR, logger="puffo_agent.portal.worker"):
        asyncio.new_event_loop().run_until_complete(coro)
    assert any("auth-failed DM" in r.message for r in caplog.records)


# ── PR #70 polish folds ────────────────────────────────────────────


def test_create_task_failure_broadly_caught(monkeypatch):
    """PR #70 nit #3: any failure to schedule the async DM (not just
    ``RuntimeError``) should reset the dedup flag so the next ENTER
    re-tries. Otherwise the flag stays stuck-True until a refresh
    arrives, masking a legitimate retry opportunity."""
    from puffo_agent.portal import worker as worker_module

    def crash(_coro):
        # close coro so it doesn't warn "never awaited"
        try:
            _coro.close()
        except Exception:
            pass
        raise OSError("unexpected scheduler failure")

    monkeypatch.setattr(worker_module.asyncio, "create_task", crash)

    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent"})()
        _client = None
        _auth_failed_notification_sent = False

        _on_auth_failed_enter = worker_module.Worker._on_auth_failed_enter
        _notify_operator_of_auth_failed_oauth = (
            worker_module.Worker._notify_operator_of_auth_failed_oauth
        )

    w = _StubWorker()
    w._on_auth_failed_enter()
    # Flag reset so a subsequent ENTER tries again instead of
    # silently swallowing.
    assert w._auth_failed_notification_sent is False


def test_workers_have_independent_dedup_flags(monkeypatch):
    """PR #70 nit #4: a Worker's ``_auth_failed_notification_sent``
    is instance state; one agent's ENTER must not silence another's."""
    from puffo_agent.portal import worker as worker_module

    stub_loop = _StubLoop()
    monkeypatch.setattr(
        worker_module.asyncio, "create_task", stub_loop.create_task,
    )

    class _StubWorker:
        _client = None
        _auth_failed_notification_sent = False

        _on_auth_failed_enter = worker_module.Worker._on_auth_failed_enter
        _notify_operator_of_auth_failed_oauth = (
            worker_module.Worker._notify_operator_of_auth_failed_oauth
        )

        def __init__(self, agent_id: str):
            self.agent_cfg = type("A", (), {"id": agent_id})()
            self._auth_failed_notification_sent = False

    w_a = _StubWorker("agent-a")
    w_b = _StubWorker("agent-b")

    w_a._on_auth_failed_enter()
    w_b._on_auth_failed_enter()

    # Both fired exactly once; the flag is per-instance.
    assert stub_loop.calls == 2
    assert w_a._auth_failed_notification_sent is True
    assert w_b._auth_failed_notification_sent is True


def test_oauth_copy_quotes_agent_id_for_markdown_safety():
    """PR #70 nit #5: ``agent_id`` is interpolated into a Markdown DM;
    backtick-wrapping it (which is what the helper does today) is the
    load-bearing defense against a stray ``*``/``_``/``[`` slipping
    through and breaking the rendered recovery instruction. Slugs are
    [a-z0-9-] today so injection is near-zero risk, but pinning the
    contract avoids future regression if the slug regex changes."""
    text = format_oauth_expired("a-b-c", "")
    # agent_id is backtick-wrapped in the label so a stray markdown char
    # can't break the rendered DM.
    assert "`a-b-c`" in text


def test_daemon_on_refresh_success_resets_dedup(monkeypatch):
    """PR #70 nit #1: the daemon's refresh-success closure resets
    ``worker._auth_failed_notification_sent``. The pieces are
    unit-tested individually; this pins the wiring between
    ``daemon._register_with_refresher`` and Worker."""
    from puffo_agent.portal import daemon as daemon_module
    from puffo_agent.portal.state import RuntimeState

    class _StubRefresher:
        """Captures the ``on_refresh_success`` callback the daemon
        registers without actually wiring the refresh loop."""
        def __init__(self):
            self.callback = None
            self.refresh_broken_callback = None

        def register_agent(self, _path):
            pass

        def register_on_refresh_success(self, cb):
            self.callback = cb

        # PUF-303: daemon now also registers a refresh-broken-enter
        # callback. Capture for completeness; not asserted here.
        def register_on_refresh_broken_enter(self, cb):
            self.refresh_broken_callback = cb

    class _StubAgentCfg:
        id = "t-agent"

        class runtime:
            harness = "claude-code"

        class puffo_core:
            slug = "alice-0001"

    class _StubWorker:
        agent_cfg = _StubAgentCfg()
        runtime = RuntimeState(status="running", started_at=0, msg_count=0)
        _auth_failed_notification_sent = True
        _refresh_broken_notification_sent = True
        _refresh_success_callback = None
        _refresh_broken_callback = None

    class _StubDaemon:
        refresher = _StubRefresher()
        codex_refresher = _StubRefresher()

        def _refresher_for(self, _cfg):
            return self.refresher

        _register_with_refresher = daemon_module.Daemon._register_with_refresher

    d = _StubDaemon()
    w = _StubWorker()
    # Simulate auth_failed → refresh_success → expect both
    # ``runtime.health`` cleared AND ``_auth_failed_notification_sent``
    # reset, so the next ENTER re-notifies.
    w.runtime.health = "auth_failed"
    d._register_with_refresher(w.agent_cfg, w)
    assert d.refresher.callback is not None
    d.refresher.callback()
    assert w.runtime.health == "ok"
    assert w._auth_failed_notification_sent is False


# ── re-arm on a transient failed send (PR #70 review) ──────────────


def _run_notify(worker_obj):
    from puffo_agent.portal import worker as worker_module
    coro = worker_module.Worker._notify_operator_of_auth_failed_oauth(worker_obj)
    asyncio.new_event_loop().run_until_complete(coro)


def test_notify_rearms_dedup_when_client_not_warm():
    """Client not yet warm at send time → no DM went out, so re-arm the
    flag for the next ENTER instead of staying silently gated."""
    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent", "display_name": ""})()
        _client = None
        _auth_failed_notification_sent = True

    w = _StubWorker()
    _run_notify(w)
    assert w._auth_failed_notification_sent is False


def test_notify_rearms_dedup_when_send_dm_raises():
    class _StubClient:
        operator_slug = "@han-0001"

        async def _send_dm(self, recipient, text, root_id):
            raise RuntimeError("network down")

    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent", "display_name": ""})()
        _client = _StubClient()
        _auth_failed_notification_sent = True

    w = _StubWorker()
    _run_notify(w)
    assert w._auth_failed_notification_sent is False


def test_notify_stays_gated_when_no_operator_slug():
    """No operator is a permanent config gap — stay gated so we don't
    respin a task on every 401."""
    class _StubClient:
        operator_slug = ""

    class _StubWorker:
        agent_cfg = type("A", (), {"id": "t-agent", "display_name": ""})()
        _client = _StubClient()
        _auth_failed_notification_sent = True

    w = _StubWorker()
    _run_notify(w)
    assert w._auth_failed_notification_sent is True
