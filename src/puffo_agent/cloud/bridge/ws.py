"""Combined Bridge transport: HTTP outbound + WebSocket inbound.

Inbound mirrors the puffo-server WS shape (auth handshake → stream of
JSON frames) but authenticates with the session token instead of a
keystore signature, and frames arrive already decrypted. The reconnect
loop lives in ``BridgeMessageClient`` (it raises on a dropped socket, as
an E2B pause cuts it). The wire format tracks the not-yet-locked Bridge
contract; it runs in prod against the real Bridge, while unit tests use
``StubBridgeClient``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import websockets

from .client import BridgeConfig, BridgeInboundEvent, OnEvent
from .http import HttpBridgeOutbound

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0


def _http_to_ws(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


class HttpWsBridgeClient(HttpBridgeOutbound):
    """Full ``BridgeClient``: HTTP send (inherited) + WS receive."""

    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self.ws_url = _http_to_ws(config.bridge_url.rstrip("/")) + "/v1/inbound/subscribe"
        self._ws: Any = None
        self._running = False

    async def run(self, on_event: OnEvent) -> None:
        self._running = True
        async with websockets.connect(
            self.ws_url, open_timeout=CONNECT_TIMEOUT,
        ) as ws:
            self._ws = ws
            await ws.send(json.dumps({
                "type": "connect",
                "token": self.config.session_token,
                "agent_id": self.config.agent_id,
            }))
            async for raw in ws:
                if not self._running:
                    break
                try:
                    frame = json.loads(raw)
                except (ValueError, TypeError):
                    logger.warning("bridge inbound: unparseable frame dropped")
                    continue
                if frame.get("type") != "messages":
                    continue
                await on_event(BridgeInboundEvent(
                    root_id=frame.get("root_id", ""),
                    messages=list(frame.get("messages", [])),
                    channel_meta=frame.get("channel_meta", {}),
                ))

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
