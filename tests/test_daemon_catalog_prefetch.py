"""``puffo-agent start`` (foreground) must warm the claude-code
model catalog so control-WS ``build_capabilities`` reports the live
list instead of the static fallback."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import patch

import pytest

from puffo_agent.portal import daemon as daemon_mod
from puffo_agent.portal import state as state_mod
from puffo_agent.portal.state import DaemonConfig


def test_daemon_run_calls_model_catalog_prefetch_at_startup():
    """Source-level guard: text-match on ``Daemon.run`` so we don't
    have to mock the API server, RPC service, refresher, and control
    WS just to pin one call."""
    source = inspect.getsource(daemon_mod.Daemon._start_runtime)
    assert "model_catalog" in source and "prefetch" in source


def test_run_daemon_short_circuit_does_not_prefetch(monkeypatch):
    """The already-running short-circuit lives in ``run_daemon``, not
    ``Daemon.run`` — so a second daemon getting refused mustn't fire a
    stray /v1/models fetch."""
    called: list[int] = []
    monkeypatch.setattr(
        "puffo_agent.agent.model_catalog.prefetch",
        lambda: called.append(1),
    )
    with patch(
        "puffo_agent.portal.daemon.is_daemon_alive", return_value=True,
    ), patch(
        "puffo_agent.portal.daemon.read_daemon_pid", return_value=4242,
    ), patch(
        "puffo_agent.portal.daemon._wait_for_existing_daemon_ready",
        return_value=True,
    ):
        rc = asyncio.run(daemon_mod.run_daemon())
    assert rc == 0
    assert called == []


@pytest.mark.asyncio
async def test_run_daemon_cleans_owned_marker_on_construction_failure(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "is_daemon_alive", lambda: False)
    monkeypatch.setattr(daemon_mod.DaemonConfig, "load", lambda: DaemonConfig())
    monkeypatch.setattr(
        "puffo_agent.portal.import_agents.cleanup_staging_dir",
        lambda: None,
    )
    original_write_pid = daemon_mod.write_daemon_pid

    def write_owned_markers(pid):
        original_write_pid(pid)
        state_mod.write_stop_request(pid)

    def fail_construction(_config):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(daemon_mod, "write_daemon_pid", write_owned_markers)
    monkeypatch.setattr(daemon_mod, "Daemon", fail_construction)

    with pytest.raises(RuntimeError, match="construction failed"):
        await daemon_mod.run_daemon()

    assert state_mod.daemon_pid_path().exists() is False
    assert state_mod.stop_request_path().exists() is False


@pytest.mark.asyncio
async def test_daemon_run_cleans_partial_services_before_readiness(
    monkeypatch, tmp_path
):
    events = []

    async def noop(*_args, **_kwargs):
        return None

    async def start_ws(config, *, ws_local_hub):
        events.append(("start", config, ws_local_hub))
        return "ws-runner"

    async def stop_ws(runner):
        events.append(("stop", runner))

    async def start_rpc(*_args, **_kwargs):
        return "rpc-runner"

    async def start_data(*_args, **_kwargs):
        raise RuntimeError("data startup failed")

    async def stop_rpc(runner):
        events.append(("stop", runner))

    class Refresher:
        async def run_loop(self, _stop):
            return None

    class ControlManager:
        async def run(self):
            return None

        def stop(self):
            return None

    daemon = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    daemon.daemon_cfg = DaemonConfig()
    daemon.ws_local_hub = object()
    daemon._stop = asyncio.Event()
    daemon._stop.set()
    daemon.workers = {}
    daemon.refresher = Refresher()
    daemon.codex_refresher = Refresher()
    monkeypatch.setattr(daemon_mod, "home_dir", lambda: tmp_path)
    monkeypatch.setattr(daemon_mod, "start_ws_local_server", start_ws)
    monkeypatch.setattr(daemon_mod, "stop_ws_local_server", stop_ws)
    monkeypatch.setattr(daemon_mod, "start_rpc_service", start_rpc)
    monkeypatch.setattr(daemon_mod, "start_data_service", start_data)
    monkeypatch.setattr(daemon_mod, "stop_rpc_service", stop_rpc)
    monkeypatch.setattr(daemon_mod, "stop_data_service", noop)
    monkeypatch.setattr(daemon_mod, "set_profile_setter", lambda _value: None)
    monkeypatch.setattr(daemon_mod, "set_client_resolver", lambda _value: None)
    monkeypatch.setattr(daemon_mod, "set_rpc_resolver", lambda _value: None)
    monkeypatch.setattr(daemon_mod, "clear_daemon_pid", lambda: None)
    monkeypatch.setattr(daemon_mod, "clear_stop_request", lambda: None)
    monkeypatch.setattr(daemon_mod, "_respawn_codex_on_mcp_change_at_startup", lambda: None)
    monkeypatch.setattr(daemon_mod, "_log_outdated_version_warning", noop)
    monkeypatch.setattr(daemon_mod, "_sweep_archived_pending_revokes_at_startup", noop)
    monkeypatch.setattr(daemon_mod, "_migrate_linked_agents_at_startup", noop)
    monkeypatch.setattr(daemon_mod, "_full_sync_all_owned_agents_at_startup", noop)
    monkeypatch.setattr(daemon_mod.Daemon, "_stop_all_workers", noop)
    monkeypatch.setattr("puffo_agent.agent.model_catalog.prefetch", lambda: None)
    monkeypatch.setattr("puffo_agent.portal.control.client.ControlManager", ControlManager)

    with pytest.raises(RuntimeError, match="data startup failed"):
        await daemon.run()

    assert events == [
        ("start", daemon.daemon_cfg.ws_local_service, daemon.ws_local_hub),
        ("stop", "ws-runner"),
        ("stop", "rpc-runner"),
    ]
