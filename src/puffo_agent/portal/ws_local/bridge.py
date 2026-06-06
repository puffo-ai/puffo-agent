"""Bridge the daemon's serial message consumer to a connected tool.

``PuffoCoreMessageClient._consume_queue`` dispatches one thread batch at
a time and advances its durable cursor only when the callback returns;
a callback that *raises* leaves the cursor untouched, so the server
redelivers on the next subscribe. ``WsLocalBridge.dispatch`` rides that
contract: it sends the batch as a bundle and blocks until the tool acks
(→ return → cursor advances), or until the session dies (→ raise →
cursor preserved → redelivery). Ack timing is the tool's to choose.

The bridge owns no transport or crypto — the ``WsLocalSession`` runs the
frame loop, relays replies, and judges liveness; the bridge only couples
"this batch" to "its ack".
"""

from __future__ import annotations

import asyncio

from .bundles import Bundle


class BridgeClosed(Exception):
    """Raised from ``dispatch`` when the session died before the batch was
    acked — signals the consumer to preserve the cursor for redelivery."""


class WsLocalBridge:
    def __init__(self) -> None:
        self._waiter: asyncio.Future[None] | None = None
        self._dead_reason: str | None = None

    # ── session hooks (passed to WsLocalSession) ─────────────────────────────

    async def on_acked(self, bundle: Bundle) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    async def on_dead(self, reason: str) -> None:
        self._dead_reason = reason
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_exception(BridgeClosed(reason))

    # ── consumer callback ────────────────────────────────────────────────────

    async def dispatch(self, session, root_id: str, batch: list[dict], channel_meta: dict) -> None:
        """Send ``batch`` to the tool and block until it acks. Raises
        ``BridgeClosed`` if the session is (or becomes) dead."""
        if self._dead_reason is not None:
            raise BridgeClosed(self._dead_reason)
        loop = asyncio.get_event_loop()
        self._waiter = loop.create_future()
        # Set the waiter before delivering so an ack processed on the
        # session's frame loop can't race ahead of us.
        await session.deliver_batch(root_id, batch, channel_meta)
        try:
            await self._waiter
        finally:
            self._waiter = None
