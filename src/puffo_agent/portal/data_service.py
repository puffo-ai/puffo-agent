"""Read-only HTTP service exposing each agent's ``messages.db`` to
its MCP subprocess.

The daemon is the sole SQLite reader/writer; MCP goes through this
service so cli-docker doesn't open the WAL'd DB across a bind-mount
boundary (which fails with "disk I/O error"). Loopback-only, no auth
— same trust boundary as the keystore bind-mount.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from ..agent.message_store import MessageStore
from .state import agent_dir

logger = logging.getLogger(__name__)


@dataclass
class DataServiceConfig:
    enabled: bool = True
    bind_host: str = "127.0.0.1"
    port: int = 63386


@dataclass
class _AppState:
    # One MessageStore per agent_id, opened lazily and held for the
    # daemon's lifetime.
    stores: dict[str, MessageStore] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_STORES_KEY = web.AppKey("puffo_data_state", _AppState)


async def _store_for(app: web.Application, agent_id: str) -> MessageStore | None:
    """Return the MessageStore for ``agent_id``, or None when the db
    doesn't exist (handler returns 404)."""
    state: _AppState = app[_STORES_KEY]
    async with state.lock:
        store = state.stores.get(agent_id)
        if store is not None:
            return store
        db_path = agent_dir(agent_id) / "messages.db"
        if not db_path.exists():
            return None
        store = MessageStore(db_path)
        try:
            await store.open()
        except Exception as exc:
            logger.warning(
                "data-service: open(%s) failed: %s", db_path, exc,
            )
            return None
        state.stores[agent_id] = store
        return store


async def _close_all_stores(app: web.Application) -> None:
    """Flush WAL files deterministically on shutdown."""
    state: _AppState = app[_STORES_KEY]
    for store in state.stores.values():
        try:
            await store.close()
        except Exception:
            logger.exception("data-service: close failed")
    state.stores.clear()


async def lookup_channel_space(request: web.Request) -> web.Response:
    """GET channel→space mapping. 404 when the channel is unseen."""
    agent_id = request.match_info["agent_id"]
    channel_id = request.match_info["channel_id"]
    store = await _store_for(request.app, agent_id)
    if store is None:
        return web.json_response({"error": "agent db not found"}, status=404)
    try:
        space_id = await store.lookup_channel_space(channel_id)
    except Exception as exc:
        logger.exception(
            "data-service: lookup_channel_space failed (agent=%s ch=%s)",
            agent_id, channel_id,
        )
        return web.json_response(
            {"error": f"lookup failed: {exc}"}, status=500,
        )
    if not space_id:
        return web.json_response({"error": "channel unknown"}, status=404)
    return web.json_response({"space_id": space_id})


async def list_recent_messages(request: web.Request) -> web.Response:
    """Recent messages for a channel, oldest first. ``channel`` may
    be ``__all__`` to fetch across every channel."""
    agent_id = request.match_info["agent_id"]
    channel_id = request.query.get("channel", "")
    if not channel_id:
        return web.json_response(
            {"error": "channel query param required"}, status=400,
        )
    try:
        limit = int(request.query.get("limit", "20"))
    except ValueError:
        return web.json_response(
            {"error": "limit must be an integer"}, status=400,
        )
    limit = max(1, min(limit, 200))
    store = await _store_for(request.app, agent_id)
    if store is None:
        return web.json_response({"error": "agent db not found"}, status=404)
    try:
        msgs = await store.get_channel_history(channel_id, limit)
    except Exception as exc:
        logger.exception(
            "data-service: get_channel_history failed (agent=%s ch=%s)",
            agent_id, channel_id,
        )
        return web.json_response(
            {"error": f"history fetch failed: {exc}"}, status=500,
        )
    return web.json_response({
        "messages": [_msg_to_dict(m) for m in msgs],
    })


async def get_message_by_envelope(request: web.Request) -> web.Response:
    """GET a single message by envelope_id. 404 if not stored."""
    agent_id = request.match_info["agent_id"]
    envelope_id = request.match_info["envelope_id"]
    store = await _store_for(request.app, agent_id)
    if store is None:
        return web.json_response({"error": "agent db not found"}, status=404)
    try:
        msg = await store.get_message_by_envelope(envelope_id)
    except Exception as exc:
        logger.exception(
            "data-service: lookup by envelope_id failed (agent=%s env=%s)",
            agent_id, envelope_id,
        )
        return web.json_response(
            {"error": f"lookup failed: {exc}"}, status=500,
        )
    if msg is None:
        return web.json_response({"error": "message not found"}, status=404)
    return web.json_response({"message": _msg_to_dict(msg)})


def _msg_to_dict(m: Any) -> dict[str, Any]:
    return {
        "envelope_id": m.envelope_id,
        "envelope_kind": m.envelope_kind,
        "sender_slug": m.sender_slug,
        "channel_id": m.channel_id,
        "space_id": m.space_id,
        "recipient_slug": m.recipient_slug,
        "content_type": m.content_type,
        "content": m.content,
        "sent_at": m.sent_at,
        "received_at": m.received_at,
        "thread_root_id": m.thread_root_id,
        "reply_to_id": m.reply_to_id,
    }


# ── Lifecycle ────────────────────────────────────────────────────


def build_app(cfg: DataServiceConfig) -> web.Application:
    app = web.Application()
    app[_STORES_KEY] = _AppState()
    app.router.add_get(
        "/v1/data/{agent_id}/channels/{channel_id}/space",
        lookup_channel_space,
    )
    app.router.add_get(
        "/v1/data/{agent_id}/messages/recent",
        list_recent_messages,
    )
    app.router.add_get(
        "/v1/data/{agent_id}/messages/{envelope_id}",
        get_message_by_envelope,
    )
    app.on_shutdown.append(_close_all_stores)
    return app


async def start_data_service(cfg: DataServiceConfig) -> web.AppRunner | None:
    """Start the data service. Returns ``None`` when disabled or the
    socket bind fails."""
    if not cfg.enabled:
        logger.info("data-service: disabled in daemon.yml; not starting")
        return None
    app = build_app(cfg)
    access_logger = logging.getLogger("puffo_agent.portal.data_service.access")
    runner = web.AppRunner(
        app,
        access_log=access_logger,
        access_log_format='%r -> %s (%Tf s)',
    )
    await runner.setup()
    site = web.TCPSite(runner, host=cfg.bind_host, port=cfg.port)
    try:
        await site.start()
    except OSError as exc:
        logger.warning(
            "data-service: failed to bind %s:%d (%s); cli-docker MCP "
            "tools will see disk I/O errors on the bind-mounted DB",
            cfg.bind_host, cfg.port, exc,
        )
        await runner.cleanup()
        return None
    logger.info("data-service: listening on http://%s:%d", cfg.bind_host, cfg.port)
    return runner


async def stop_data_service(runner: web.AppRunner | None) -> None:
    if runner is None:
        return
    try:
        await runner.cleanup()
    except Exception as exc:
        logger.warning("data-service: cleanup failed: %s", exc)
