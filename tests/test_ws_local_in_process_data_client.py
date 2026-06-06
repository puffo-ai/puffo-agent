"""InProcessDataClient delegation surface.

Pins that every method on the shim forwards to the underlying
MessageStore or PuffoCoreMessageClient with the same kwargs the
puffo_core_tools handlers pass — keeps the swap from
``mcp.data_client.DataClient`` invisible.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from puffo_agent.portal.ws_local.in_process_data_client import InProcessDataClient


def _make_client() -> tuple[InProcessDataClient, MagicMock, MagicMock]:
    store = MagicMock()
    store.lookup_channel_space = AsyncMock(return_value="sp_x")
    store.get_channel_roots = AsyncMock(return_value=[])
    store.get_thread_messages = AsyncMock(return_value=[])
    store.get_message_by_envelope = AsyncMock(return_value=None)
    worker = MagicMock()
    worker.set_profile = MagicMock(return_value=None)
    return InProcessDataClient(store, worker), store, worker


@pytest.mark.asyncio
async def test_close_is_noop():
    client, _, _ = _make_client()
    assert await client.close() is None


@pytest.mark.asyncio
async def test_lookup_channel_space_forwards():
    client, store, _ = _make_client()
    assert await client.lookup_channel_space("ch_42") == "sp_x"
    store.lookup_channel_space.assert_awaited_once_with("ch_42")


@pytest.mark.asyncio
async def test_get_channel_roots_forwards_kwargs():
    client, store, _ = _make_client()
    await client.get_channel_roots(
        "ch_42", limit=50, since_envelope_id="msg_x",
        before_ts=10, after_ts=5,
    )
    store.get_channel_roots.assert_awaited_once_with(
        channel_id="ch_42", limit=50, since_envelope_id="msg_x",
        before_ts=10, after_ts=5,
    )


@pytest.mark.asyncio
async def test_get_thread_messages_forwards_kwargs():
    client, store, _ = _make_client()
    await client.get_thread_messages(
        "msg_root", limit=10, since_envelope_id=None,
        before_ts=None, after_ts=None,
    )
    store.get_thread_messages.assert_awaited_once_with(
        root_id="msg_root", limit=10, since_envelope_id=None,
        before_ts=None, after_ts=None,
    )


@pytest.mark.asyncio
async def test_get_message_by_envelope_forwards():
    client, store, _ = _make_client()
    await client.get_message_by_envelope("msg_q")
    store.get_message_by_envelope.assert_awaited_once_with("msg_q")


@pytest.mark.asyncio
async def test_update_profile_cache_calls_worker_set_profile():
    client, _, worker = _make_client()
    await client.update_profile_cache("alice", "Alice", "https://x/a.png")
    worker.set_profile.assert_called_once_with("alice", "Alice", "https://x/a.png")
