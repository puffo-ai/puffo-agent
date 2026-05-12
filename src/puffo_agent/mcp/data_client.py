"""HTTP client the MCP tools use to reach the daemon's data service.

Routes ``messages.db`` reads through the daemon so cli-docker
doesn't open a WAL'd SQLite across a bind-mount.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class StoredMessageDict:
    """Mirrors ``MessageStore.StoredMessage`` so MCP tools see the
    same shape regardless of read path."""
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
    thread_root_id: Optional[str]
    reply_to_id: Optional[str]


# Re-exported from ``message_store`` so both the network-backed
# DataClient and the in-process MessageStore (used as a duck-type
# drop-in in tests) raise the same type — the MCP tool layer can
# ``except DataNotFound`` regardless of which one it has.
from ..agent.message_store import DataNotFound  # noqa: E402  (intentional placement)


@dataclass
class ChannelRootDict:
    """Per-thread head + reply count returned by
    ``get_channel_roots``. ``message`` is the root post itself
    (``thread_root_id`` is None); ``reply_count`` is how many
    replies currently point at its ``envelope_id``.
    """
    message: "StoredMessageDict"
    reply_count: int


def _msg_from_dict(d: dict[str, Any]) -> StoredMessageDict:
    return StoredMessageDict(
        envelope_id=d.get("envelope_id", ""),
        envelope_kind=d.get("envelope_kind", "channel"),
        sender_slug=d.get("sender_slug", ""),
        channel_id=d.get("channel_id"),
        space_id=d.get("space_id"),
        recipient_slug=d.get("recipient_slug"),
        content_type=d.get("content_type", "text/plain"),
        content=d.get("content"),
        sent_at=int(d.get("sent_at", 0)),
        received_at=int(d.get("received_at", 0)),
        thread_root_id=d.get("thread_root_id"),
        reply_to_id=d.get("reply_to_id"),
    )


class DataClient:
    """Async client for the daemon's data service."""

    def __init__(self, base_url: str, agent_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def lookup_channel_space(self, channel_id: str) -> str | None:
        """Returns the space_id last seen for this channel, or None."""
        if not channel_id:
            return None
        path = (
            f"/v1/data/{urllib.parse.quote(self.agent_id, safe='')}"
            f"/channels/{urllib.parse.quote(channel_id, safe='')}/space"
        )
        session = await self._get_session()
        try:
            async with session.get(f"{self.base_url}{path}") as resp:
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "data-service: lookup_channel_space %s -> %d %s",
                        path, resp.status, body,
                    )
                    return None
                data = await resp.json()
                return data.get("space_id") or None
        except aiohttp.ClientError as exc:
            logger.warning("data-service: lookup_channel_space transport: %s", exc)
            return None

    async def get_channel_history(
        self, channel_id: str, limit: int = 20,
    ) -> list[StoredMessageDict]:
        """Recent messages for ``channel_id``, oldest first.
        ``__all__`` fetches across every channel."""
        path = (
            f"/v1/data/{urllib.parse.quote(self.agent_id, safe='')}"
            f"/messages/recent"
        )
        params = {"channel": channel_id, "limit": str(limit)}
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.base_url}{path}", params=params,
            ) as resp:
                if resp.status == 404:
                    return []
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "data-service: get_channel_history %s -> %d %s",
                        path, resp.status, body,
                    )
                    return []
                data = await resp.json()
                msgs = data.get("messages") or []
                return [_msg_from_dict(m) for m in msgs]
        except aiohttp.ClientError as exc:
            logger.warning("data-service: get_channel_history transport: %s", exc)
            return []

    async def get_channel_roots(
        self,
        channel_id: str,
        limit: int = 20,
        since_envelope_id: str | None = None,
        before_ts: int | None = None,
        after_ts: int | None = None,
    ) -> list[ChannelRootDict]:
        """Root posts in ``channel_id`` with reply counts. Filters:
        ``since_envelope_id`` (results have ``sent_at >`` that
        envelope's ``sent_at``), ``before_ts`` / ``after_ts``
        (ms-epoch bounds).
        """
        path = (
            f"/v1/data/{urllib.parse.quote(self.agent_id, safe='')}"
            f"/channels/roots"
        )
        params: dict[str, str] = {
            "channel": channel_id,
            "limit": str(limit),
        }
        if since_envelope_id:
            params["since"] = since_envelope_id
        if before_ts is not None:
            params["before"] = str(before_ts)
        if after_ts is not None:
            params["after"] = str(after_ts)
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.base_url}{path}", params=params,
            ) as resp:
                if resp.status == 404:
                    raise DataNotFound(f"channel not found: {channel_id}")
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "data-service: get_channel_roots %s -> %d %s",
                        path, resp.status, body,
                    )
                    return []
                data = await resp.json()
                roots = data.get("roots") or []
                return [
                    ChannelRootDict(
                        message=_msg_from_dict(r.get("message") or {}),
                        reply_count=int(r.get("reply_count") or 0),
                    )
                    for r in roots
                ]
        except aiohttp.ClientError as exc:
            logger.warning("data-service: get_channel_roots transport: %s", exc)
            return []

    async def get_thread_messages(
        self,
        root_id: str,
        limit: int = 50,
        since_envelope_id: str | None = None,
        before_ts: int | None = None,
        after_ts: int | None = None,
    ) -> list[StoredMessageDict]:
        """Root + every reply in the thread anchored at ``root_id``.
        Same filters as ``get_channel_roots``."""
        path = (
            f"/v1/data/{urllib.parse.quote(self.agent_id, safe='')}"
            f"/threads/{urllib.parse.quote(root_id, safe='')}"
        )
        params: dict[str, str] = {"limit": str(limit)}
        if since_envelope_id:
            params["since"] = since_envelope_id
        if before_ts is not None:
            params["before"] = str(before_ts)
        if after_ts is not None:
            params["after"] = str(after_ts)
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.base_url}{path}", params=params,
            ) as resp:
                if resp.status == 404:
                    raise DataNotFound(f"thread root not found: {root_id}")
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "data-service: get_thread_messages %s -> %d %s",
                        path, resp.status, body,
                    )
                    return []
                data = await resp.json()
                msgs = data.get("messages") or []
                return [_msg_from_dict(m) for m in msgs]
        except aiohttp.ClientError as exc:
            logger.warning("data-service: get_thread_messages transport: %s", exc)
            return []

    async def get_message_by_envelope(
        self, envelope_id: str,
    ) -> StoredMessageDict | None:
        """Single-message lookup. Returns None when not stored."""
        if not envelope_id:
            return None
        path = (
            f"/v1/data/{urllib.parse.quote(self.agent_id, safe='')}"
            f"/messages/{urllib.parse.quote(envelope_id, safe='')}"
        )
        session = await self._get_session()
        try:
            async with session.get(f"{self.base_url}{path}") as resp:
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "data-service: get_message_by_envelope %s -> %d %s",
                        path, resp.status, body,
                    )
                    return None
                data = await resp.json()
                m = data.get("message")
                if not isinstance(m, dict):
                    return None
                return _msg_from_dict(m)
        except aiohttp.ClientError as exc:
            logger.warning(
                "data-service: get_message_by_envelope transport: %s", exc,
            )
            return None
