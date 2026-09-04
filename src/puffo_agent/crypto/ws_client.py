from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Coroutine, Mapping, TypedDict

import websockets
import websockets.exceptions  # websockets>=16 lazy-loads submodules; bare `import websockets` doesn't bind `.exceptions`

from .encoding import base64url_encode, generate_nonce
from .http_client import PuffoCoreHttpClient, heal_if_dead_executor
from .http_session import create_remote_ssl_context
from .keystore import KeyStore, decode_secret
from .primitives import Ed25519KeyPair
from ..tasks import spawn

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0
MAX_BACKOFF = 30
INITIAL_BACKOFF = 1
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _http_to_ws(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def _now_ms() -> int:
    return int(time.time() * 1000)


class ServerDelivery(TypedDict):
    seq: int
    envelope: dict[str, Any]


class TransportOutcome(str, Enum):
    ACK = "ack"
    DEFER = "defer"
    HOLD = "hold"


@dataclass(frozen=True)
class DeliveryResult:
    outcome: TransportOutcome
    envelope_id: str | None
    reason: str


MessageHandler = Callable[[ServerDelivery], Awaitable[TransportOutcome]]
EventHandler = Callable[[str, dict], Coroutine[Any, Any, None]]
CertHandler = Callable[[dict], Coroutine[Any, Any, None]]
SpaceMembershipHandler = Callable[[str], Coroutine[Any, Any, None]]


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
        self.on_space_membership_changed: SpaceMembershipHandler | None = None
        self.on_channel_update: Callable[[dict], Coroutine[Any, Any, None]] | None = None
        # Fires after every (re)connect handshake, before catch-up.
        self.on_connect: Callable[[], Coroutine[Any, Any, None]] | None = None
        # Sync observer for reconnect health: (healthy, consecutive_failures).
        # Fired on every failed reconnect attempt and once on the reconnect
        # that ends a failure streak. Policy (thresholds, health writes)
        # belongs to the listener; this client only counts.
        self.on_transport_state: Callable[[bool, int], None] | None = None
        self._reconnect_failures = 0
        self._catchup_lock = asyncio.Lock()

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

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT)
            resp = json.loads(raw)
            if not isinstance(resp, Mapping) or resp.get("type") != "connected":
                raise ValueError
            session_id = resp.get("session_id")
            if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(
                session_id
            ):
                raise ValueError
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ConnectionError("websocket handshake protocol error") from exc
        return session_id

    async def _catchup(self) -> None:
        """Drain `/messages/pending` — rows the server held while we
        were offline. MUST run after ``_handshake`` (it registers the
        session); ordering is register → pending fetch → live listen.

        An incomplete drain does not abort the connection. Raising here
        aborts before ``_listen_loop``, so one envelope this receiver can
        never own — or one transient failure on ``/messages/pending`` —
        would reconnect, fail the same way, and never reach live listen
        again: every later message wedged behind one. Unacked envelopes are
        redelivered on the next drain, so continuing costs a retry and
        losing the connection costs everything after it.
        """
        if not await self._run_catchup():
            logger.warning(
                "Catch-up incomplete (session=%s) — continuing to live "
                "listen; unacked envelopes are redelivered on the next drain",
                self.session_id or "<pre-handshake>",
            )

    async def recover_pending_until(self, envelope_id: str) -> bool:
        """Use the signed pending endpoint when a held WS watermark is late."""
        if not envelope_id:
            return False
        return await self._run_catchup(required_envelope_id=envelope_id)

    async def _run_catchup(self, required_envelope_id: str | None = None) -> bool:
        found_required = False
        try:
            async with self._catchup_lock:
                data = await self.http_client.get("/messages/pending")
                messages = data.get("messages", [])
                # Log on every reconnect, even zero — a silent reconnect
                # should still leave one line (else it's indistinguishable
                # from a no-op).
                logger.info(
                    "Catch-up: %d pending messages (session=%s)",
                    len(messages), self.session_id or "<pre-handshake>",
                )
                if not messages:
                    return required_envelope_id is None
                # HTTP, not WS-frame (no pong until the listen loop → WS dies
                # mid-catch-up); chunked so progress survives a death.
                envelope_ids = []
                held = 0
                blocked = False
                for item in messages:
                    result = await self.dispatch_delivery(item)
                    if result.outcome is TransportOutcome.DEFER:
                        # Reliably classified local deferrals, such as a
                        # foreign DM awaiting operator approval, do not own
                        # their Server ACK yet but also do not block unrelated
                        # later deliveries.
                        continue
                    if result.outcome is not TransportOutcome.ACK:
                        # This one envelope is not ours to own — an
                        # undecryptable payload, an unreadable blocklist, a
                        # lifecycle conflict.
                        held += 1
                        logger.warning(
                            "Catch-up held one delivery (envelope_id=%s "
                            "reason=%s) — left unacked",
                            result.envelope_id or "<unknown>",
                            result.reason,
                        )
                        if required_envelope_id is not None:
                            # Held recovery answers "is everything through
                            # this exact envelope durably mine?", so a gap
                            # makes the answer no. It must stop, not skip.
                            blocked = True
                            break
                        # A plain drain has no watermark to protect: acks
                        # here are per envelope id, so leaving this one
                        # unacked proves nothing about the ones after it.
                        # Stopping would let a single permanently unopenable
                        # envelope wedge every message behind it, forever.
                        continue
                    if result.envelope_id:
                        envelope_ids.append(result.envelope_id)
                        if result.envelope_id == required_envelope_id:
                            found_required = True
                    if len(envelope_ids) >= 25:
                        await self._ack_http(envelope_ids)
                        envelope_ids = []
                    if found_required:
                        break
                if envelope_ids:
                    await self._ack_http(envelope_ids)
                if held:
                    logger.warning(
                        "Catch-up finished with %d held delivery/deliveries "
                        "(session=%s); the server redelivers them",
                        held,
                        self.session_id or "<pre-handshake>",
                    )
                if blocked:
                    return False
        except Exception:
            logger.exception("Catch-up failed")
            return False
        return found_required if required_envelope_id is not None else True

    async def _ack_http(self, envelope_ids: list[str]) -> None:
        try:
            await self.http_client.post(
                "/messages/ack", {"envelope_ids": list(envelope_ids)},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "catch-up HTTP ack failed (%d ids): %s", len(envelope_ids), exc,
            )

    async def _send_ack(self, envelope_ids: list[str]) -> None:
        if self._ws:
            await self._ws.send(json.dumps({
                "type": "ack",
                "envelope_ids": envelope_ids,
            }))

    async def dispatch_delivery(self, item: Mapping[str, Any]) -> DeliveryResult:
        """Validate and classify one Server delivery without sending an ACK.

        Both HTTP catch-up and live WebSocket delivery use this seam.  Callers
        may also reuse it for another durable delivery source.
        """
        if not isinstance(item, Mapping):
            return DeliveryResult(TransportOutcome.HOLD, None, "delivery is not a mapping")
        seq = item.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
            return DeliveryResult(TransportOutcome.HOLD, None, "invalid server sequence")
        raw_envelope = item.get("envelope")
        if not isinstance(raw_envelope, Mapping):
            return DeliveryResult(TransportOutcome.HOLD, None, "invalid envelope")
        envelope = dict(raw_envelope)
        envelope_id = envelope.get("envelope_id")
        if not isinstance(envelope_id, str) or not envelope_id.strip():
            return DeliveryResult(TransportOutcome.HOLD, None, "invalid envelope id")
        if self.on_message is None:
            return DeliveryResult(TransportOutcome.HOLD, envelope_id, "no message handler")

        delivery: ServerDelivery = {"seq": seq, "envelope": envelope}
        try:
            outcome = await self.on_message(delivery)
        except Exception:
            logger.exception("on_message callback failed")
            return DeliveryResult(TransportOutcome.HOLD, envelope_id, "handler exception")
        if outcome is TransportOutcome.ACK:
            return DeliveryResult(TransportOutcome.ACK, envelope_id, "handler committed")
        if outcome is TransportOutcome.DEFER:
            return DeliveryResult(
                TransportOutcome.DEFER,
                envelope_id,
                "handler durably deferred",
            )
        if outcome is TransportOutcome.HOLD:
            return DeliveryResult(TransportOutcome.HOLD, envelope_id, "handler held")
        return DeliveryResult(TransportOutcome.HOLD, envelope_id, "unexpected handler result")

    async def _handle_frame(self, raw: str) -> None:
        msg = json.loads(raw)
        if not isinstance(msg, Mapping):
            logger.warning("ignoring malformed WebSocket frame")
            return
        msg_type = msg.get("type")

        if msg_type == "ping":
            await self._ws.send(json.dumps({"type": "pong"}))

        elif msg_type == "message":
            result = await self.dispatch_delivery(msg)
            if result.outcome is TransportOutcome.ACK and result.envelope_id:
                await self._send_ack([result.envelope_id])
            elif result.outcome is TransportOutcome.HOLD:
                # Stop this live stream at the first delivery we could not
                # durably own. Reconnecting re-enters the ordered signed
                # pending catch-up path before any later live delivery.
                raise ConnectionError(
                    "live delivery stopped at an unacknowledged envelope"
                )

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

        elif msg_type == "space_membership_changed":
            space_id = msg.get("space_id")
            if self.on_space_membership_changed and isinstance(space_id, str):
                try:
                    await self.on_space_membership_changed(space_id)
                except Exception:
                    logger.exception("on_space_membership_changed callback failed")

        elif msg_type == "channel_update":
            if self.on_channel_update:
                try:
                    await self.on_channel_update(msg)
                except Exception:
                    logger.exception("on_channel_update callback failed")

    async def _listen_loop(self) -> None:
        async for raw in self._ws:
            await self._handle_frame(raw)

    async def connect_once(self) -> None:
        await self.http_client._ensure_subkey()
        ssl_context = (
            create_remote_ssl_context() if self.ws_url.startswith("wss://") else None
        )
        async with websockets.connect(self.ws_url, ssl=ssl_context) as ws:
            self._ws = ws
            self.session_id = await self._handshake(ws)
            logger.info("[%s] WS connected, session=%s", self.slug, self.session_id)
            if self._reconnect_failures:
                self._reconnect_failures = 0
                self._notify_transport_state(healthy=True)
            if self.on_connect:
                try:
                    await self.on_connect()
                except Exception:
                    logger.exception("on_connect callback failed")
            await self._catchup()
            await self._listen_loop()

    async def run(self) -> None:
        self._running = True
        backoff = INITIAL_BACKOFF
        while self._running:
            try:
                await self.connect_once()
                backoff = INITIAL_BACKOFF
            except websockets.exceptions.ConnectionClosed as exc:
                if not self._running:
                    break
                logger.warning(
                    "[%s] WS reconnect category=connection_closed exception=%s "
                    "retry_delay=%ds",
                    self.slug, type(exc).__name__, backoff,
                )
                self._note_reconnect_failure()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except asyncio.TimeoutError as exc:
                if not self._running:
                    break
                logger.warning(
                    "[%s] WS reconnect category=timeout exception=%s "
                    "retry_delay=%ds",
                    self.slug, type(exc).__name__, backoff,
                )
                self._note_reconnect_failure()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except ConnectionError as exc:
                if not self._running:
                    break
                logger.warning(
                    "[%s] WS reconnect category=protocol exception=%s "
                    "retry_delay=%ds",
                    self.slug, type(exc).__name__, backoff,
                )
                self._note_reconnect_failure()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except OSError as exc:
                if not self._running:
                    break
                logger.warning(
                    "[%s] WS reconnect category=transport exception=%s "
                    "retry_delay=%ds",
                    self.slug, type(exc).__name__, backoff,
                )
                self._note_reconnect_failure()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except RuntimeError as exc:
                if not self._running:
                    break
                # A valid subkey means connect_once() made no HTTP request
                # before ``websockets.connect`` hit ``loop.getaddrinfo``,
                # so the dead default executor must be healed here — the
                # HTTP wrapper's heal never ran (8/30 incident).
                healed = heal_if_dead_executor(exc)
                logger.warning(
                    "[%s] WS reconnect category=%s exception=%s "
                    "retry_delay=%ds",
                    self.slug,
                    "dead_executor_healed" if healed else "unexpected",
                    type(exc).__name__, backoff,
                )
                self._note_reconnect_failure()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "[%s] WS reconnect category=unexpected exception=%s "
                    "retry_delay=%ds",
                    self.slug, type(exc).__name__, backoff,
                )
                self._note_reconnect_failure()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
        self._ws = None
        self.session_id = None

    def _note_reconnect_failure(self) -> None:
        self._reconnect_failures += 1
        self._notify_transport_state(healthy=False)

    def _notify_transport_state(self, *, healthy: bool) -> None:
        if self.on_transport_state is None:
            return
        try:
            self.on_transport_state(healthy, self._reconnect_failures)
        except Exception:
            logger.exception("on_transport_state callback failed")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            spawn(self._ws.close(), name="ws.close")
