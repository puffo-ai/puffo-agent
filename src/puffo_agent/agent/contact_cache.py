"""The agent's own DM contact cache (allowlist + blocklist), hydrated
from puffo-server. Per-agent - the server scopes both lists to the
authenticated identity. Single read/write point for every allow/block
decision - never hit /allowlists + /blocklists ad hoc.
"""

from __future__ import annotations

import time
from typing import Any


class BlocklistUnavailable(RuntimeError):
    """The blocklist has no answer to give, so no admit decision is safe.

    Raised only by the one case where silence is indistinguishable from
    "nobody is blocked": a signed agent that has never hydrated and whose
    refresh just failed. The caller holds the delivery instead of admitting
    it — the server redelivers, so a hold costs a retry, whereas answering
    "not blocked" from an empty cache puts a blocked account in front of the
    model. Keyless never raises this: its empty list is a real, explicit
    local-state answer, not a missing one.
    """


class ContactCache:
    def __init__(
        self,
        http_client: Any,
        log: Any,
        *,
        ttl: float = 300.0,
        miss_refresh_interval: float = 15.0,
    ):
        self._http = http_client
        self._log = log
        self._ttl = ttl
        self._miss_refresh_interval = miss_refresh_interval
        self._allow: set[str] = set()
        self._block: set[str] = set()
        self._fetched_at: float = 0.0
        self._degrade_logged = False
        # True once a server read has actually landed. Distinguishes "the
        # server says these are blocked" from "we have never been told".
        self._hydrated = False

    @property
    def _keyless(self) -> bool:
        return bool(getattr(self._http, "keyless", False))

    async def refresh(self) -> None:
        """Replace both sets, preserving stale data when refresh fails.

        ``/allowlists`` and ``/blocklists`` are subkey-signed and have no
        keyless counterpart, so a keyless agent can never hydrate from
        the server. It serves purely local state instead of retrying a
        request that cannot succeed — logged once so the degrade is
        visible. Either way a refresh failure is non-fatal: every
        allow/block decision still answers from what is known.
        """
        if self._keyless:
            if not self._degrade_logged:
                self._degrade_logged = True
                self._log.info(
                    "contact_cache: keyless transport cannot read "
                    "/allowlists or /blocklists; serving local state only"
                )
            return
        try:
            allow = await self._http.get("/allowlists")
            block = await self._http.get("/blocklists")
        except Exception as exc:  # noqa: BLE001
            self._log.warning("contact_cache: refresh failed: %s", exc)
            return
        self._allow = {
            entry.get("peer_slug", "")
            for entry in (allow.get("entries") or [])
        } - {""}
        self._block = {
            entry.get("id", "")
            for entry in (block.get("blocks") or [])
            if entry.get("target") == "user"
        } - {""}
        self._fetched_at = time.monotonic()
        self._hydrated = True

    def _age(self) -> float:
        if not self._fetched_at:
            return float("inf")
        return time.monotonic() - self._fetched_at

    async def _maybe_refresh(self, *, on_miss: bool) -> None:
        age = self._age()
        if age >= self._ttl:
            await self.refresh()
        elif on_miss and age >= self._miss_refresh_interval:
            await self.refresh()

    async def is_allowed(self, slug: str) -> bool:
        if not slug:
            return False
        await self._maybe_refresh(on_miss=slug not in self._allow)
        return slug in self._allow

    async def is_blocked(self, slug: str) -> bool:
        """Whether ``slug`` is blocked, or ``BlocklistUnavailable`` if unknown.

        ``refresh`` preserves stale data when a fetch fails, which is the
        right call once there *is* stale data. At cold start there is none:
        the sets are in-memory only, so a process that restarts during a
        server incident would otherwise report every blocked account as
        unblocked — and this gate is the only thing filtering a blocked
        sender's channel traffic. So a signed agent that has never hydrated
        says "unavailable" rather than "not blocked".
        """
        if not slug:
            return False
        await self._maybe_refresh(on_miss=False)
        if not self._hydrated and not self._keyless:
            raise BlocklistUnavailable(
                "blocklist has never been read from the server"
            )
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
