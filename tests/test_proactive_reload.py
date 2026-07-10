"""Proactive (between-turns) in-process profile reload for the CLOUD agent.

A ``profile.md`` / host-sync / session edit takes effect **proactively**
— while the agent is IDLE, target < 500 ms — by running the EXISTING
turn-start reload primitive (``_process_refresh_flags`` → ``adapter.reload``)
between turns and on ``SIGHUP``, instead of only lazily at the next turn.

These tests drive the REAL watcher code (``Worker._refresh_watcher_loop`` /
``Worker._proactive_refresh_tick``) and the REAL reload primitive
(``_process_refresh_flags`` + ``_rebuild_managed_system_prompt``) with a
fake adapter + fake puffo + real temp flag files. The bridge WS lives on
``Worker._client``; the reload is adapter-only, so WS-preservation /
no-restart is true by construction — the tests guard it.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal import daemon as daemon_mod
from puffo_agent.portal import worker as worker_mod
from puffo_agent.portal.daemon import Daemon, _install_posix_sighup_handler
from puffo_agent.portal.runtime_matrix import RUNTIME_CLI_LOCAL
from puffo_agent.portal.state import AgentConfig, DaemonConfig, RuntimeState
from puffo_agent.portal.worker import Worker, _process_refresh_flags


# ── fakes ──────────────────────────────────────────────────────────────────


class _FakeAdapter:
    """Records reload/warm/close so tests can prove the proactive path
    is adapter-``reload``-only (no warm, no close, no restart)."""

    def __init__(self):
        self.reload_calls: list[tuple[str, bool]] = []
        self.warm_calls = 0
        self.close_calls = 0

    async def reload(self, new_system_prompt, *, with_session=False):
        self.reload_calls.append((new_system_prompt, with_session))

    async def warm(self, system_prompt):
        self.warm_calls += 1

    async def aclose(self):
        self.close_calls += 1


class _FakePuffo:
    def __init__(self, prompt="old prompt"):
        self.system_prompt = prompt


class _FakeBridgeClient:
    """Stand-in for the bridge WS handle on ``Worker._client``. Any
    close / stop / reconnect call is recorded so the tests can assert
    the proactive reload never touches it."""

    def __init__(self):
        self.close_calls = 0
        self.stop_calls = 0
        self.connect_calls = 0
        self.listen_calls = 0

    async def stop(self):
        self.stop_calls += 1

    async def aclose(self):
        self.close_calls += 1

    async def close(self):
        self.close_calls += 1

    async def connect(self):
        self.connect_calls += 1

    async def listen(self, *a, **k):
        self.listen_calls += 1


# ── builders ────────────────────────────────────────────────────────────────


def _make_worker() -> Worker:
    """A cloud-shaped worker: ``runtime.kind = cli-local`` +
    ``puffo_core.transport = bridge``. Mirrors the constructor pattern in
    test_worker_pre_delivery_token_check."""
    cfg = MagicMock(spec=AgentConfig)
    cfg.id = "t"
    cfg.runtime = MagicMock()
    cfg.runtime.kind = RUNTIME_CLI_LOCAL
    cfg.runtime.harness = "claude-code"
    cfg.display_name = "Test"
    cfg.role = "Tester"
    cfg.role_short = "test"
    cfg.puffo_core = MagicMock()
    cfg.puffo_core.transport = "bridge"
    w = Worker(DaemonConfig(), cfg)
    w.runtime = RuntimeState(status="running")
    return w


def _reload_env(tmp_path, adapter, puffo, monkeypatch, marker="PROACTIVE_MARKER_v2"):
    """Real temp profile.md + flag files + an ``apply`` closure bound over
    the REAL ``_process_refresh_flags`` (exactly what ``_run`` binds for
    the watcher). ``HOME`` / ``PUFFO_AGENT_HOME`` are redirected to temp
    dirs so the host-sync branch and prompt rebuild stay hermetic."""
    home = tmp_path / "agent_home"
    home.mkdir()
    host_home = tmp_path / "host_home"
    host_home.mkdir()
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(home))
    monkeypatch.setenv("HOME", str(host_home))

    profile = tmp_path / "profile.md"
    profile.write_text(f"{marker} — the new profile body", encoding="utf-8")
    shared = tmp_path / "shared"
    shared.mkdir()
    mem = tmp_path / "memory"
    mem.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()

    pa = tmp_path / ".puffo-agent"
    pa.mkdir()
    agent_flag = pa / "refresh_agent.flag"
    host_flag = pa / "refresh_host_sync.flag"
    session_flag = pa / "refresh_session.flag"
    flag_paths = (agent_flag, host_flag, session_flag)

    async def apply():
        await _process_refresh_flags(
            agent_id="t",
            harness_name="claude-code",
            shared_path=shared,
            profile_path=str(profile),
            memory_path=str(mem),
            workspace_path=str(ws),
            display_name="Test",
            role="Tester",
            role_short="test",
            puffo=puffo,
            adapter=adapter,
            refresh_agent_flag=agent_flag,
            refresh_host_sync_flag=host_flag,
            refresh_session_flag=session_flag,
        )

    return types.SimpleNamespace(
        profile=profile,
        agent_flag=agent_flag,
        host_flag=host_flag,
        session_flag=session_flag,
        flag_paths=flag_paths,
        apply=apply,
        marker=marker,
    )


# ── criterion 2: idle-proactive apply (no inbound message) ───────────────────


@pytest.mark.asyncio
async def test_idle_proactive_apply_no_message(tmp_path, monkeypatch):
    """No message arrives: writing profile.md + dropping the flags and
    running one watcher tick rebuilds the prompt from disk, calls
    adapter.reload exactly once, and unlinks all three flags."""
    adapter = _FakeAdapter()
    puffo = _FakePuffo()
    env = _reload_env(tmp_path, adapter, puffo, monkeypatch)

    # Spy on the REAL rebuild so we prove (a) it ran AND (b) the new
    # profile.md content actually flows into puffo.system_prompt.
    orig_rebuild = worker_mod._rebuild_managed_system_prompt
    rebuild_calls: list[dict] = []

    def spy_rebuild(**kw):
        rebuild_calls.append(kw)
        return orig_rebuild(**kw)

    monkeypatch.setattr(worker_mod, "_rebuild_managed_system_prompt", spy_rebuild)

    # Drop all three flags — NO inbound message / on_message_batch.
    env.agent_flag.write_text("{}", encoding="utf-8")
    env.host_flag.write_text("{}", encoding="utf-8")
    env.session_flag.write_text("{}", encoding="utf-8")

    worker = _make_worker()
    applied = await worker._proactive_refresh_tick(env.flag_paths, env.apply)

    assert applied is True
    # (a) rebuild ran and the new profile.md content is in the prompt.
    assert rebuild_calls, "_rebuild_managed_system_prompt did not run"
    assert env.marker in puffo.system_prompt
    # (b) adapter.reload called exactly once.
    assert len(adapter.reload_calls) == 1
    # (c) all three flag files unlinked.
    assert not env.agent_flag.exists()
    assert not env.host_flag.exists()
    assert not env.session_flag.exists()


# ── criterion 3: latency < 500 ms via the real watcher loop ──────────────────


@pytest.mark.asyncio
async def test_proactive_reload_latency_under_500ms(tmp_path, monkeypatch):
    """The real ``_refresh_watcher_loop`` (production 0.25 s poll) applies
    a flag written while idle in well under 500 ms."""
    adapter = _FakeAdapter()
    puffo = _FakePuffo()
    env = _reload_env(tmp_path, adapter, puffo, monkeypatch)
    worker = _make_worker()

    applied_at: dict[str, float] = {}

    async def apply_and_stop():
        await env.apply()
        applied_at["t"] = time.monotonic()
        worker._stop.set()  # let the loop exit after the first apply

    # Default interval is the production 0.25 s; drive the real loop.
    task = asyncio.ensure_future(
        worker._refresh_watcher_loop(env.flag_paths, apply_and_stop)
    )
    await asyncio.sleep(0)  # let the loop enter its poll wait
    t0 = time.monotonic()
    env.agent_flag.write_text("{}", encoding="utf-8")
    await asyncio.wait_for(task, timeout=3.0)

    assert len(adapter.reload_calls) == 1
    assert applied_at["t"] - t0 < 0.5, applied_at["t"] - t0


# ── criterion 4: WS-preserving / no restart (cloud config) ───────────────────


@pytest.mark.asyncio
async def test_ws_preserving_no_restart(tmp_path, monkeypatch):
    """Proactive reload on a cli-local + bridge worker keeps the adapter
    and the bridge/WS handle identical, re-runs neither warm nor build,
    and never re-invokes _run/start."""
    adapter = _FakeAdapter()
    puffo = _FakePuffo()
    env = _reload_env(tmp_path, adapter, puffo, monkeypatch)
    worker = _make_worker()

    # Cloud config sanity.
    assert worker.agent_cfg.runtime.kind == RUNTIME_CLI_LOCAL
    assert worker.agent_cfg.puffo_core.transport == "bridge"

    ws = _FakeBridgeClient()
    worker._client = ws
    worker._adapter = adapter
    adapter_before = worker._adapter
    client_before = worker._client

    # Guards: the proactive path must not rebuild the adapter or restart
    # the worker. None of these should ever be reached.
    build_calls: list = []
    run_calls: list = []
    start_calls: list = []
    monkeypatch.setattr(
        worker_mod, "build_adapter", lambda *a, **k: build_calls.append(1)
    )
    monkeypatch.setattr(Worker, "_run", lambda self: run_calls.append(1))
    monkeypatch.setattr(Worker, "start", lambda self: start_calls.append(1))

    env.agent_flag.write_text("{}", encoding="utf-8")
    applied = await worker._proactive_refresh_tick(env.flag_paths, env.apply)

    assert applied is True
    assert len(adapter.reload_calls) == 1
    # Same adapter object; warm NOT re-called; adapter never re-built.
    assert worker._adapter is adapter_before
    assert adapter.warm_calls == 0
    assert build_calls == []
    # Bridge/WS handle identity-unchanged and never closed / reconnected.
    assert worker._client is client_before
    assert ws.close_calls == 0
    assert ws.stop_calls == 0
    assert ws.connect_calls == 0
    assert ws.listen_calls == 0
    # Worker._run / start not re-invoked.
    assert run_calls == []
    assert start_calls == []


# ── criterion 5: no mid-turn apply / consume-once ────────────────────────────


@pytest.mark.asyncio
async def test_no_apply_mid_turn(tmp_path, monkeypatch):
    """A turn is active: the watcher defers — no reload, flag preserved
    for the turn-start path."""
    adapter = _FakeAdapter()
    puffo = _FakePuffo()
    env = _reload_env(tmp_path, adapter, puffo, monkeypatch)
    worker = _make_worker()

    worker._turn_active = True
    env.agent_flag.write_text("{}", encoding="utf-8")

    applied = await worker._proactive_refresh_tick(env.flag_paths, env.apply)

    assert applied is False
    assert adapter.reload_calls == []
    assert env.agent_flag.exists()  # deferred to turn-start


@pytest.mark.asyncio
async def test_consume_once_turn_start_and_watcher(tmp_path, monkeypatch):
    """Turn-start consumption and a concurrent watcher tick share the
    reload lock, so a single pending flag reloads EXACTLY once total."""
    adapter = _FakeAdapter()
    puffo = _FakePuffo()
    env = _reload_env(tmp_path, adapter, puffo, monkeypatch)
    worker = _make_worker()

    env.agent_flag.write_text("{}", encoding="utf-8")

    async def turn_start():
        # Mirrors on_message_batch: same lock, same primitive.
        async with worker._reload_lock:
            await env.apply()

    async def watcher_tick():
        await worker._proactive_refresh_tick(env.flag_paths, env.apply)

    await asyncio.gather(turn_start(), watcher_tick())

    assert len(adapter.reload_calls) == 1  # consume-once
    assert not env.agent_flag.exists()


# ── criterion 6: SIGHUP wiring + safe no-op ──────────────────────────────────


def test_sighup_handler_wakes_all_workers():
    """The installed SIGHUP handler wakes (notify_refresh → _refresh_now)
    every ``Daemon.workers`` entry."""
    daemon = Daemon.__new__(Daemon)  # avoid keychain/home side effects
    daemon.workers = {"a": _make_worker(), "b": _make_worker()}

    def handle_sighup():
        daemon.notify_refresh_all()

    class _RecordingLoop:
        def __init__(self):
            self.registered: list[tuple] = []

        def add_signal_handler(self, sig, cb):
            self.registered.append((sig, cb))

    loop = _RecordingLoop()
    installed = _install_posix_sighup_handler(loop, handle_sighup)

    # Main thread + real SIGHUP present → installs the handler.
    assert installed is True
    assert loop.registered == [(signal.SIGHUP, handle_sighup)]

    for w in daemon.workers.values():
        assert not w._refresh_now.is_set()

    # Fire the installed handler exactly as the signal would.
    _sig, registered_cb = loop.registered[0]
    registered_cb()

    for w in daemon.workers.values():
        assert w._refresh_now.is_set()


@pytest.mark.asyncio
async def test_watcher_applies_immediately_when_refresh_now_set(tmp_path, monkeypatch):
    """A watcher whose ``_refresh_now`` is set (as SIGHUP sets it) applies
    the pending flags immediately, without waiting the poll interval."""
    adapter = _FakeAdapter()
    puffo = _FakePuffo()
    env = _reload_env(tmp_path, adapter, puffo, monkeypatch)
    worker = _make_worker()

    env.agent_flag.write_text("{}", encoding="utf-8")
    worker.notify_refresh()  # what the SIGHUP fan-out does

    async def apply_and_stop():
        await env.apply()
        worker._stop.set()

    t0 = time.monotonic()
    # A long poll interval proves the apply is driven by _refresh_now, not
    # by the timeout.
    await asyncio.wait_for(
        worker._refresh_watcher_loop(env.flag_paths, apply_and_stop, interval=30.0),
        timeout=3.0,
    )
    elapsed = time.monotonic() - t0

    assert len(adapter.reload_calls) == 1
    assert elapsed < 1.0, elapsed  # immediate, not after the 30 s poll


def test_install_posix_sighup_handler_missing_sighup(monkeypatch):
    """No SIGHUP on the platform (e.g. Windows) → safe no-op / False."""
    fake_signal = types.SimpleNamespace()  # no SIGHUP attribute
    monkeypatch.setattr(daemon_mod, "signal", fake_signal)

    class _RecordingLoop:
        def __init__(self):
            self.registered: list = []

        def add_signal_handler(self, sig, cb):
            self.registered.append((sig, cb))

    loop = _RecordingLoop()
    assert _install_posix_sighup_handler(loop, lambda: None) is False
    assert loop.registered == []


def test_install_posix_sighup_handler_not_implemented():
    """Loop can't register signal handlers (e.g. Windows proactor) →
    NotImplementedError is swallowed, returns False."""

    class _RaisingLoop:
        def add_signal_handler(self, sig, cb):
            raise NotImplementedError

    # Runs on the main thread with a real SIGHUP present.
    assert _install_posix_sighup_handler(_RaisingLoop(), lambda: None) is False


def test_install_posix_sighup_handler_off_main_thread():
    """Off the main thread ``add_signal_handler`` → ``set_wakeup_fd``
    raises; the installer must skip (return False) without raising."""
    out: dict = {}

    def run():
        loop = asyncio.new_event_loop()
        try:
            out["installed"] = _install_posix_sighup_handler(loop, lambda: None)
        except BaseException as exc:  # noqa: BLE001
            out["error"] = repr(exc)
        finally:
            loop.close()

    t = threading.Thread(target=run)
    t.start()
    t.join()

    assert "error" not in out, out.get("error")
    assert out["installed"] is False
