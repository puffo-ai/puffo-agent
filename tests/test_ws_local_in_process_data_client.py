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
    store.get_visible_message_by_envelope = AsyncMock(return_value=None)
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
        "ch_42",
        limit=50,
        since_envelope_id="msg_x",
        before_envelope_id="msg_y",
        before_ts=10,
        after_ts=5,
        before_seq=12,
        after_seq=6,
    )
    store.get_channel_roots.assert_awaited_once_with(
        channel_id="ch_42",
        limit=50,
        since_envelope_id="msg_x",
        before_envelope_id="msg_y",
        before_ts=10,
        after_ts=5,
        before_seq=12,
        after_seq=6,
    )


@pytest.mark.asyncio
async def test_get_dm_history_forwards():
    client, store, _ = _make_client()
    store.get_dm_history = AsyncMock(return_value=["m1"])
    out = await client.get_dm_history(
        "alice-1a",
        limit=10,
        before=123,
        before_envelope_id="msg_before",
        after_envelope_id="msg_after",
    )
    assert out == ["m1"]
    store.get_dm_history.assert_awaited_once_with(
        peer_slug="alice-1a",
        limit=10,
        before=123,
        before_envelope_id="msg_before",
        after_envelope_id="msg_after",
    )


@pytest.mark.asyncio
async def test_get_thread_messages_forwards_kwargs():
    client, store, _ = _make_client()
    await client.get_thread_messages(
        "msg_root",
        limit=10,
        since_envelope_id=None,
        before_envelope_id="msg_before",
        before_ts=None,
        after_ts=None,
        before_seq=None,
        after_seq=6,
    )
    store.get_thread_messages.assert_awaited_once_with(
        root_id="msg_root",
        limit=10,
        since_envelope_id=None,
        before_envelope_id="msg_before",
        before_ts=None,
        after_ts=None,
        before_seq=None,
        after_seq=6,
    )


@pytest.mark.asyncio
async def test_get_message_by_envelope_forwards():
    """Forwards to the model-visible read: this shim serves ``get_post``,
    so a foreign DM still held for approval must stay withheld here."""
    client, store, _ = _make_client()
    await client.get_message_by_envelope("msg_q")
    store.get_visible_message_by_envelope.assert_awaited_once_with("msg_q")


@pytest.mark.asyncio
async def test_update_profile_cache_calls_worker_set_profile():
    client, _, worker = _make_client()
    await client.update_profile_cache("alice", "Alice", "https://x/a.png")
    worker.set_profile.assert_called_once_with("alice", "Alice", "https://x/a.png")


async def _daemon_dm_path():
    """Build one daemon-authored rootless DM and report its endpoint."""
    from puffo_agent.agent import outbound_messages

    keystore = MagicMock()
    keystore.load_session.return_value = MagicMock(
        subkey_id="sk_test", subkey_secret_key="",
    )

    async def fetch_devices(_slugs):
        return [MagicMock(device_id="dev_a")]

    _envelope, path = await outbound_messages._build_native_dm(
        slug="agent-0001",
        recipient_slug="operator-1",
        text="**stranger** is DM-ing me",
        root_id="",
        is_visible_to_human=True,
        keystore=keystore,
        fetch_devices=fetch_devices,
    )
    return path


async def _dm_gate_prompt_send():
    """Run the gate and return the ``_send_dm`` call it issued."""
    from puffo_agent.agent import dm_gate

    sends = []

    async def _send_dm(recipient, text, root_id):
        sends.append((recipient, root_id))
        return {"envelope_id": f"prompt_{len(sends)}"}

    gate_client = MagicMock()
    gate_client._pending_dm_approvals = {}
    gate_client.operator_slug = "operator-1"
    gate_client.slug = "agent-0001"
    gate_client._fetch_user_profile = AsyncMock(return_value=("Stranger", ""))
    gate_client._send_dm = _send_dm
    assert await dm_gate.maybe_gate_foreign_dm(
        gate_client,
        sender_slug="stranger-1",
        text="confidential",
    )
    return sends[0]


@pytest.mark.asyncio
async def test_daemon_authored_dms_always_use_the_e2ee_endpoint(monkeypatch):
    """Every daemon-authored DM seals — there is no per-turn downgrade.

    ``dm_gate``'s operator prompt embeds a decrypted stranger DM, so this
    invariant is what keeps that quoted content off the wire in the clear.
    """
    import puffo_agent.agent.outbound_messages as om

    monkeypatch.setattr(
        om, "encrypt_message", lambda _message, _k: {"envelope_id": "enc_1"},
    )
    monkeypatch.setattr(
        om.Ed25519KeyPair, "from_secret_bytes", staticmethod(lambda _b: MagicMock()),
    )
    monkeypatch.setattr(om, "decode_secret", lambda _v: b"")
    assert await _daemon_dm_path() == "/messages"
    assert await _dm_gate_prompt_send() == ("operator-1", "")
