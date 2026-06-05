"""Serve one localhost WS connection end to end.

Glue, but dependency-injected so the decision logic stays unit-testable:
read the ``connect`` frame, authenticate by decrypting the export, gate
on "agent is managed here and is a ws-local runtime", claim the
single-WS slot, hand back the live agent context, then run the tool
attached — the session frame-loop and the message consumer together —
until either ends, and always free the slot on the way out.

The daemon supplies the collaborators: ``make_session`` builds the
session wired to the ``bridge``; ``start_consumer`` runs the agent's
``PuffoCoreMessageClient.listen`` with the bridge's dispatch callback;
``agent_context`` reads the live role + profile.md. Attaching a tool is
what brings the agent online (point 4) — no tool, no consumer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from ..export import ImportPackError
from .auth import AuthedAgent, AuthError
from .bridge import WsLocalBridge
from .protocol import Connect, Connected, Error, ProtocolError, decode_inbound, encode
from .registry import SessionRegistry
from .session import Transport, WsLocalSession

logger = logging.getLogger(__name__)


async def serve_connection(
    transport: Transport,
    *,
    authenticate: Callable[[bytes, str], AuthedAgent],
    is_servable: Callable[[str], bool],
    agent_context: Callable[[str], Awaitable[dict]],
    registry: SessionRegistry[WsLocalSession],
    make_session: Callable[[AuthedAgent, str, Transport, WsLocalBridge], WsLocalSession],
    start_consumer: Callable[[AuthedAgent, Callable], Awaitable[None]],
    new_session_id: Callable[[], str],
    base64_decode: Callable[[str], bytes],
) -> None:
    raw = await transport.recv()
    if raw is None:
        return
    try:
        frame = decode_inbound(raw)
    except ProtocolError as exc:
        await _reject(transport, f"malformed handshake: {exc}")
        return
    if not isinstance(frame, Connect):
        await _reject(transport, "first frame must be `connect`")
        return

    try:
        blob = base64_decode(frame.bundle)
    except Exception:
        await _reject(transport, "bundle is not valid base64")
        return
    try:
        authed = authenticate(blob, frame.password)
    except (AuthError, ImportPackError) as exc:
        await _reject(transport, f"authentication failed: {exc}")
        return

    if not is_servable(authed.slug):
        await _reject(transport, f"{authed.slug!r} is not a ws-local agent on this daemon")
        return

    session_id = new_session_id()
    bridge = WsLocalBridge()
    session = make_session(authed, session_id, transport, bridge)
    if not registry.acquire(authed.slug, session):
        await _reject(transport, f"{authed.slug!r} already has an active connection")
        return

    try:
        context = await agent_context(authed.slug)
        await transport.send(encode(Connected(session_id, context)))
        await _run_attached(authed, session, bridge, start_consumer)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ws-local %s: serve failed: %s", authed.slug, exc)
        await _safe_close(transport)
    registry.release(authed.slug, session)


async def _run_attached(
    authed: AuthedAgent,
    session: WsLocalSession,
    bridge: WsLocalBridge,
    start_consumer: Callable[[AuthedAgent, Callable], Awaitable[None]],
) -> None:
    """Run the session frame-loop and the message consumer concurrently.
    The session ends when the tool disconnects; the consumer ends if the
    server stream drops. Whichever ends first tears down the other."""

    async def on_message(root_id, batch, channel_meta):
        await bridge.dispatch(session, root_id, batch, channel_meta)

    session_task = asyncio.ensure_future(session.run())
    consumer_task = asyncio.ensure_future(start_consumer(authed, on_message))
    try:
        await asyncio.wait(
            {session_task, consumer_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in (session_task, consumer_task):
            task.cancel()
        for task in (session_task, consumer_task):
            try:
                await task
            except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
                if not isinstance(exc, asyncio.CancelledError):
                    logger.debug("ws-local %s: attached task ended: %s", authed.slug, exc)


async def _reject(transport: Transport, reason: str) -> None:
    try:
        await transport.send(encode(Error(reason)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("ws-local: reject send failed: %s", exc)
    await _safe_close(transport)


async def _safe_close(transport: Transport) -> None:
    try:
        await transport.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("ws-local: close failed: %s", exc)
