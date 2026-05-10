from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine

import websockets

from .encoding import base64url_encode, generate_nonce
from .http_client import PuffoCoreHttpClient
from .keystore import KeyStore, decode_secret
from .primitives import Ed25519KeyPair

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0
MAX_BACKOFF = 30
INITIAL_BACKOFF = 1


def _http_to_ws(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def _now_ms() -> int:
    return int(time.time() * 1000)


MessageHandler = Callable[[dict], Coroutine[Any, Any, None]]
EventHandler = Callable[[str, dict], Coroutine[Any, Any, None]]
CertHandler = Callable[[dict], Coroutine[Any, Any, None]]


class PuffoCoreWsClient:
    def __init__(
        self,
        server_url: str,
        keystore: KeyStore,
        slug: str,
        http_client: PuffoCoreHttpClient,
    ):
        self.ws_url = _http_to_ws(server_url.rstrip("/")) + "/subscribe"
        self.keystore = keystore
        self.slug = slug
        self.http_client = http_client
        self._ws: Any = None
        self._running = False
        self.session_id: str | None = None

        self.on_message: MessageHandler | None = None
        self.on_event: EventHandler | None = None
        self.on_cert_update: CertHandler | None = None

    def _build_connect_frame(self) -> str:
        sess = self.keystore.load_session(self.slug)
        key = Ed25519KeyPair.from_secret_bytes(decode_secret(sess.subkey_secret_key))
        nonce = generate_nonce()
        timestamp = _now_ms()

        message = f"ws-connect\n{self.slug}\n{sess.subkey_id}\n{nonce}\n{timestamp}".encode()
        sig = key.sign(message)

        return json.dumps({
            "type": "connect",
            "slug": self.slug,
            "subkey_id": sess.subkey_id,
            "nonce": nonce,
            "timestamp": timestamp,
            "signature": base64url_encode(sig),
        })

    async def _handshake(self, ws: Any) -> str:
        frame = self._build_connect_frame()
        await ws.send(frame)

        raw = await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT)
        resp = json.loads(raw)
        if resp.get("type") != "connected":
            raise ConnectionError(f"Unexpected handshake response: {resp}")
        return resp["session_id"]

    async def _catchup(self) -> None:
        try:
            data = await self.http_client.get("/messages/pending")
            messages = data.get("messages", [])
            if not messages:
                return
            logger.info("Catch-up: %d pending messages", len(messages))
            envelope_ids = []
            for item in messages:
                envelope = item.get("envelope", item)
                if self.on_message:
                    try:
                        await self.on_message(envelope)
                    except Exception:
                        logger.exception("on_message callback failed during catch-up")
                eid = envelope.get("envelope_id")
                if eid:
                    envelope_ids.append(eid)
            if envelope_ids and self._ws:
                await self._send_ack(envelope_ids)
        except Exception:
            logger.exception("Catch-up failed")

    async def _send_ack(self, envelope_ids: list[str]) -> None:
        if self._ws:
            await self._ws.send(json.dumps({
                "type": "ack",
                "envelope_ids": envelope_ids,
            }))

    async def _handle_frame(self, raw: str) -> None:
        msg = json.loads(raw)
        msg_type = msg.get("type")

        if msg_type == "ping":
            await self._ws.send(json.dumps({"type": "pong"}))

        elif msg_type == "message":
            envelope = msg.get("envelope", {})
            if self.on_message:
                try:
                    await self.on_message(envelope)
                except Exception:
                    logger.exception("on_message callback failed")
            eid = envelope.get("envelope_id")
            if eid:
                await self._send_ack([eid])

        elif msg_type == "cert_update":
            if self.on_cert_update:
                try:
                    await self.on_cert_update(msg.get("entry", {}))
                except Exception:
                    logger.exception("on_cert_update callback failed")

        elif msg_type == "event":
            if self.on_event:
                try:
                    await self.on_event(msg.get("scope", ""), msg.get("event", {}))
                except Exception:
                    logger.exception("on_event callback failed")

    async def _listen_loop(self) -> None:
        async for raw in self._ws:
            await self._handle_frame(raw)

    async def connect_once(self) -> None:
        await self.http_client._ensure_subkey()
        async with websockets.connect(self.ws_url) as ws:
            self._ws = ws
            self.session_id = await self._handshake(ws)
            logger.info("WS connected, session=%s", self.session_id)
            await self._catchup()
            await self._listen_loop()

    async def run(self) -> None:
        self._running = True
        backoff = INITIAL_BACKOFF
        while self._running:
            try:
                await self.connect_once()
                backoff = INITIAL_BACKOFF
            except (
                websockets.exceptions.ConnectionClosed,
                ConnectionError,
                OSError,
                asyncio.TimeoutError,
            ):
                if not self._running:
                    break
                logger.warning("WS disconnected, reconnecting in %ds", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception:
                if not self._running:
                    break
                logger.exception("WS unexpected error, reconnecting in %ds", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
        self._ws = None
        self.session_id = None

    def stop(self) -> None:
        self._running = False
        if self._ws:
            asyncio.ensure_future(self._ws.close())
