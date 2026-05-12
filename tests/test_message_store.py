import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.message_store import (
    ChannelRoot,
    MessageStore,
    StoredMessage,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _temp_store() -> MessageStore:
    d = tempfile.mkdtemp()
    return MessageStore(os.path.join(d, "messages.db"))


def _channel_payload(envelope_id: str, channel_id: str = "ch_1", sent_at: int | None = None, **kwargs):
    return {
        "envelope_id": envelope_id,
        "envelope_kind": "channel",
        "sender_slug": kwargs.get("sender_slug", "alice-0001"),
        "channel_id": channel_id,
        "space_id": kwargs.get("space_id", "sp_1"),
        "content_type": "text/plain",
        "content": kwargs.get("content", f"Message {envelope_id}"),
        "sent_at": sent_at or _now_ms(),
        "thread_root_id": kwargs.get("thread_root_id"),
        "reply_to_id": kwargs.get("reply_to_id"),
    }


def _dm_payload(envelope_id: str, sender: str, recipient: str, sent_at: int | None = None, **kwargs):
    return {
        "envelope_id": envelope_id,
        "envelope_kind": "dm",
        "sender_slug": sender,
        "recipient_slug": recipient,
        "content_type": "text/plain",
        "content": kwargs.get("content", f"DM {envelope_id}"),
        "sent_at": sent_at or _now_ms(),
    }


@pytest.mark.asyncio
async def test_store_and_has_message():
    store = _temp_store()
    await store.open()

    assert not await store.has_message("env_1")
    await store.store(_channel_payload("env_1"))
    assert await store.has_message("env_1")

    await store.close()


@pytest.mark.asyncio
async def test_duplicate_insert_ignored():
    store = _temp_store()
    await store.open()

    await store.store(_channel_payload("env_1", content="first"))
    await store.store(_channel_payload("env_1", content="second"))

    msgs = await store.get_channel_history("ch_1")
    assert len(msgs) == 1
    assert msgs[0].content == "first"

    await store.close()


@pytest.mark.asyncio
async def test_channel_history_order():
    store = _temp_store()
    await store.open()

    base = _now_ms()
    await store.store(_channel_payload("env_1", sent_at=base))
    await store.store(_channel_payload("env_2", sent_at=base + 1000))
    await store.store(_channel_payload("env_3", sent_at=base + 2000))

    msgs = await store.get_channel_history("ch_1")
    assert len(msgs) == 3
    assert msgs[0].envelope_id == "env_1"
    assert msgs[2].envelope_id == "env_3"

    await store.close()


@pytest.mark.asyncio
async def test_channel_history_limit():
    store = _temp_store()
    await store.open()

    base = _now_ms()
    for i in range(10):
        await store.store(_channel_payload(f"env_{i}", sent_at=base + i * 1000))

    msgs = await store.get_channel_history("ch_1", limit=3)
    assert len(msgs) == 3
    assert msgs[0].envelope_id == "env_7"
    assert msgs[2].envelope_id == "env_9"

    await store.close()


@pytest.mark.asyncio
async def test_channel_history_before():
    store = _temp_store()
    await store.open()

    base = 1_000_000_000_000
    await store.store(_channel_payload("env_1", sent_at=base))
    await store.store(_channel_payload("env_2", sent_at=base + 1000))
    await store.store(_channel_payload("env_3", sent_at=base + 2000))

    msgs = await store.get_channel_history("ch_1", before=base + 2000)
    assert len(msgs) == 2
    assert msgs[0].envelope_id == "env_1"
    assert msgs[1].envelope_id == "env_2"

    await store.close()


@pytest.mark.asyncio
async def test_channel_filter():
    store = _temp_store()
    await store.open()

    await store.store(_channel_payload("env_1", channel_id="ch_1"))
    await store.store(_channel_payload("env_2", channel_id="ch_2"))
    await store.store(_channel_payload("env_3", channel_id="ch_1"))

    msgs = await store.get_channel_history("ch_1")
    assert len(msgs) == 2
    assert all(m.channel_id == "ch_1" for m in msgs)

    await store.close()


@pytest.mark.asyncio
async def test_dm_history():
    store = _temp_store()
    await store.open()

    base = _now_ms()
    await store.store(_dm_payload("env_1", "alice-0001", "bob-0001", sent_at=base))
    await store.store(_dm_payload("env_2", "bob-0001", "alice-0001", sent_at=base + 1000))
    await store.store(_dm_payload("env_3", "alice-0001", "carol-0001", sent_at=base + 2000))

    msgs = await store.get_dm_history("bob-0001")
    assert len(msgs) == 2
    assert msgs[0].envelope_id == "env_1"
    assert msgs[1].envelope_id == "env_2"

    await store.close()


@pytest.mark.asyncio
async def test_dm_history_before():
    store = _temp_store()
    await store.open()

    base = 1_000_000_000_000
    await store.store(_dm_payload("env_1", "alice", "bob", sent_at=base))
    await store.store(_dm_payload("env_2", "bob", "alice", sent_at=base + 1000))

    msgs = await store.get_dm_history("bob", before=base + 1000)
    assert len(msgs) == 1
    assert msgs[0].envelope_id == "env_1"

    await store.close()


@pytest.mark.asyncio
async def test_cleanup():
    store = _temp_store()
    await store.open()

    old_time = _now_ms() - 100 * 86_400_000
    await store.store(_channel_payload("env_old", sent_at=old_time), received_at=old_time)
    await store.store(_channel_payload("env_new", sent_at=_now_ms()))

    count = await store.cleanup(retention_days=90)
    assert count == 1
    assert not await store.has_message("env_old")
    assert await store.has_message("env_new")

    await store.close()


@pytest.mark.asyncio
async def test_json_content_roundtrip():
    store = _temp_store()
    await store.open()

    payload = _channel_payload("env_1", content={"text": "hello", "attachments": [1, 2]})
    await store.store(payload)

    msgs = await store.get_channel_history("ch_1")
    assert msgs[0].content == {"text": "hello", "attachments": [1, 2]}

    await store.close()


@pytest.mark.asyncio
async def test_string_content_roundtrip():
    store = _temp_store()
    await store.open()

    payload = _channel_payload("env_1", content="plain text")
    await store.store(payload)

    msgs = await store.get_channel_history("ch_1")
    assert msgs[0].content == "plain text"

    await store.close()


@pytest.mark.asyncio
async def test_threading_fields():
    store = _temp_store()
    await store.open()

    payload = _channel_payload(
        "env_1", thread_root_id="env_root", reply_to_id="env_parent",
    )
    await store.store(payload)

    msgs = await store.get_channel_history("ch_1")
    assert msgs[0].thread_root_id == "env_root"
    assert msgs[0].reply_to_id == "env_parent"

    await store.close()


@pytest.mark.asyncio
async def test_auto_open():
    store = _temp_store()
    await store.store(_channel_payload("env_1"))
    assert await store.has_message("env_1")
    await store.close()


@pytest.mark.asyncio
async def test_for_agent_factory():
    store = MessageStore.for_agent("test-agent-123")
    assert "test-agent-123" in str(store.db_path)
    assert str(store.db_path).endswith("messages.db")


# ── get_channel_roots ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_channel_roots_excludes_replies_and_counts_them():
    """Only thread_root_id IS NULL rows are returned; the
    ``reply_count`` field is the running count of replies."""
    store = _temp_store()
    await store.open()
    # Two roots in the same channel.
    await store.store(_channel_payload("root_a", sent_at=100))
    await store.store(_channel_payload("root_b", sent_at=200))
    # Three replies on root_a, one on root_b.
    for i, rt in enumerate(["root_a", "root_a", "root_a"], start=1):
        await store.store(_channel_payload(
            f"reply_a_{i}", sent_at=100 + i, thread_root_id=rt,
        ))
    await store.store(_channel_payload(
        "reply_b_1", sent_at=210, thread_root_id="root_b",
    ))

    roots = await store.get_channel_roots("ch_1")
    assert [r.message.envelope_id for r in roots] == ["root_a", "root_b"]
    counts = {r.message.envelope_id: r.reply_count for r in roots}
    assert counts == {"root_a": 3, "root_b": 1}
    await store.close()


@pytest.mark.asyncio
async def test_channel_roots_since_envelope_id_filters_by_sent_at():
    """``since=<envelope_id>`` resolves to that envelope's sent_at
    and applies an exclusive lower bound."""
    store = _temp_store()
    await store.open()
    await store.store(_channel_payload("root_old", sent_at=100))
    await store.store(_channel_payload("root_mid", sent_at=200))
    await store.store(_channel_payload("root_new", sent_at=300))

    roots = await store.get_channel_roots(
        "ch_1", since_envelope_id="root_old",
    )
    # Strictly after root_old's sent_at, so root_mid + root_new.
    assert [r.message.envelope_id for r in roots] == ["root_mid", "root_new"]
    await store.close()


@pytest.mark.asyncio
async def test_channel_roots_before_and_after_ts():
    """``before`` / ``after`` are exclusive ms-epoch bounds."""
    store = _temp_store()
    await store.open()
    for env_id, ts in [
        ("r_1", 100), ("r_2", 200), ("r_3", 300), ("r_4", 400),
    ]:
        await store.store(_channel_payload(env_id, sent_at=ts))

    roots = await store.get_channel_roots(
        "ch_1", after_ts=100, before_ts=400,
    )
    assert [r.message.envelope_id for r in roots] == ["r_2", "r_3"]
    await store.close()


# ── get_thread_messages ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_thread_messages_includes_root_and_replies():
    store = _temp_store()
    await store.open()
    await store.store(_channel_payload("root_x", sent_at=100))
    await store.store(_channel_payload(
        "reply_1", sent_at=110, thread_root_id="root_x",
    ))
    await store.store(_channel_payload(
        "reply_2", sent_at=120, thread_root_id="root_x",
    ))
    # An unrelated root + reply mustn't leak in.
    await store.store(_channel_payload("root_other", sent_at=130))
    await store.store(_channel_payload(
        "other_reply", sent_at=140, thread_root_id="root_other",
    ))

    msgs = await store.get_thread_messages("root_x")
    assert [m.envelope_id for m in msgs] == ["root_x", "reply_1", "reply_2"]
    await store.close()


@pytest.mark.asyncio
async def test_thread_messages_since_filter():
    store = _temp_store()
    await store.open()
    await store.store(_channel_payload("root_x", sent_at=100))
    await store.store(_channel_payload(
        "reply_1", sent_at=110, thread_root_id="root_x",
    ))
    await store.store(_channel_payload(
        "reply_2", sent_at=120, thread_root_id="root_x",
    ))

    msgs = await store.get_thread_messages(
        "root_x", since_envelope_id="reply_1",
    )
    # Strictly after reply_1's sent_at → only reply_2.
    assert [m.envelope_id for m in msgs] == ["reply_2"]
    await store.close()
