"""PUF-376: channel-cache self-heal.

A membership event missed during a WS reconnect used to leave a
genuinely-member channel uncached — send-path fail-loud with no
self-heal until daemon restart, while list-path (server-query) showed
it. Fixes:
  (α) on a ``ch_`` cache-miss the in-process data client re-warms from
      the server (membership-filtered) + re-checks before failing loud.
  (γ) the warm re-runs on every WS (re)connect, not just first connect.
Plus Nova's membership-filter invariant: the warm only caches channels
the server returns (proving membership), never all space channels.
"""

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


# ── membership-filter invariant (Nova's insurance) ───────────────────

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

    async def fake_warm():
        warmed.set()

    client._warm_member_caches = fake_warm
    await client._on_ws_connect()  # fire-and-forget
    await asyncio.wait_for(warmed.wait(), timeout=1.0)


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
