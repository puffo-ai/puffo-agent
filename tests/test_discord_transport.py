from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.core import AgentAPIError
from puffo_agent.agent.discord_client import DiscordMessageClient
from puffo_agent.agent.message_store import MessageStore
from puffo_agent.portal.state import AgentConfig, DiscordConfig
from puffo_agent.portal.worker import _build_message_client


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _make_store() -> MessageStore:
    d = tempfile.mkdtemp()
    store = MessageStore(os.path.join(d, "messages.db"))
    await store.open()
    return store


@dataclass
class FakeAuthor:
    id: str
    name: str = "Alice"
    display_name: str = "Alice"
    bot: bool = False


@dataclass
class FakeGuild:
    id: str = "123"
    name: str = "Elon Team"


@dataclass
class FakeChannel:
    id: str = "456"
    name: str = "general"


@dataclass
class FakeReference:
    message_id: str


@dataclass
class FakeMessage:
    id: str
    content: str
    author: FakeAuthor
    channel: FakeChannel = field(default_factory=FakeChannel)
    guild: FakeGuild = field(default_factory=FakeGuild)
    mentions: list = field(default_factory=list)
    reference: FakeReference | None = None
    created_at: object | None = None


def _client(store: MessageStore, **kwargs) -> DiscordMessageClient:
    return DiscordMessageClient(
        agent_slug="engineer-357c8a5e",
        bot_token="token",
        guild_id="123",
        agent_user_id="999",
        bot_user_id="777",
        message_store=store,
        **kwargs,
    )


def test_discord_config_is_configured():
    assert not DiscordConfig().is_configured()
    assert DiscordConfig(
        bot_token="token",
        guild_id="123",
        agent_user_id="999",
    ).is_configured()


def test_build_message_client_selects_discord_backend():
    cfg = AgentConfig(
        id="engineer-357c8a5e",
        chat_backend="discord",
        discord=DiscordConfig(
            bot_token="token",
            guild_id="123",
            agent_user_id="999",
            channel_ids=["456"],
        ),
    )

    client = _build_message_client(cfg, cfg.id)

    assert isinstance(client, DiscordMessageClient)
    assert client.channel_ids == {"456"}


@pytest.mark.asyncio
async def test_discord_mention_routes_to_thread_queue_and_rewrites_self_mention():
    store = await _make_store()
    client = _client(store)
    msg = FakeMessage(
        id="1000",
        content="<@999> please implement this",
        author=FakeAuthor(id="42"),
    )

    await client.handle_discord_message(msg)

    entry = client._thread_state["discord:message:1000"]
    assert entry.messages[0]["mentions"] == [{
        "username": "engineer-357c8a5e",
        "is_bot": True,
        "is_self": True,
    }]
    assert "@you(engineer-357c8a5e)" in entry.messages[0]["text"]
    assert entry.channel_meta["channel_id"] == "discord:channel:456"
    assert await store.lookup_channel_space("discord:channel:456") == "discord:guild:123"
    await store.close()


@pytest.mark.asyncio
async def test_discord_bot_loop_suppresses_unmentioned_bot_messages():
    store = await _make_store()
    client = _client(store)
    msg = FakeMessage(
        id="1001",
        content="background bot chatter",
        author=FakeAuthor(id="43", name="helper", bot=True),
    )

    await client.handle_discord_message(msg)

    assert client._thread_state == {}
    assert not await store.has_message("discord:message:1001")
    await store.close()


@pytest.mark.asyncio
async def test_discord_reply_preserves_root_history_with_prefixed_ids():
    store = await _make_store()
    client = _client(store)

    await client.handle_discord_message(FakeMessage(
        id="2000",
        content="<@999> root task",
        author=FakeAuthor(id="42"),
    ))
    await client.handle_discord_message(FakeMessage(
        id="2001",
        content="follow-up",
        author=FakeAuthor(id="42"),
        reference=FakeReference(message_id="2000"),
    ))

    roots = await store.get_channel_roots("discord:channel:456")
    assert [r.message.envelope_id for r in roots] == ["discord:message:2000"]
    assert roots[0].reply_count == 1
    thread = await store.get_thread_messages("discord:message:2000")
    assert [m.envelope_id for m in thread] == [
        "discord:message:2000",
        "discord:message:2001",
    ]
    await store.close()


@pytest.mark.asyncio
async def test_discord_consumer_retries_interrupted_turn_and_marks_processed():
    store = await _make_store()
    client = _client(store)
    await client.handle_discord_message(FakeMessage(
        id="3000",
        content="<@999> retry this",
        author=FakeAuthor(id="42"),
    ))
    done = asyncio.Event()
    calls = {"fresh": 0, "retry": 0, "success": 0}

    async def on_message(root_id, batch, channel_meta):
        calls["fresh"] += 1
        raise AgentAPIError("rate limited")

    async def on_retry(root_id, batch, channel_meta):
        calls["retry"] += 1

    async def on_success(root_id, batch, channel_meta):
        calls["success"] += 1
        done.set()

    task = asyncio.create_task(
        client._consume_queue(
            on_message,
            on_api_error_retry=on_retry,
            on_turn_success=on_success,
        )
    )
    await asyncio.wait_for(done.wait(), timeout=2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert calls == {"fresh": 1, "retry": 1, "success": 1}
    assert await store.get_last_processed_sent_at("discord:message:3000") > 0
    await store.close()
