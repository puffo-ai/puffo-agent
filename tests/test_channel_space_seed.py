"""Seed channel->space from list_spaces.

A keyless bridge agent's daemon resolves a channel's space via
``lookup_channel_space`` before it can post. Previously that map was only
filled by inbound messages, so posting to a member channel the agent had
never been messaged in 404'd ("no record of channel"). ``_refresh_bridge_spaces``
now seeds every channel the ``list_spaces`` reply carries — on startup and on
``added_to_space`` — so a proactive post works without a message-there-first.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent import puffo_core_client as pcc
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.agent.message_store import MessageStore
from puffo_agent.crypto.http_client import PuffoCoreHttpClient
from puffo_agent.crypto.keystore import KeyStore


class _FakeBridge:
    def __init__(self, spaces):
        self._spaces = spaces
        self.list_spaces_count = 0

    async def send_list_spaces(self, *, timeout: float = 30.0) -> dict:
        self.list_spaces_count += 1
        return {"spaces": self._spaces}


def _client(tmp_path, bridge, slug="bot-0001") -> PuffoCoreMessageClient:
    ks = KeyStore(str(tmp_path / "keys"))
    http = PuffoCoreHttpClient("http://127.0.0.1:1", ks, slug)
    store = MessageStore(str(tmp_path / "messages.db"))
    return PuffoCoreMessageClient(
        slug=slug,
        device_id="dev_test",
        space_id="sp_home",
        keystore=ks,
        http_client=http,
        message_store=store,
        workspace="",
        bridge_client=bridge,
    )


def test_refresh_seeds_channel_space_for_member_channels(tmp_path, monkeypatch):
    # persist_space writes to a real disk cache — no-op it in the test.
    monkeypatch.setattr(pcc.disk_cache, "persist_space", lambda *a, **k: None)
    bridge = _FakeBridge(spaces=[
        {"space_id": "sp_1", "name": "SilverLake", "channels": [
            {"channel_id": "ch_general", "name": "General"},
            {"channel_id": "ch_random", "name": "Random"},
        ]},
        {"space_id": "sp_2", "name": "Other", "channels": [
            {"channel_id": "ch_x", "name": "X"},
        ]},
    ])
    c = _client(tmp_path, bridge)

    async def go():
        await c.store.open()
        # Before: unknown channel -> the data-service would 404.
        assert await c.store.lookup_channel_space("ch_general") is None

        await c._refresh_bridge_spaces()

        assert bridge.list_spaces_count == 1
        # After: every member channel resolves in BOTH the persistent store
        # (what the data-service reads) and the in-memory cache.
        for cid, sid in (("ch_general", "sp_1"), ("ch_random", "sp_1"), ("ch_x", "sp_2")):
            assert c._channel_space[cid] == sid
            assert await c.store.lookup_channel_space(cid) == sid

    asyncio.run(go())


def test_refresh_tolerates_spaces_without_channels(tmp_path, monkeypatch):
    # Back-compat: an entry with no ``channels`` key must not crash; space
    # name caching still works.
    monkeypatch.setattr(pcc.disk_cache, "persist_space", lambda *a, **k: None)
    bridge = _FakeBridge(spaces=[{"space_id": "sp_1", "name": "Team"}])
    c = _client(tmp_path, bridge)

    async def go():
        await c.store.open()
        await c._refresh_bridge_spaces()
        assert c._space_name_cache.get("sp_1") == "Team"

    asyncio.run(go())
