from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    envelope_id TEXT PRIMARY KEY,
    envelope_kind TEXT NOT NULL,
    sender_slug TEXT NOT NULL,
    channel_id TEXT,
    space_id TEXT,
    recipient_slug TEXT,
    content_type TEXT NOT NULL DEFAULT 'text/plain',
    content TEXT NOT NULL,
    sent_at INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    thread_root_id TEXT,
    reply_to_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_channel
    ON messages (channel_id, sent_at) WHERE channel_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_dm
    ON messages (sender_slug, sent_at) WHERE envelope_kind = 'dm';
CREATE INDEX IF NOT EXISTS idx_messages_received
    ON messages (received_at);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class StoredMessage:
    envelope_id: str
    envelope_kind: str
    sender_slug: str
    channel_id: Optional[str]
    space_id: Optional[str]
    recipient_slug: Optional[str]
    content_type: str
    content: Any
    sent_at: int
    received_at: int
    thread_root_id: Optional[str] = None
    reply_to_id: Optional[str] = None


class MessageStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._db: Optional[aiosqlite.Connection] = None

    @staticmethod
    def for_agent(agent_id: str) -> MessageStore:
        home = os.environ.get("PUFFO_HOME", os.path.expanduser("~/.puffo-agent"))
        path = Path(home) / "agents" / agent_id / "messages.db"
        return MessageStore(path)

    async def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.open()
        assert self._db is not None
        return self._db

    async def store(self, payload: Any, *, received_at: int | None = None) -> None:
        db = await self._ensure_db()
        if isinstance(payload, dict):
            envelope_id = payload.get("envelope_id", "")
            envelope_kind = payload.get("envelope_kind", "channel")
            sender_slug = payload.get("sender_slug", "")
            channel_id = payload.get("channel_id")
            space_id = payload.get("space_id")
            recipient_slug = payload.get("recipient_slug")
            content_type = payload.get("content_type", "text/plain")
            content = payload.get("content", "")
            sent_at = payload.get("sent_at", _now_ms())
            thread_root_id = payload.get("thread_root_id")
            reply_to_id = payload.get("reply_to_id")
        else:
            envelope_id = payload.envelope_id
            envelope_kind = payload.envelope_kind
            sender_slug = payload.sender_slug
            channel_id = payload.channel_id
            space_id = payload.space_id
            recipient_slug = payload.recipient_slug
            content_type = payload.content_type
            content = payload.content
            sent_at = payload.sent_at
            thread_root_id = getattr(payload, "thread_root_id", None)
            reply_to_id = getattr(payload, "reply_to_id", None)

        content_str = json.dumps(content) if not isinstance(content, str) else content
        if received_at is None:
            received_at = _now_ms()

        await db.execute(
            """INSERT OR IGNORE INTO messages
            (envelope_id, envelope_kind, sender_slug, channel_id, space_id,
             recipient_slug, content_type, content, sent_at, received_at,
             thread_root_id, reply_to_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                envelope_id, envelope_kind, sender_slug, channel_id, space_id,
                recipient_slug, content_type, content_str, sent_at, received_at,
                thread_root_id, reply_to_id,
            ),
        )
        await db.commit()

    async def has_message(self, envelope_id: str) -> bool:
        db = await self._ensure_db()
        async with db.execute(
            "SELECT 1 FROM messages WHERE envelope_id = ?", (envelope_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def lookup_channel_space(self, channel_id: str) -> str | None:
        """Return the ``space_id`` last seen for ``channel_id``, or
        ``None`` when no message from that channel has been stored.
        Used by the MCP subprocess (which can't read the daemon's
        in-memory ``_channel_space`` map) as a cross-space fallback.
        """
        if not channel_id:
            return None
        db = await self._ensure_db()
        async with db.execute(
            "SELECT space_id FROM messages WHERE channel_id = ? "
            "AND space_id IS NOT NULL AND space_id != '' "
            "ORDER BY sent_at DESC LIMIT 1",
            (channel_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return row[0] if row[0] else None

    async def get_channel_history(
        self,
        channel_id: str,
        limit: int = 50,
        before: int | None = None,
    ) -> list[StoredMessage]:
        db = await self._ensure_db()
        if before is not None:
            sql = """SELECT * FROM messages
                     WHERE channel_id = ? AND sent_at < ?
                     ORDER BY sent_at DESC, envelope_id DESC LIMIT ?"""
            params: tuple = (channel_id, before, limit)
        else:
            sql = """SELECT * FROM messages
                     WHERE channel_id = ?
                     ORDER BY sent_at DESC, envelope_id DESC LIMIT ?"""
            params = (channel_id, limit)

        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in reversed(rows)]

    async def get_dm_history(
        self,
        peer_slug: str,
        limit: int = 50,
        before: int | None = None,
    ) -> list[StoredMessage]:
        db = await self._ensure_db()
        if before is not None:
            sql = """SELECT * FROM messages
                     WHERE envelope_kind = 'dm'
                       AND (sender_slug = ? OR recipient_slug = ?)
                       AND sent_at < ?
                     ORDER BY sent_at DESC, envelope_id DESC LIMIT ?"""
            params: tuple = (peer_slug, peer_slug, before, limit)
        else:
            sql = """SELECT * FROM messages
                     WHERE envelope_kind = 'dm'
                       AND (sender_slug = ? OR recipient_slug = ?)
                     ORDER BY sent_at DESC, envelope_id DESC LIMIT ?"""
            params = (peer_slug, peer_slug, limit)

        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in reversed(rows)]

    async def get_message_by_envelope(
        self, envelope_id: str,
    ) -> Optional["StoredMessage"]:
        """Single-row lookup by envelope_id. Returns ``None`` when
        the agent never saw the message. Lets this class act as an
        in-process drop-in for ``mcp.data_client.DataClient`` in hosts
        that skip the loopback HTTP round-trip.
        """
        if not envelope_id:
            return None
        db = await self._ensure_db()
        async with db.execute(
            "SELECT * FROM messages WHERE envelope_id = ?", (envelope_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_msg(row)

    async def cleanup(self, retention_days: int = 90) -> int:
        db = await self._ensure_db()
        cutoff = _now_ms() - retention_days * 86_400_000
        async with db.execute(
            "DELETE FROM messages WHERE received_at < ?", (cutoff,)
        ) as cursor:
            count = cursor.rowcount
        await db.commit()
        return count

    def _row_to_msg(self, row: aiosqlite.Row) -> StoredMessage:
        content_raw = row["content"]
        try:
            content = json.loads(content_raw)
        except (json.JSONDecodeError, ValueError):
            content = content_raw

        return StoredMessage(
            envelope_id=row["envelope_id"],
            envelope_kind=row["envelope_kind"],
            sender_slug=row["sender_slug"],
            channel_id=row["channel_id"],
            space_id=row["space_id"],
            recipient_slug=row["recipient_slug"],
            content_type=row["content_type"],
            content=content,
            sent_at=row["sent_at"],
            received_at=row["received_at"],
            thread_root_id=row["thread_root_id"],
            reply_to_id=row["reply_to_id"],
        )
