"""Discord message transport for Puffo agents.

The live gateway path is intentionally thin; most behavior is exposed
through ``handle_discord_message`` so routing, storage, and retry
semantics can be tested without a Discord connection.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .message_store import MessageStore
from .puffo_core_client import _compute_priority

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<@!?(\d+)>")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _discord_id(kind: str, value: str | int | None) -> str:
    raw = "" if value is None else str(value)
    if raw.startswith("discord:"):
        return raw
    return f"discord:{kind}:{raw}"


def _raw_discord_id(value: str) -> str:
    return str(value).rsplit(":", 1)[-1]


@dataclass
class _ThreadEntry:
    current_priority: int
    current_seq: int
    messages: list[dict] = field(default_factory=list)
    in_queue: bool = True
    channel_meta: dict = field(default_factory=dict)
    dispatching_ids: set[str] = field(default_factory=set)


class DiscordMessageClient:
    """Discord-backed message client.

    ``send_func`` is a test seam. In production, ``listen`` starts a
    discord.py client and ``send_fallback_message`` posts through that
    client. If ``webhook_url`` is configured, a later implementation can
    route outbound messages through a per-agent webhook identity without
    changing the worker contract.
    """

    MAX_API_ERROR_RETRIES = 3

    def __init__(
        self,
        *,
        agent_slug: str,
        bot_token: str,
        guild_id: str,
        agent_user_id: str,
        message_store: MessageStore,
        bot_user_id: str = "",
        channel_ids: list[str] | None = None,
        webhook_url: str = "",
        display_prefix: str = "",
        send_func: Optional[Callable[[str, str, str], Awaitable[None]]] = None,
    ):
        self.agent_slug = agent_slug
        self.bot_token = bot_token
        self.guild_id = str(guild_id)
        self.agent_user_id = str(agent_user_id)
        self.bot_user_id = str(bot_user_id)
        self.channel_ids = {str(c) for c in (channel_ids or []) if str(c)}
        self.webhook_url = webhook_url
        self.display_prefix = display_prefix
        self.store = message_store
        self._send_func = send_func
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._queue_seq = 0
        self._thread_state: dict[str, _ThreadEntry] = {}
        self._thread_channel_ids: set[str] = set()
        self._discord_client: Any = None
        self._consumer_task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    async def listen(
        self,
        on_message,
        on_api_error_retry=None,
        on_api_error_abandon=None,
        on_turn_success=None,
    ) -> None:
        await self.store.open()
        self._consumer_task = asyncio.create_task(
            self._consume_queue(
                on_message,
                on_api_error_retry,
                on_api_error_abandon,
                on_turn_success,
            ),
        )
        try:
            discord = _import_discord()
            intents = discord.Intents.default()
            intents.message_content = True
            intents.guilds = True
            intents.members = True
            client = discord.Client(intents=intents)
            self._discord_client = client

            @client.event
            async def on_ready():
                if not self.bot_user_id and client.user is not None:
                    self.bot_user_id = str(client.user.id)
                logger.info("discord transport ready for agent %s", self.agent_slug)

            @client.event
            async def on_message(message):
                await self.handle_discord_message(message)

            await client.start(self.bot_token)
        finally:
            if self._consumer_task is not None:
                self._consumer_task.cancel()
                try:
                    await self._consumer_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def handle_discord_message(self, message: Any) -> None:
        """Normalize one Discord message into the worker batch queue."""
        channel = getattr(message, "channel", None)
        channel_raw = str(getattr(channel, "id", ""))
        parent_channel_raw = str(getattr(channel, "parent_id", "") or "")
        if (
            self.channel_ids
            and channel_raw not in self.channel_ids
            and parent_channel_raw not in self.channel_ids
        ):
            return

        author = getattr(message, "author", None)
        author_id = str(getattr(author, "id", ""))
        if self.bot_user_id and author_id == self.bot_user_id:
            return

        content = str(getattr(message, "content", "") or "")
        mention_ids = {m.group(1) for m in _MENTION_RE.finditer(content)}
        for mentioned in getattr(message, "mentions", []) or []:
            mid = str(getattr(mentioned, "id", ""))
            if mid:
                mention_ids.add(mid)
        is_self_mention = self.agent_user_id in mention_ids
        author_is_bot = bool(getattr(author, "bot", False))
        if author_is_bot and not is_self_mention:
            return

        message_id = _discord_id("message", getattr(message, "id", ""))
        channel_id = _discord_id("channel", channel_raw)
        if _looks_like_thread(channel):
            self._thread_channel_ids.add(channel_raw)
        guild_obj = getattr(message, "guild", None)
        guild_id = _discord_id("guild", getattr(guild_obj, "id", self.guild_id))

        root_id = self._root_id_for(message)
        thread_root_id = None if root_id == message_id else root_id
        sender_slug = self._sender_slug(author)
        sent_at = self._sent_at_ms(message)
        clean_text = self._rewrite_self_mentions(content) if is_self_mention else content

        await self.store.store({
            "envelope_id": message_id,
            "envelope_kind": "channel",
            "sender_slug": sender_slug,
            "channel_id": channel_id,
            "space_id": guild_id,
            "recipient_slug": None,
            "content_type": "text/plain",
            "content": clean_text,
            "sent_at": sent_at,
            "thread_root_id": thread_root_id,
            "reply_to_id": self._reply_to_id_for(message),
        })
        await self.store.mark_channel_space(channel_id, guild_id)

        last_processed = await self.store.get_last_processed_sent_at(root_id)
        if sent_at <= last_processed:
            return

        mentions = [
            {
                "username": self.agent_slug,
                "is_bot": True,
                "is_self": True,
            }
        ] if is_self_mention else []
        priority = _compute_priority(is_self_mention, author_is_bot)
        channel_name = str(getattr(channel, "name", channel_raw))
        guild_name = str(getattr(guild_obj, "name", self.guild_id))
        msg_dict = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "space_id": guild_id,
            "space_name": guild_name,
            "sender_slug": sender_slug,
            "sender_display_name": str(
                getattr(author, "display_name", "")
                or getattr(author, "name", "")
                or author_id
            ),
            "sender_email": "",
            "text": clean_text,
            "root_id": thread_root_id or "",
            "is_dm": False,
            "attachments": [],
            "sender_is_bot": author_is_bot,
            "mentions": mentions,
            "envelope_id": message_id,
            "sent_at": sent_at,
            "is_visible_to_human": True,
        }
        channel_meta = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "space_id": guild_id,
            "space_name": guild_name,
            "is_dm": False,
        }
        await self._admit_thread_message(
            root_id=root_id,
            priority=priority,
            msg_dict=msg_dict,
            channel_meta=channel_meta,
        )

    def _root_id_for(self, message: Any) -> str:
        channel = getattr(message, "channel", None)
        if _looks_like_thread(channel):
            return _discord_id("message", getattr(channel, "id", ""))
        reply_id = self._reply_to_id_for(message)
        return reply_id or _discord_id("message", getattr(message, "id", ""))

    def _reply_to_id_for(self, message: Any) -> str | None:
        ref = getattr(message, "reference", None)
        if ref is None:
            return None
        ref_id = getattr(ref, "message_id", None)
        if not ref_id:
            resolved = getattr(ref, "resolved", None)
            ref_id = getattr(resolved, "id", None)
        return _discord_id("message", ref_id) if ref_id else None

    def _sender_slug(self, author: Any) -> str:
        name = (
            getattr(author, "global_name", "")
            or getattr(author, "name", "")
            or "discord-user"
        )
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(name)).strip("-").lower()
        return f"discord-{safe or 'user'}-{getattr(author, 'id', '')}"

    def _sent_at_ms(self, message: Any) -> int:
        created_at = getattr(message, "created_at", None)
        if created_at is not None and hasattr(created_at, "timestamp"):
            return int(created_at.timestamp() * 1000)
        return _now_ms()

    def _rewrite_self_mentions(self, content: str) -> str:
        return re.sub(
            rf"<@!?{re.escape(self.agent_user_id)}>",
            f"@you({self.agent_slug})",
            content,
        )

    async def _admit_thread_message(
        self,
        *,
        root_id: str,
        priority: int,
        msg_dict: dict,
        channel_meta: dict,
    ) -> None:
        entry = self._thread_state.get(root_id)
        incoming_id = msg_dict.get("envelope_id", "")
        if entry is not None and incoming_id and incoming_id in entry.dispatching_ids:
            return
        if entry is None or not entry.in_queue:
            self._queue_seq += 1
            if entry is None:
                entry = _ThreadEntry(
                    current_priority=priority,
                    current_seq=self._queue_seq,
                    messages=[msg_dict],
                    in_queue=True,
                    channel_meta=channel_meta,
                )
                self._thread_state[root_id] = entry
            else:
                entry.current_priority = priority
                entry.current_seq = self._queue_seq
                entry.messages = [msg_dict]
                entry.in_queue = True
                entry.channel_meta = channel_meta
            await self._queue.put((priority, entry.current_seq, root_id))
            return
        if incoming_id and any(m.get("envelope_id") == incoming_id for m in entry.messages):
            return
        entry.messages.append(msg_dict)
        if priority < entry.current_priority:
            self._queue_seq += 1
            entry.current_priority = priority
            entry.current_seq = self._queue_seq
            await self._queue.put((priority, entry.current_seq, root_id))

    async def _consume_queue(
        self,
        on_message_batch,
        on_api_error_retry=None,
        on_api_error_abandon=None,
        on_turn_success=None,
    ) -> None:
        from .core import AgentAPIError

        while True:
            try:
                _priority, popped_seq, root_id = await self._queue.get()
            except asyncio.CancelledError:
                return
            entry = self._thread_state.get(root_id)
            if entry is None or not entry.in_queue or entry.current_seq != popped_seq:
                continue
            batch = entry.messages
            channel_meta = entry.channel_meta
            entry.messages = []
            entry.in_queue = False
            entry.dispatching_ids = {
                m.get("envelope_id") for m in batch if m.get("envelope_id")
            }
            try:
                await on_message_batch(root_id, batch, channel_meta)
            except AgentAPIError:
                await self._do_api_error_retries(
                    root_id=root_id,
                    entry=entry,
                    batch=batch,
                    channel_meta=channel_meta,
                    on_api_error_retry=on_api_error_retry,
                    on_api_error_abandon=on_api_error_abandon,
                    on_turn_success=on_turn_success,
                )
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("discord batch handler failed for root %s", root_id)
                continue
            if batch:
                await self.store.mark_thread_processed(root_id, batch[-1].get("sent_at", 0))
            entry.dispatching_ids = set()
            if on_turn_success is not None:
                await on_turn_success(root_id, batch, channel_meta)

    async def _do_api_error_retries(
        self,
        *,
        root_id: str,
        entry: _ThreadEntry,
        batch: list[dict],
        channel_meta: dict,
        on_api_error_retry,
        on_api_error_abandon,
        on_turn_success,
    ) -> None:
        from .core import AgentAPIError

        entry.dispatching_ids = set()
        if on_api_error_retry is None:
            if on_api_error_abandon is not None:
                await on_api_error_abandon(root_id, batch, channel_meta, 0)
            return
        attempts = 0
        for attempt in range(1, self.MAX_API_ERROR_RETRIES + 1):
            attempts = attempt
            try:
                await on_api_error_retry(root_id, batch, channel_meta)
                if batch:
                    await self.store.mark_thread_processed(root_id, batch[-1].get("sent_at", 0))
                if on_turn_success is not None:
                    await on_turn_success(root_id, batch, channel_meta)
                return
            except AgentAPIError:
                continue
        if on_api_error_abandon is not None:
            await on_api_error_abandon(root_id, batch, channel_meta, attempts)

    async def send_fallback_message(
        self, channel_id: str, text: str, root_id: str = "",
    ) -> None:
        outbound = f"{self.display_prefix}: {text}" if self.display_prefix else text
        if self._send_func is not None:
            await self._send_func(channel_id, outbound, root_id)
            return
        if self.webhook_url and _raw_discord_id(channel_id) in self._thread_channel_ids:
            await self._send_via_webhook(outbound, channel_id)
            return
        if self._discord_client is None:
            logger.warning("discord fallback send dropped: client is not connected")
            return
        raw_channel_id = int(_raw_discord_id(channel_id))
        channel = self._discord_client.get_channel(raw_channel_id)
        if channel is None:
            channel = await self._discord_client.fetch_channel(raw_channel_id)
        raw_root_id = _raw_discord_id(root_id) if root_id else ""
        if raw_root_id and raw_root_id != str(raw_channel_id):
            try:
                root_message = await channel.fetch_message(int(raw_root_id))
                await root_message.reply(outbound, mention_author=False)
                return
            except Exception:
                logger.exception(
                    "discord fallback reply-to-root failed; posting in channel"
                )
        await channel.send(outbound)

    async def _send_via_webhook(self, text: str, channel_id: str) -> None:
        import aiohttp

        url = self.webhook_url
        raw_channel_id = _raw_discord_id(channel_id)
        if raw_channel_id in self._thread_channel_ids:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}thread_id={raw_channel_id}"
        payload = {"content": text}
        if self.display_prefix:
            payload["username"] = self.display_prefix
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Discord webhook send failed: HTTP {resp.status}: {body[:200]}"
                    )

    async def stop(self) -> None:
        self._stopped.set()
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._discord_client is not None:
            await self._discord_client.close()
        await self.store.close()


def _looks_like_thread(channel: Any) -> bool:
    if channel is None:
        return False
    if bool(getattr(channel, "is_thread", False)):
        return True
    return channel.__class__.__name__.lower() == "thread"


def _import_discord():
    try:
        import discord  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Discord transport requires `pip install puffo-agent[discord]` "
            "and Discord Message Content Intent enabled for the bot."
        ) from exc
    return discord
