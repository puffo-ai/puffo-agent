"""Per-agent DM contact cache: the operator's allowlist + blocklist,
hydrated from puffo-server and shared by every runtime (chat / cli /
sdk / ws-local) through the one ``PuffoCoreMessageClient``.

Single read/write point for allow/block decisions — the foreign-DM
gate, the blocked-sender channel drop, and the approval / outbound
writers all go through here instead of hitting ``/allowlists`` +
``/blocklists`` ad hoc. Operator-scoped on the server, so one cache
covers all of the operator's agents on this host.
"""

from __future__ import annotations

import time
from typing import Any


class ContactCache:
    def __init__(
        self, http_client: Any, log: Any, *,
        ttl: float = 300.0, miss_refresh_interval: float = 15.0,
    ):
        self._http = http_client
        self._log = log
        self._ttl = ttl
        # A miss on the hot path refreshes at most this often, so an
        # allowlist entry added elsewhere (another device / MCP tool)
        # is honored well before the TTL without hammering the server.
        self._miss_refresh_interval = miss_refresh_interval
        self._allow: set[str] = set()
        self._block: set[str] = set()
        self._fetched_at: float = 0.0  # time.monotonic(); 0.0 = never

    async def refresh(self) -> None:
        """Replace both sets from the server. Best-effort: on failure the
        existing sets + timestamp are kept so callers fall back to
        (possibly stale) data rather than an empty cache."""
        try:
            allow = await self._http.get("/allowlists")
            block = await self._http.get("/blocklists")
        except Exception as exc:  # noqa: BLE001
            self._log.warning("contact_cache: refresh failed: %s", exc)
            return
        self._allow = {
            e.get("peer_slug", "")
            for e in (allow.get("entries") or [])
        } - {""}
        self._block = {
            b.get("id", "")
            for b in (block.get("blocks") or [])
            if b.get("target") == "user"
        } - {""}
        self._fetched_at = time.monotonic()

    def _age(self) -> float:
        return float("inf") if not self._fetched_at else time.monotonic() - self._fetched_at

    async def _maybe_refresh(self, *, on_miss: bool) -> None:
        age = self._age()
        if age >= self._ttl:
            await self.refresh()
        elif on_miss and age >= self._miss_refresh_interval:
            await self.refresh()

    async def is_allowed(self, slug: str) -> bool:
        """DM-gate path. A miss triggers a (rate-limited) refresh so an
        allowlist added on another device / via MCP is honored promptly,
        not only after the TTL."""
        if not slug:
            return False
        await self._maybe_refresh(on_miss=slug not in self._allow)
        return slug in self._allow

    async def is_blocked(self, slug: str) -> bool:
        """Channel-drop path. TTL freshness only — no miss refresh: this
        runs on every channel message and the common case is 'not
        blocked', so a new block lands within the TTL window rather than
        forcing a fetch per message."""
        if not slug:
            return False
        await self._maybe_refresh(on_miss=False)
        return slug in self._block

    def note_allowed(self, slug: str) -> None:
        if slug:
            self._allow.add(slug)

    def note_blocked(self, slug: str, blocked: bool) -> None:
        if not slug:
            return
        if blocked:
            self._block.add(slug)
        else:
            self._block.discard(slug)
