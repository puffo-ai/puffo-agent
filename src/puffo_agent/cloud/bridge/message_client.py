"""Posture-B inbound/outbound message client for cli-cloud.

The sandbox holds no keys: the Bridge decrypts inbound messages and
pushes them as plaintext events, and encrypts/signs/forwards our
outbound sends. This client wires that transport to the same
MessageStore + ``on_message`` dispatch the harness path expects, with
reconnect-on-wake so an E2B resume re-establishes the stream.

Rich server-side flows the local client runs inline (invite
auto-accept, leave gating, cert/profile sync) need the keys, so they
live Bridge-side and ride the Bridge contract rather than being
reimplemented here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from .client import BridgeClient, BridgeInboundEvent

logger = logging.getLogger(__name__)

RECONNECT_BACKOFF_SECONDS = 5.0


class _BridgeHttpShim:
    """Minimal ``http``-shaped object for StatusReporter. Status posts
    are forwarded to the Bridge (which signs them); reads aren't served
    here — cli-cloud history reads go through the local MessageStore."""

    def __init__(self, bridge: BridgeClient) -> None:
        self._bridge = bridge

    async def post(self, path: str, body: Any) -> dict:
        await self._bridge.report_status({"path": path, "body": body})
        return {}

    async def get(self, path: str) -> dict:
        raise NotImplementedError(
            "cli-cloud reads go through the Bridge; direct server GET is unavailable"
        )


class BridgeMessageClient:
    def __init__(
        self,
        *,
        bridge: BridgeClient,
        message_store: Any,
        slug: str,
        operator_slug: str = "",
        space_id: str = "",
        workspace: str = "",
    ) -> None:
        self.bridge = bridge
        self.store = message_store
        self.slug = slug
        self.operator_slug = operator_slug
        self.space_id = space_id
        self.workspace = workspace
        # No keystore in the sandbox — the Bridge holds the keys.
        self.keystore = None
        self.http = _BridgeHttpShim(bridge)
        self._last_dm_sender = ""
        self._profile_cache: dict[str, tuple[str, str]] = {}
        self._stop = asyncio.Event()

    def set_profile(self, slug: str, display_name: str, avatar_url: str) -> None:
        self._profile_cache[slug] = (display_name, avatar_url)

    async def send_fallback_message(
        self,
        channel_id: str,
        text: str,
        root_id: str = "",
        is_visible_to_human: bool = True,
    ) -> None:
        channel = channel_id or (f"@{self._last_dm_sender}" if self._last_dm_sender else "")
        if not channel:
            logger.warning("bridge client %s: no channel for fallback message", self.slug)
            return
        await self.bridge.send_message(
            channel=channel,
            text=text,
            is_visible_to_human=is_visible_to_human,
            root_id=root_id,
        )

    async def listen(
        self,
        on_message: Callable[..., Awaitable[Any]],
        on_api_error_retry: Optional[Callable[..., Awaitable[Any]]] = None,
        on_api_error_abandon: Optional[Callable[..., Awaitable[Any]]] = None,
        on_turn_success: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        """Reconnect-on-wake loop. ``bridge.run`` raises on a dropped
        socket (an E2B pause cuts it); we back off and re-establish,
        mirroring the puffo-server WS reconnect."""

        async def on_event(event: BridgeInboundEvent) -> None:
            await self._dispatch(
                event, on_message, on_api_error_abandon, on_turn_success,
            )

        while not self._stop.is_set():
            try:
                await self.bridge.run(on_event)
                return  # clean return = stop requested
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                if self._stop.is_set():
                    return
                logger.warning(
                    "bridge client %s: stream dropped (%s); reconnecting in %.0fs",
                    self.slug, exc, RECONNECT_BACKOFF_SECONDS,
                )
                await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)

    async def _dispatch(
        self,
        event: BridgeInboundEvent,
        on_message: Callable[..., Awaitable[Any]],
        on_api_error_abandon: Optional[Callable[..., Awaitable[Any]]],
        on_turn_success: Optional[Callable[..., Awaitable[Any]]],
    ) -> None:
        # Persist the already-decrypted batch so the data-service read
        # tools (get_channel_history etc.) see it, same as the local path.
        for msg in event.messages:
            try:
                await self.store.store(msg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("bridge client %s: store failed: %s", self.slug, exc)
            if msg.get("envelope_kind") == "dm":
                sender = msg.get("sender_slug", "")
                if sender and sender != self.slug:
                    self._last_dm_sender = sender
        try:
            await on_message(event.root_id, event.messages, event.channel_meta)
        except Exception as exc:  # noqa: BLE001
            if on_api_error_abandon is not None:
                await on_api_error_abandon(
                    event.root_id, event.messages, event.channel_meta, 1,
                )
            else:
                logger.error("bridge client %s: turn failed: %s", self.slug, exc)
            return
        if on_turn_success is not None:
            await on_turn_success(event.root_id, event.messages, event.channel_meta)

    async def stop(self) -> None:
        self._stop.set()
        try:
            await self.bridge.stop()
            await self.bridge.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("bridge client %s: stop failed: %s", self.slug, exc)
