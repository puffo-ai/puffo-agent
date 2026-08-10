"""Smoke tests for the daemon's read-only data service.

The data service lets MCP subprocesses read the per-agent
``messages.db`` without opening a second SQLite handle on a WAL'd
file across a bind-mount.
"""

from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from aiohttp.test_utils import TestClient as AiohttpTestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.message_store import MessageStore, ReceiptDisposition
from puffo_agent.portal import data_service as ds
from puffo_agent.portal.local_service_auth import (
    issue_local_service_token,
    local_service_headers,
)


class TestClient(AiohttpTestClient):
    """Authenticate test requests for the Agent id selected by the path."""

    async def _request(self, method, path, **kwargs):
        parts = urlsplit(str(path)).path.split("/")
        agent_id = unquote(parts[3])
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault(
            "Authorization",
            local_service_headers(issue_local_service_token(agent_id))[
                "Authorization"
            ],
        )
        return await super()._request(method, path, headers=headers, **kwargs)


def test_msg_to_dict_accepts_legacy_message_without_server_seq() -> None:
    legacy = SimpleNamespace(
        envelope_id="legacy",
        envelope_kind="channel",
        sender_slug="alice",
        recipient_slug=None,
        channel_id="ch_legacy",
        space_id="sp_legacy",
        content_type="text/plain",
        content="hello",
        sent_at=1,
        received_at=2,
        thread_root_id=None,
        reply_to_id=None,
        is_encrypted=True,
    )

    assert ds._msg_to_dict(legacy)["server_seq"] is None


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
    first = {
        "envelope_id": "msg_aaa",
        "envelope_kind": "channel",
        "sender_slug": "alice",
        "channel_id": "ch_1",
        "space_id": "sp_1",
        "content_type": "text/plain",
        "content": "hello",
        "sent_at": 1700000000_000,
    }
    second = {
        "envelope_id": "msg_bbb",
        "envelope_kind": "channel",
        "sender_slug": "bob",
        "channel_id": "ch_1",
        "space_id": "sp_1",
        "content_type": "text/plain",
        "content": "world",
        "sent_at": 1700000001_000,
    }
    for seq, payload in ((11, first), (12, second)):
        await store.store_receipt(
            payload,
            server_seq=seq,
            disposition=ReceiptDisposition.ELIGIBLE,
            reason="test",
        )
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
async def test_data_service_rejects_token_scoped_to_another_agent() -> None:
    """One Agent's MCP token cannot select another Agent's database path."""
    app = ds.build_app(ds.DataServiceConfig())
    headers = local_service_headers(issue_local_service_token("agent_a"))
    async with AiohttpTestClient(TestServer(app)) as client:
        response = await client.get(
            "/v1/data/agent_b/channels/ch_1/space",
            headers=headers,
        )
    assert response.status == 401


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
        assert [m["server_seq"] for m in msgs] == [11, 12]
        assert msgs[0]["sender_slug"] == "alice"

        from puffo_agent.mcp.data_client import DataClient

        data_client = DataClient(
            str(client.server.make_url("")).rstrip("/"),
            "agent-data-3",
            issue_local_service_token("agent-data-3"),
        )
        try:
            parsed = await data_client.get_channel_history("ch_1", 10)
            assert [message.server_seq for message in parsed] == [11, 12]
        finally:
            await data_client.close()


async def _seed_dms(home: str, agent_id: str) -> None:
    agent_path = Path(home) / "agents" / agent_id
    agent_path.mkdir(parents=True, exist_ok=True)
    store = MessageStore(agent_path / "messages.db")
    await store.open()
    for env, sender, recip, ts in (
        ("dm_1", "alice", "me", 1700000000_000),
        ("dm_2", "me", "alice", 1700000001_000),
        ("dm_3", "bob", "me", 1700000002_000),   # different peer
    ):
        await store.store({
            "envelope_id": env, "envelope_kind": "dm",
            "sender_slug": sender, "recipient_slug": recip,
            "content_type": "text/plain", "content": env, "sent_at": ts,
        })
    await store.close()


@pytest.mark.asyncio
async def test_dm_history_route_returns_peer_dms() -> None:
    home = _isolated_home()
    await _seed_dms(home, "agent-dm-1")
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/data/agent-dm-1/dms/recent",
            params={"peer": "alice", "limit": "10"},
        )
        assert resp.status == 200
        body = await resp.json()
        # only the alice DMs, chronological; bob's excluded
        assert [m["envelope_id"] for m in body["messages"]] == ["dm_1", "dm_2"]


