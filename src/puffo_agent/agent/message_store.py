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
CREATE INDEX IF NOT EXISTS idx_messages_thread_root
    ON messages (thread_root_id, sent_at) WHERE thread_root_id IS NOT NULL;

-- Per-thread cursor used by the thread-batched priority queue. After
-- ``on_message_batch`` finishes successfully, the consumer advances
-- this to the ``sent_at`` of the last message in the dispatched
-- batch. The listen handler then drops any inbound message whose
-- ``sent_at`` is <= the stored cursor, so server-side pending-message
-- redeliveries after a daemon restart don't re-trigger the agent on
-- already-processed threads.
CREATE TABLE IF NOT EXISTS thread_processing_state (
    root_id TEXT PRIMARY KEY,
    last_processed_sent_at INTEGER NOT NULL
);

-- One row per channel the agent has been auto-prompted to introduce
-- itself in. Gate set in ``_accept_invite`` so a daemon restart (or a
-- server-side invite redelivery) can't trigger a second intro.
CREATE TABLE IF NOT EXISTS channel_intro_prompted (
    channel_id TEXT PRIMARY KEY,
    prompted_at INTEGER NOT NULL
);

-- Out-of-band channel→space mappings discovered without an inbound
-- message. The /messages table inference is enough for channels the
-- agent has received traffic from, but the intro-nudge path needs
-- the mapping BEFORE the first real message — agent calls
-- send_message against the freshly-joined channel, MCP asks
-- lookup_channel_space, and without this table the daemon-side
-- query returns 404 → MCP falls back to agent.yml's home space,
-- which is the wrong space when the agent is now multi-space.
-- Populated by ``_find_public_general_channel`` (and any future
-- channel-discovery hook). ``lookup_channel_space`` checks this
-- table first before the /messages fallback.
CREATE TABLE IF NOT EXISTS channel_space_map (
    channel_id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    learned_at INTEGER NOT NULL
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


class DataNotFound(Exception):
    """Raised by reads that need to distinguish "this channel /
    thread has never been seen" from "seen, but the requested
    window is empty after filters". The MCP tool layer surfaces a
    different user-facing message for each.
    """


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


@dataclass
class ChannelRoot:
    """One root post in a channel plus how many replies it accrued.
    Used by ``get_channel_roots`` to surface thread heads without
    blasting every reply into the agent's context window — the agent
    sees N=reply_count and calls ``get_thread_messages`` only on
    threads it actually wants to read into.
    """
    message: StoredMessage
    reply_count: int


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

    async def channel_exists(self, channel_id: str) -> bool:
        """True iff the store has ever recorded a message in
        ``channel_id``. Used by the data service to return 404 when
        the caller asks for history on a channel the agent has never
        seen — distinguishes "unknown channel" from "known channel,
        empty window after filters" (200 + empty list).
        """
        if not channel_id:
            return False
        db = await self._ensure_db()
        async with db.execute(
            "SELECT 1 FROM messages WHERE channel_id = ? LIMIT 1",
            (channel_id,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def has_message(self, envelope_id: str) -> bool:
        db = await self._ensure_db()
        async with db.execute(
            "SELECT 1 FROM messages WHERE envelope_id = ?", (envelope_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def lookup_channel_space(self, channel_id: str) -> str | None:
        """Return the ``space_id`` known for ``channel_id``, or
        ``None`` when neither the explicit map nor any prior message
        gives one. Used by the MCP subprocess (which can't read the
        daemon's in-memory ``_channel_space`` map) as a cross-space
        fallback.

        Two-source lookup, in order:

        1. ``channel_space_map`` — explicit mappings recorded by
           out-of-band discovery (``_find_public_general_channel`` and
           friends). Lets send_message resolve a channel BEFORE the
           first inbound message lands on it — the case the intro
           nudge needs.
        2. ``messages`` — last ``space_id`` seen on an envelope in
           that channel. Steady-state fallback that doesn't need
           explicit bookkeeping; works automatically once any message
           arrives.
        """
        if not channel_id:
            return None
        db = await self._ensure_db()
        async with db.execute(
            "SELECT space_id FROM channel_space_map WHERE channel_id = ?",
            (channel_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None and row[0]:
            return row[0]
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

    async def mark_channel_space(self, channel_id: str, space_id: str) -> None:
        """Record an explicit channel→space mapping. Called by
        out-of-band channel-discovery paths (``_find_public_general_channel``)
        so ``lookup_channel_space`` can resolve the channel before
        the first inbound message lands."""
        if not channel_id or not space_id:
            return
        db = await self._ensure_db()
        await db.execute(
            """INSERT INTO channel_space_map (channel_id, space_id, learned_at)
               VALUES (?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                 space_id = excluded.space_id,
                 learned_at = excluded.learned_at""",
            (channel_id, space_id, _now_ms()),
        )
        await db.commit()

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

    async def get_thread_batch(
        self,
        root_id: str,
        since_sent_at: int,
    ) -> list[StoredMessage]:
        """Root message + every reply in its thread with
        ``sent_at > since_sent_at``, ordered ascending. The root row
        itself has ``thread_root_id IS NULL`` and matches via the
        ``envelope_id = root_id`` arm of the OR.
        """
        if not root_id:
            return []
        db = await self._ensure_db()
        async with db.execute(
            """SELECT * FROM messages
               WHERE (envelope_id = ? OR thread_root_id = ?)
                 AND sent_at > ?
               ORDER BY sent_at ASC, envelope_id ASC""",
            (root_id, root_id, since_sent_at),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def _resolve_since_sent_at(self, since_envelope_id: str | None) -> int | None:
        """Look up the ``sent_at`` of a reference envelope. Used by
        ``get_channel_roots`` / ``get_thread_messages`` to translate
        a ``since=<envelope_id>`` filter into an exclusive sent_at
        lower bound. Returns ``None`` when the envelope isn't in the
        store (caller treats that as "no since filter")."""
        if not since_envelope_id:
            return None
        db = await self._ensure_db()
        async with db.execute(
            "SELECT sent_at FROM messages WHERE envelope_id = ?",
            (since_envelope_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else None

    async def get_channel_roots(
        self,
        channel_id: str,
        limit: int = 20,
        since_envelope_id: str | None = None,
        before_ts: int | None = None,
        after_ts: int | None = None,
    ) -> list[ChannelRoot]:
        """Recent root posts in ``channel_id`` (``thread_root_id``
        IS NULL) with the count of replies that point at each.

        Replies in any thread are excluded from the result — the
        agent gets one bullet per conversation head and can drill
        into the threads it actually cares about via
        ``get_thread_messages``. Filtering options:

        - ``since_envelope_id`` — return roots whose ``sent_at`` is
          strictly greater than that envelope's. Convenient when
          the agent already knows the latest root it processed.
        - ``after_ts`` (ms-epoch) — exclusive lower bound on
          ``sent_at``. Combined with ``since`` we take the larger.
        - ``before_ts`` (ms-epoch) — exclusive upper bound on
          ``sent_at``.

        Returned newest-first up to ``limit``. Raises
        ``DataNotFound`` if the channel has never had any message
        stored — so the MCP layer can distinguish "unknown channel"
        from "known but empty window".
        """
        if not await self.channel_exists(channel_id):
            raise DataNotFound(f"channel not found: {channel_id}")
        db = await self._ensure_db()
        lower_bounds: list[int] = []
        since_resolved = await self._resolve_since_sent_at(since_envelope_id)
        if since_resolved is not None:
            lower_bounds.append(since_resolved)
        if after_ts is not None:
            lower_bounds.append(int(after_ts))
        effective_after = max(lower_bounds) if lower_bounds else None

        # ``reply_count`` is a correlated subquery on the same
        # ``messages`` table; the WAL writer is the only producer, so
        # the count is point-in-time consistent.
        clauses = ["m.channel_id = ?", "m.thread_root_id IS NULL"]
        params: list = [channel_id]
        if effective_after is not None:
            clauses.append("m.sent_at > ?")
            params.append(effective_after)
        if before_ts is not None:
            clauses.append("m.sent_at < ?")
            params.append(int(before_ts))
        where = " AND ".join(clauses)
        sql = (
            "SELECT m.*, "
            "(SELECT COUNT(*) FROM messages r "
            " WHERE r.thread_root_id = m.envelope_id) AS reply_count "
            f"FROM messages m WHERE {where} "
            "ORDER BY m.sent_at DESC, m.envelope_id DESC LIMIT ?"
        )
        params.append(max(1, min(int(limit), 200)))

        async with db.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()
        # Reverse so callers see oldest-first inside the window.
        return [
            ChannelRoot(
                message=self._row_to_msg(r),
                reply_count=int(r["reply_count"]),
            )
            for r in reversed(rows)
        ]

    async def get_thread_messages(
        self,
        root_id: str,
        limit: int = 50,
        since_envelope_id: str | None = None,
        before_ts: int | None = None,
        after_ts: int | None = None,
    ) -> list[StoredMessage]:
        """Messages belonging to a thread (the root itself plus
        every reply pointing at it), filtered the same way as
        ``get_channel_roots``. Newest-first selection, then
        reversed to oldest-first in the returned list — matches
        ``get_channel_history``'s shape so the MCP tool can format
        either the same way. Raises ``DataNotFound`` when no
        message with that envelope_id has been stored — same
        rationale as ``get_channel_roots``.
        """
        if not root_id:
            raise DataNotFound("thread root not found: (empty)")
        if not await self.has_message(root_id):
            raise DataNotFound(f"thread root not found: {root_id}")
        db = await self._ensure_db()
        lower_bounds: list[int] = []
        since_resolved = await self._resolve_since_sent_at(since_envelope_id)
        if since_resolved is not None:
            lower_bounds.append(since_resolved)
        if after_ts is not None:
            lower_bounds.append(int(after_ts))
        effective_after = max(lower_bounds) if lower_bounds else None

        clauses = ["(envelope_id = ? OR thread_root_id = ?)"]
        params: list = [root_id, root_id]
        if effective_after is not None:
            clauses.append("sent_at > ?")
            params.append(effective_after)
        if before_ts is not None:
            clauses.append("sent_at < ?")
            params.append(int(before_ts))
        where = " AND ".join(clauses)
        sql = (
            f"SELECT * FROM messages WHERE {where} "
            "ORDER BY sent_at DESC, envelope_id DESC LIMIT ?"
        )
        params.append(max(1, min(int(limit), 200)))

        async with db.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in reversed(rows)]

    async def get_last_processed_sent_at(self, root_id: str) -> int:
        """``sent_at`` of the last message in the most recently
        dispatched batch for this thread, or ``0`` if the agent has
        never processed it. Used at enqueue time to drop redelivered
        messages whose work has already been done.
        """
        if not root_id:
            return 0
        db = await self._ensure_db()
        async with db.execute(
            "SELECT last_processed_sent_at FROM thread_processing_state "
            "WHERE root_id = ?",
            (root_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def mark_thread_processed(
        self, root_id: str, sent_at: int,
    ) -> None:
        """Upsert the per-thread cursor. ``MAX(existing, new)`` so
        out-of-order writes (extremely unlikely but cheap to guard)
        never regress the cursor.
        """
        if not root_id:
            return
        db = await self._ensure_db()
        await db.execute(
            """INSERT INTO thread_processing_state (root_id, last_processed_sent_at)
               VALUES (?, ?)
               ON CONFLICT(root_id) DO UPDATE SET
                 last_processed_sent_at = MAX(
                   thread_processing_state.last_processed_sent_at,
                   excluded.last_processed_sent_at
                 )""",
            (root_id, sent_at),
        )
        await db.commit()

    async def has_channel_intro_been_prompted(self, channel_id: str) -> bool:
        """True iff the agent already had a self-introduction prompted
        for ``channel_id``. Used by ``_accept_invite`` to gate the
        synthetic system-message enqueue so a restart-time replay of
        the same pending invite doesn't fire a second intro."""
        if not channel_id:
            return False
        db = await self._ensure_db()
        async with db.execute(
            "SELECT 1 FROM channel_intro_prompted WHERE channel_id = ?",
            (channel_id,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def mark_channel_intro_prompted(self, channel_id: str) -> None:
        """Record that an intro nudge has been enqueued for
        ``channel_id``. Idempotent."""
        if not channel_id:
            return
        db = await self._ensure_db()
        await db.execute(
            """INSERT INTO channel_intro_prompted (channel_id, prompted_at)
               VALUES (?, ?)
               ON CONFLICT(channel_id) DO NOTHING""",
            (channel_id, _now_ms()),
        )
        await db.commit()

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
