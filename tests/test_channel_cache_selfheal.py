"""Channel-cache self-heal: on a ``ch_`` lookup miss the data
clients re-warm from the server (membership-filtered) and re-check
before failing loud; the warm also re-runs on every WS (re)connect."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from puffo_agent.agent import puffo_core_client as pcc
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.portal.ws_local.in_process_data_client import InProcessDataClient


def _data_client() -> tuple[InProcessDataClient, MagicMock, MagicMock]:
    store = MagicMock()
    daemon = MagicMock()
    daemon.rewarm_channel_caches = AsyncMock()
    return InProcessDataClient(store, daemon), store, daemon


# ── (α) on-miss re-warm ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lookup_rewarms_and_rechecks_on_ch_miss():
    client, store, daemon = _data_client()
    # Miss, then the re-warm "heals" the store so the re-check hits.
    store.lookup_channel_space = AsyncMock(side_effect=[None, "sp_healed"])
    out = await client.lookup_channel_space("ch_stale")
    assert out == "sp_healed"
    daemon.rewarm_channel_caches.assert_awaited_once()
    assert store.lookup_channel_space.await_count == 2


@pytest.mark.asyncio
async def test_lookup_returns_none_when_rewarm_doesnt_heal():
    client, store, daemon = _data_client()
    store.lookup_channel_space = AsyncMock(return_value=None)  # genuine non-member
    out = await client.lookup_channel_space("ch_ghost")
    assert out is None
    daemon.rewarm_channel_caches.assert_awaited_once()  # tried, but authoritative miss


@pytest.mark.asyncio
async def test_lookup_no_rewarm_on_bare_slug_miss():
    client, store, daemon = _data_client()
    store.lookup_channel_space = AsyncMock(return_value=None)
    out = await client.lookup_channel_space("alice-1a")  # not a ch_ id → DM path
    assert out is None
    daemon.rewarm_channel_caches.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_hit_skips_rewarm():
    client, store, daemon = _data_client()
    store.lookup_channel_space = AsyncMock(return_value="sp_x")
    assert await client.lookup_channel_space("ch_42") == "sp_x"
    daemon.rewarm_channel_caches.assert_not_awaited()


# ── membership-filter invariant ──────────────────────────────────────

@pytest.mark.asyncio
async def test_warm_caches_only_server_returned_channels(monkeypatch):
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client._channel_space = {}
    client._channel_name_cache = {}
    client.http = MagicMock()
    # Server returns ONLY the member channel (it is membership-filtered).
    client.http.get = AsyncMock(
        return_value={"channels": [{"channel_id": "ch_member", "name": "general"}]}
    )
    client.store = MagicMock()
    client.store.mark_channel_space = AsyncMock()
    monkeypatch.setattr(pcc.disk_cache, "persist_channel", lambda *a, **k: None)

    await client._warm_channels_for_space("sp_1")

    # The one member channel is cached; nothing the server withheld appears.
    assert client._channel_space == {"ch_member": "sp_1"}
    client.store.mark_channel_space.assert_awaited_once_with("ch_member", "sp_1")


# ── (γ) warm on (re)connect ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_ws_connect_schedules_warm():
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    warmed = asyncio.Event()
    hydrated = asyncio.Event()

    async def fake_warm():
        warmed.set()

    client._warm_member_caches = fake_warm
    client._contacts = MagicMock()
    client._contacts.refresh = AsyncMock(side_effect=hydrated.set)
    await client._on_ws_connect()  # fire-and-forget
    await asyncio.wait_for(warmed.wait(), timeout=1.0)
    # Contact allow/block hydration rides the same reconnect tick.
    await asyncio.wait_for(hydrated.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_rewarm_is_debounced():
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client._rewarm_lock = asyncio.Lock()
    client._last_rewarm = 0.0
    calls: list[int] = []

    async def fake_warm():
        calls.append(1)

    client._warm_member_caches = fake_warm
    await client.rewarm_channel_caches()
    await client.rewarm_channel_caches()  # within the 5s window → skipped
    assert len(calls) == 1


# ── data-service (cli-local / cli-docker MCP path) on-miss re-warm ───

def _isolated_home() -> str:
    import os
    import tempfile
    from pathlib import Path

    home = tempfile.mkdtemp(prefix="puffo-agent-selfheal-")
    os.environ["PUFFO_AGENT_HOME"] = home
    os.environ["PUFFO_HOME"] = home
    Path(home, "agents").mkdir(parents=True, exist_ok=True)
    return home


async def _seed_empty_agent(home: str, agent_id: str):
    from pathlib import Path

    from puffo_agent.agent.message_store import MessageStore

    agent_path = Path(home) / "agents" / agent_id
    agent_path.mkdir(parents=True, exist_ok=True)
    db_path = agent_path / "messages.db"
    store = MessageStore(db_path)
    await store.open()
    await store.close()
    return db_path


@pytest.mark.asyncio
async def test_data_service_lookup_rewarms_and_heals():
    from aiohttp.test_utils import TestClient, TestServer

    from puffo_agent.agent.message_store import MessageStore
    from puffo_agent.portal import data_service as ds

    home = _isolated_home()
    db_path = await _seed_empty_agent(home, "agent-heal-1")

    class _FakeClient:
        async def rewarm_channel_caches(self):
            # The real warm writes through to the same messages.db.
            store = MessageStore(db_path)
            await store.open()
            await store.mark_channel_space("ch_healed", "sp_9")
            await store.close()

    ds.set_client_resolver(lambda aid: _FakeClient() if aid == "agent-heal-1" else None)
    try:
        app = ds.build_app(ds.DataServiceConfig())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/v1/data/agent-heal-1/channels/ch_healed/space")
            assert resp.status == 200
            assert (await resp.json())["space_id"] == "sp_9"
    finally:
        ds.set_client_resolver(None)


@pytest.mark.asyncio
async def test_data_service_lookup_404_when_rewarm_doesnt_heal():
    from aiohttp.test_utils import TestClient, TestServer

    from puffo_agent.portal import data_service as ds

    home = _isolated_home()
    await _seed_empty_agent(home, "agent-heal-2")
    rewarmed: list[int] = []

    class _FakeClient:
        async def rewarm_channel_caches(self):
            rewarmed.append(1)

    ds.set_client_resolver(lambda aid: _FakeClient())
    try:
        app = ds.build_app(ds.DataServiceConfig())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/v1/data/agent-heal-2/channels/ch_ghost/space")
            assert resp.status == 404
            assert rewarmed == [1]  # tried; authoritative miss
    finally:
        ds.set_client_resolver(None)


@pytest.mark.asyncio
async def test_data_service_lookup_404_without_resolver():
    from aiohttp.test_utils import TestClient, TestServer

    from puffo_agent.portal import data_service as ds

    home = _isolated_home()
    await _seed_empty_agent(home, "agent-heal-3")
    ds.set_client_resolver(None)
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/data/agent-heal-3/channels/ch_x/space")
        assert resp.status == 404


def test_daemon_registers_client_resolver():
    """Source pin: the daemon wires + clears the data-service resolver."""
    import inspect

    from puffo_agent.portal import daemon as daemon_mod

    src = inspect.getsource(daemon_mod)
    assert "set_client_resolver(self._resolve_message_client)" in src
    assert "set_client_resolver(None)" in src
