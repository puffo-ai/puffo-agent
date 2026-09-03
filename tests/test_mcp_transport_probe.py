"""MCP transport wedge fixes (2026-09-03 incident): the generation
handshake probe, the hello RPC route, per-spawn config generations, and
refresh flags surviving a failed reload."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_failed_reload_keeps_flags_byte_for_byte(tmp_path):
    """A failed reload must not consume the refresh intent: the flag
    files survive unmodified (their content may carry daemon-owned
    scheduling fields) and the call reports failure."""
    provider_flag = tmp_path / "refresh_provider_auth.flag"
    provider_flag.write_text('{"not_before": 123.0}', encoding="utf-8")

    ok = _run(_process_refresh_flags(**_flags_kwargs(tmp_path, _FailingAdapter())))

    assert ok is False
    assert provider_flag.read_text(encoding="utf-8") == '{"not_before": 123.0}'


def _seed_worker(health: str = "ok") -> Worker:
    worker = object.__new__(Worker)
    worker.runtime = RuntimeState(status="running", health=health)
    worker._turn_active = False
    worker._mcp_probe_strikes = 0
    worker._refresh_reload_failures = 0
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
    def __init__(self, generation: str, opened_at: float):
        self.spec = SimpleNamespace(mcp_generation=generation)
        self.last_open_monotonic = opened_at
        self.reload_calls: list[bool] = []

    async def reload_resources(self, *, preserve_session, spec=None):
        self.reload_calls.append(preserve_session)
        self.last_open_monotonic = time.monotonic()


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

    _run(worker.probe_mcp_transport("t"))
    assert mgr.reload_calls == [True]
    assert worker._mcp_probe_strikes == 1
    assert worker.runtime.health == "ok"

    # Freshly recycled: within grace, the probe must not double-punish.
    _run(worker.probe_mcp_transport("t"))
    assert mgr.reload_calls == [True]

    mgr.last_open_monotonic = time.monotonic() - 120
    _run(worker.probe_mcp_transport("t"))
    assert worker.runtime.health == "mcp_unreachable"
    assert ("t", "mcp_unreachable", worker.runtime.error) in saved_states


def test_probe_hello_clears_wedge_state(registered_manager, saved_states):
    opened_at = time.monotonic() - 120
    registered_manager(_FakeManager("g1", opened_at))
    rpc_service.record_mcp_hello("t", "g1")
    worker = _seed_worker(health="mcp_unreachable")
    worker._mcp_probe_strikes = 2

    _run(worker.probe_mcp_transport("t"))

    assert worker._mcp_probe_strikes == 0
    assert worker.runtime.health == "ok"


def test_probe_ignores_stale_generation_hello(registered_manager):
    """A hello from the previous config generation is not proof the
    current spawn's transport works."""
    mgr = registered_manager(_FakeManager("g2", time.monotonic() - 120))
    rpc_service.record_mcp_hello("t", "g1")
    worker = _seed_worker()

    _run(worker.probe_mcp_transport("t"))

    assert mgr.reload_calls == [True]


def test_probe_defers_while_turn_active(registered_manager):
    mgr = registered_manager(_FakeManager("g1", time.monotonic() - 120))
    rpc_service.clear_mcp_hello("t")
    worker = _seed_worker()
    worker._turn_active = True

    _run(worker.probe_mcp_transport("t"))

    assert mgr.reload_calls == []
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
            bad = await client.post(
                "/v1/rpc/t/mcp-hello", json={}, headers=headers,
            )
            assert bad.status == 400
        finally:
            await client.close()

    rpc_service.clear_mcp_hello("t")
    _run(_exercise())
    generation, seen_at = rpc_service.mcp_hello_state("t")
    assert generation == "gen-42"
    assert seen_at > 0.0
    rpc_service.clear_mcp_hello("t")


def test_hello_startup_absent_without_generation(monkeypatch):
    from puffo_agent.mcp import puffo_core_server

    monkeypatch.delenv("PUFFO_MCP_GENERATION", raising=False)
    assert puffo_core_server._make_hello_startup(object()) is None
    monkeypatch.setenv("PUFFO_MCP_GENERATION", "g")
    assert puffo_core_server._make_hello_startup(None) is None


def test_hello_startup_retries_then_succeeds(monkeypatch):
    from puffo_agent.mcp import puffo_core_server

    monkeypatch.setenv("PUFFO_MCP_GENERATION", "g7")
    monkeypatch.setattr(
        puffo_core_server, "_HELLO_RETRY_DELAY_SECONDS", 0.0,
    )
    calls: list[str] = []

    class _Client:
        async def hello(self, generation):
            calls.append(generation)
            if len(calls) < 3:
                raise RuntimeError("rpc mcp-hello transport error")
            return "ok"

    startup = puffo_core_server._make_hello_startup(_Client())
    _run(startup())
    assert calls == ["g7", "g7", "g7"]


# ── per-spawn config generation ────────────────────────────────────────


def test_claude_spec_mints_fresh_generation(tmp_path, monkeypatch):
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime
    from puffo_agent.agent.harness.runtime.local_runtime import (
        LocalRuntimePreparer,
    )

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "puffo"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "host"))
    monkeypatch.setattr(
        local_runtime, "resolve_claude_bin", lambda: "/bin/claude",
    )
    monkeypatch.setattr(local_runtime, "is_macos", lambda: False)
    config = AgentConfig(
        id="gen-test",
        runtime=RuntimeConfig(
            kind="cli-local", provider="anthropic", harness="claude-code",
        ),
        puffo_core=PuffoCoreConfig(
            slug="bot-gen", device_id="d1", space_id="sp1",
        ),
    )
    preparer = LocalRuntimePreparer(DaemonConfig(), config)

    first = preparer._prepare_claude_spec("prompt")
    config_doc = json.loads(
        (tmp_path / "puffo" / "agents" / "gen-test" / "mcp-config.json")
        .read_text(encoding="utf-8")
    )
    written = config_doc["mcpServers"]["puffo"]["env"]["PUFFO_MCP_GENERATION"]
    second = preparer._prepare_claude_spec("prompt")

    assert first.mcp_generation and second.mcp_generation
    assert first.mcp_generation != second.mcp_generation
    assert written == first.mcp_generation
