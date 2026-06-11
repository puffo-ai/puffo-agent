"""In-process replacement for ``mcp.data_client.DataClient``.

The MCP subprocess data_client speaks HTTP to the daemon's data
service so the MCP can read SQLite + the profile cache from a
separate process. In ws-local context the dispatch runs inside the
daemon, so we wire straight to the underlying store + worker client
— no HTTP round-trip needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...agent.message_store import ChannelRoot, MessageStore, StoredMessage
    from ...agent.puffo_core_client import PuffoCoreMessageClient


class InProcessDataClient:
    """Mirrors the read surface ``puffo_core_tools`` consumes.

    Method names + signatures match ``mcp.data_client.DataClient`` so
    the existing tool implementations don't notice the swap.
    """

    def __init__(self, store: "MessageStore", client: "PuffoCoreMessageClient") -> None:
        self._store = store
        self._client = client

    async def close(self) -> None:
        return None

    async def lookup_channel_space(self, channel_id: str) -> str | None:
        return await self._store.lookup_channel_space(channel_id)

    async def get_channel_roots(
        self,
        channel_id: str,
        limit: int = 20,
        since_envelope_id: str | None = None,
        before_ts: int | None = None,
        after_ts: int | None = None,
    ) -> list["ChannelRoot"]:
        return await self._store.get_channel_roots(
            channel_id=channel_id,
            limit=limit,
            since_envelope_id=since_envelope_id,
            before_ts=before_ts,
            after_ts=after_ts,
        )

    async def get_dm_history(
        self, peer_slug: str, limit: int = 20, before: int | None = None,
    ) -> list["StoredMessage"]:
        return await self._store.get_dm_history(peer_slug, limit, before)

    async def get_thread_messages(
        self,
        root_id: str,
        limit: int = 50,
        since_envelope_id: str | None = None,
        before_ts: int | None = None,
        after_ts: int | None = None,
    ) -> list["StoredMessage"]:
        return await self._store.get_thread_messages(
            root_id=root_id,
            limit=limit,
            since_envelope_id=since_envelope_id,
            before_ts=before_ts,
            after_ts=after_ts,
        )

    async def get_message_by_envelope(self, envelope_id: str) -> Any:
        return await self._store.get_message_by_envelope(envelope_id)

    async def update_profile_cache(
        self, slug: str, display_name: str, avatar_url: str,
    ) -> None:
        self._client.set_profile(slug, display_name, avatar_url)
