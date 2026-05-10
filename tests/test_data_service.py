"""Smoke tests for the daemon's read-only data service.

The data service lets MCP subprocesses read the per-agent
``messages.db`` without opening a second SQLite handle on a WAL'd
file across a bind-mount.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.portal import data_service as ds


def _isolated_home() -> str:
    """Fresh ``~/.puffo-agent`` dir; state.py reads PUFFO_AGENT_HOME,
    MessageStore reads PUFFO_HOME."""
    home = tempfile.mkdtemp(prefix="puffo-agent-data-")
    os.environ["PUFFO_AGENT_HOME"] = home
    os.environ["PUFFO_HOME"] = home
    Path(home, "agents").mkdir(parents=True, exist_ok=True)
    return home


async def _seed_agent(home: str, agent_id: str) -> Path:
    """Agent dir with a pre-populated messages.db for deterministic
    assertions."""
    agent_path = Path(home) / "agents" / agent_id
    agent_path.mkdir(parents=True, exist_ok=True)
    db_path = agent_path / "messages.db"
    store = MessageStore(db_path)
    await store.open()
    await store.store({
        "envelope_id": "msg_aaa",
        "envelope_kind": "channel",
        "sender_slug": "alice",
        "channel_id": "ch_1",
        "space_id": "sp_1",
        "content_type": "text/plain",
        "content": "hello",
        "sent_at": 1700000000_000,
    })
    await store.store({
        "envelope_id": "msg_bbb",
        "envelope_kind": "channel",
        "sender_slug": "bob",
        "channel_id": "ch_1",
        "space_id": "sp_1",
        "content_type": "text/plain",
        "content": "world",
        "sent_at": 1700000001_000,
    })
    await store.close()
    return db_path


@pytest.mark.asyncio
async def test_lookup_channel_space_returns_seen_space() -> None:
    home = _isolated_home()
    await _seed_agent(home, "agent-data-1")
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/data/agent-data-1/channels/ch_1/space")
        assert resp.status == 200
        body = await resp.json()
        assert body["space_id"] == "sp_1"


@pytest.mark.asyncio
async def test_lookup_channel_space_404_for_unknown_channel() -> None:
    home = _isolated_home()
    await _seed_agent(home, "agent-data-2")
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/data/agent-data-2/channels/ch_unseen/space")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_recent_messages_returns_chronological() -> None:
    home = _isolated_home()
    await _seed_agent(home, "agent-data-3")
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/data/agent-data-3/messages/recent",
            params={"channel": "ch_1", "limit": "10"},
        )
        assert resp.status == 200
        body = await resp.json()
        msgs = body["messages"]
        assert [m["envelope_id"] for m in msgs] == ["msg_aaa", "msg_bbb"]
        assert msgs[0]["sender_slug"] == "alice"


@pytest.mark.asyncio
async def test_message_by_envelope_returns_single_row() -> None:
    home = _isolated_home()
    await _seed_agent(home, "agent-data-4")
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/data/agent-data-4/messages/msg_bbb")
        assert resp.status == 200
        body = await resp.json()
        assert body["message"]["envelope_id"] == "msg_bbb"
        assert body["message"]["content"] == "world"


@pytest.mark.asyncio
async def test_message_by_envelope_404_when_missing() -> None:
    home = _isolated_home()
    await _seed_agent(home, "agent-data-5")
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/data/agent-data-5/messages/msg_missing")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_unknown_agent_returns_404() -> None:
    _isolated_home()  # empty agents dir
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/data/no-such-agent/channels/ch_1/space")
        assert resp.status == 404
