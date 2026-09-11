"""The agent's own DM contact cache (allowlist + blocklist), hydrated
from puffo-server. Per-agent - the server scopes both lists to the
authenticated identity. Single read/write point for every allow/block
decision - never hit /allowlists + /blocklists ad hoc.

A keyless transport can never hydrate those signed lists, so a cache given
an optional per-agent local-state path serves (and atomically persists) a
small JSON allow/block set instead. Signed caches and keyless caches without
a path keep today's server/in-memory behavior unchanged.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
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
        local_state_path: str | os.PathLike[str] | None = None,
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
        # Keyless-only per-Agent local persistence. ``None`` (the default)
        # keeps every caller's behavior exactly as it was.
        self._local_state_path = (
            Path(local_state_path) if local_state_path is not None else None
        )
        self._local_state_loaded = False

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

    def _ensure_local_state(self) -> None:
        """Load the keyless local allow/block set once, if this cache owns it.

        A keyless transport cannot hydrate from the signed server lists, so a
        cache handed a per-agent path answers from the small JSON set that
        ``note_allowed`` / ``note_blocked`` persist. Signed caches and keyless
        caches without a path never read the file and never write one.
        """
        if self._local_state_loaded or self._local_state_path is None:
            return
        self._local_state_loaded = True
        if not self._keyless:
            return
        path = self._local_state_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._log.warning(
                "contact_cache: keyless local state %s unreadable (%s); "
                "starting empty",
                path,
                exc,
            )
            return
        if isinstance(raw, dict):
            allow = raw.get("allow")
            block = raw.get("block")
            if isinstance(allow, list):
                self._allow = {str(entry) for entry in allow if entry} - {""}
            if isinstance(block, list):
                self._block = {str(entry) for entry in block if entry} - {""}
        # The sets stay mutually exclusive even if a hand-edited file breaks
        # the invariant; an explicit block wins over an allow.
        self._allow -= self._block

    def _persist_local_state(self) -> None:
        """Atomically write the keyless local allow/block sets, if owned."""
        if self._local_state_path is None or not self._keyless:
            return
        path = self._local_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"allow": sorted(self._allow), "block": sorted(self._block)},
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _persist_or_rollback(self, allow_before: set[str], block_before: set[str]) -> None:
        """Persist keyless state, restoring the pre-mutation sets on failure.

        The mutation has already been applied in memory; if the durable write
        raises, the sets are rolled back and the error re-raised so the caller
        can retry as a full mutation. Memory must never run ahead of disk, or
        a retry would see "no change" and skip persistence entirely.
        """
        try:
            self._persist_local_state()
        except OSError:
            self._allow = allow_before
            self._block = block_before
            raise

    async def _maybe_refresh(self, *, on_miss: bool) -> None:
        age = self._age()
        if age >= self._ttl:
            await self.refresh()
        elif on_miss and age >= self._miss_refresh_interval:
            await self.refresh()

    async def is_allowed(self, slug: str) -> bool:
        if not slug:
            return False
        self._ensure_local_state()
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
        self._ensure_local_state()
        await self._maybe_refresh(on_miss=False)
        if not self._hydrated and not self._keyless:
            raise BlocklistUnavailable(
                "blocklist has never been read from the server"
            )
        return slug in self._block

    def note_allowed(self, slug: str) -> None:
        if not slug:
            return
        self._ensure_local_state()
        keyless = self._keyless
        if keyless:
            allow_before = set(self._allow)
            block_before = set(self._block)
        changed = False
        if keyless and slug in self._block:
            # The allow/block sets stay mutually exclusive; an explicit allow
            # cancels a block the operator is reversing.
            self._block.discard(slug)
            changed = True
        if slug not in self._allow:
            self._allow.add(slug)
            changed = True
        if changed and keyless:
            self._persist_or_rollback(allow_before, block_before)

    def note_blocked(self, slug: str, blocked: bool) -> None:
        if not slug:
            return
        self._ensure_local_state()
        keyless = self._keyless
        if keyless:
            allow_before = set(self._allow)
            block_before = set(self._block)
        if blocked:
            changed = False
            if keyless and slug in self._allow:
                # Mutual exclusion, mirrored from ``note_allowed``.
                self._allow.discard(slug)
                changed = True
            if slug not in self._block:
                self._block.add(slug)
                changed = True
            if changed and keyless:
                self._persist_or_rollback(allow_before, block_before)
        elif slug in self._block:
            self._block.discard(slug)
            if keyless:
                self._persist_or_rollback(allow_before, block_before)