@pytest.mark.asyncio
async def test_dm_history_route_requires_peer() -> None:
    home = _isolated_home()
    await _seed_dms(home, "agent-dm-2")
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/data/agent-dm-2/dms/recent")
        assert resp.status == 400


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
        assert body["message"]["server_seq"] == 12


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


@pytest.mark.asyncio
async def test_channel_roots_404_for_unknown_channel() -> None:
    # Catches the DataNotFound → 404 mapping for list_channel_roots.
    home = _isolated_home()
    await _seed_agent(home, "agent-roots-404")
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/data/agent-roots-404/channels/roots?channel=ch_nope"
        )
        assert resp.status == 404
        body = await resp.json()
        assert body == {"error": "channel not found"}


@pytest.mark.asyncio
async def test_channel_roots_200_for_seen_channel() -> None:
    home = _isolated_home()
    await _seed_agent(home, "agent-roots-ok")
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/data/agent-roots-ok/channels/roots?channel=ch_1"
        )
        assert resp.status == 200
        body = await resp.json()
        assert "roots" in body


@pytest.mark.asyncio
async def test_thread_messages_404_for_unknown_root() -> None:
    home = _isolated_home()
    await _seed_agent(home, "agent-thread-404")
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/v1/data/agent-thread-404/threads/msg_nope"
        )
        assert resp.status == 404
        body = await resp.json()
        assert body == {"error": "thread root not found"}


@pytest.mark.asyncio
async def test_send_encryption_fail_safe_for_unknown_agent() -> None:
    _isolated_home()
    app = ds.build_app(ds.DataServiceConfig())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/data/nope/send-encryption?slug=x")
        assert resp.status == 200
        assert (await resp.json())["encrypt"] is True


@pytest.mark.asyncio
async def test_send_encryption_decision_matrix_over_http() -> None:
    from puffo_agent.agent import send_mode

    home = _isolated_home()
    agent_path = Path(home) / "agents" / "agent-enc-1"
    agent_path.mkdir(parents=True, exist_ok=True)
    store = MessageStore(agent_path / "messages.db")
    await store.open()
    await store.store({
        "envelope_id": "msg_pt",
        "envelope_kind": "channel",
        "sender_slug": "alice",
        "channel_id": "ch_1",
        "space_id": "sp_1",
        "content_type": "text/plain",
        "content": "clear",
        "sent_at": 1700000000_000,
        "is_encrypted": False,
    })
    await store.store({
        "envelope_id": "msg_enc",
        "envelope_kind": "channel",
        "sender_slug": "bob",
        "channel_id": "ch_1",
        "space_id": "sp_1",
        "content_type": "text/plain",
        "content": "sealed",
        "sent_at": 1700000001_000,
    })
    await store.close()

    send_mode._turn_bundle_encrypted.clear()
    app = ds.build_app(ds.DataServiceConfig())
    try:
        async with TestClient(TestServer(app)) as client:
            base = "/v1/data/agent-enc-1/send-encryption?slug=bot-1"

            async def ask(url):
                resp = await client.get(url)
                assert resp.status == 200
                return (await resp.json())["encrypt"]

            assert await ask(base) is False  # default plaintext
            assert await ask(base + "&thread_root_id=msg_pt") is False
            assert await ask(base + "&thread_root_id=msg_enc") is True  # legacy row
            assert await ask(base + "&thread_root_id=msg_gone") is True  # fail-safe
            send_mode.note_turn_bundle(["bot-1"], True)
            assert await ask(base) is True

            # The MCP-side wrapper resolves the same answers over HTTP.
            from puffo_agent.mcp.data_client import DataClient
            send_mode._turn_bundle_encrypted.clear()
            dc = DataClient(
                str(client.server.make_url("")).rstrip("/"),
                "agent-enc-1",
                issue_local_service_token("agent-enc-1"),
            )
            try:
                assert await dc.get_send_encryption("bot-1", "msg_pt") is False
                assert await dc.get_send_encryption("bot-1", "msg_gone") is True
            finally:
                await dc.close()

            # Error branches fail safe to E2EE: non-route 404 + dead host.
            dc404 = DataClient(
                str(client.server.make_url("/bogus")).rstrip("/"),
                "agent-enc-1",
                issue_local_service_token("agent-enc-1"),
            )
            try:
                assert await dc404.get_send_encryption("bot-1", None) is True
            finally:
                await dc404.close()
            dc_dead = DataClient(
                "http://127.0.0.1:1",
                "agent-enc-1",
                issue_local_service_token("agent-enc-1"),
            )
            try:
                assert await dc_dead.get_send_encryption("bot-1", None) is True
            finally:
                await dc_dead.close()
    finally:
        send_mode._turn_bundle_encrypted.clear()
