"""aiohttp WS route for ws-local tools: ``GET /v1/ws-local``.

Loopback-only (the bridge binds loopback). Auth is the handshake's own
``.puffoagent`` decryption — this path is exempt from the bridge's HTTP
signature middleware. The handler wires the hub's per-agent attach point
into ``serve_connection``: the session relays replies + judges liveness,
the consumer (``client.listen``) feeds batches and advances the cursor
on ack.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path

from aiohttp import web

from .aiohttp_transport import AiohttpTransport
from .auth import authenticate_bundle
from .bundles import BundleQueue
from .endpoint import serve_connection
from .hub import WsLocalHub
from .protocol import Error, encode
from .session import Transport, WsLocalSession

logger = logging.getLogger(__name__)

WS_LOCAL_PATH = "/v1/ws-local"


async def handle_ws_local(request: web.Request) -> web.WebSocketResponse:
    hub: WsLocalHub | None = request.app.get("ws_local_hub")
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    transport = AiohttpTransport(ws)
    if hub is None:
        await transport.send(encode(Error("ws-local is not enabled on this daemon")))
        await transport.close()
        return ws
    await serve_attached(transport, hub)
    return ws


async def serve_attached(transport: Transport, hub: WsLocalHub) -> None:
    """Wire the hub into ``serve_connection``. Split out from the aiohttp
    boilerplate so it's exercisable over any transport."""

    async def agent_context(slug: str) -> dict:
        point = hub.get(slug)
        if point is None:
            return {}
        cfg = point.agent_cfg
        try:
            profile_md = Path(cfg.resolve_profile_path()).read_text(encoding="utf-8")
        except OSError:
            profile_md = ""
        return {
            "slug": slug,
            "display_name": getattr(cfg, "display_name", ""),
            "profile_md": profile_md,
        }

    def make_session(authed, session_id, t, bridge) -> WsLocalSession:
        point = hub.get(authed.slug)

        async def reply_sender(channel_id, target_root_id, text):
            await point.client.send_fallback_message(channel_id, text, target_root_id)

        return WsLocalSession(
            slug=authed.slug,
            session_id=session_id,
            transport=t,
            queue=BundleQueue(),
            reporter=point.reporter,
            reply_sender=reply_sender,
            on_acked=bridge.on_acked,
            on_dead=bridge.on_dead,
            now=time.monotonic,
            ack_timeout_s=point.ack_timeout_s,
            ping_interval_s=point.ping_interval_s,
        )

    async def start_consumer(authed, on_message):
        point = hub.get(authed.slug)
        # Attaching is what brings the agent online: run the heartbeat
        # for the lifetime of the consumer.
        hb = asyncio.ensure_future(point.reporter.run_heartbeat_loop())
        try:
            await point.client.listen(on_message)
        finally:
            point.reporter.stop()
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass

    await serve_connection(
        transport,
        authenticate=authenticate_bundle,
        is_servable=hub.is_servable,
        agent_context=agent_context,
        registry=hub.registry,
        make_session=make_session,
        start_consumer=start_consumer,
        new_session_id=lambda: f"wsl_{uuid.uuid4().hex}",
        base64_decode=base64.b64decode,
    )
