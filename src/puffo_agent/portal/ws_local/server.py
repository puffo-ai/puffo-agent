"""Loopback HTTP server for the ws-local WebSocket endpoint."""

from __future__ import annotations

import ipaddress
import logging

from aiohttp import hdrs, web

from ..state import WsLocalServiceConfig
from .route import WS_LOCAL_HUB_KEY, WS_LOCAL_PATH, handle_ws_local

logger = logging.getLogger(__name__)


def _is_loopback_bind_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _loopback_host_headers(cfg: WsLocalServiceConfig) -> set[str]:
    hosts = {"127.0.0.1", "localhost", "[::1]"}
    bind_host = cfg.bind_host.lower()
    if ":" in bind_host:
        bind_host = f"[{bind_host}]"
    hosts.add(bind_host)
    return hosts | {f"{host}:{cfg.port}" for host in hosts}


def _require_local_request(cfg: WsLocalServiceConfig):
    allowed_hosts = _loopback_host_headers(cfg)

    @web.middleware
    async def require_local_request(request: web.Request, handler):
        host = (request.headers.get(hdrs.HOST) or "").lower()
        if host not in allowed_hosts:
            return web.Response(status=403, text="invalid host")
        if hdrs.ORIGIN in request.headers:
            return web.Response(status=403, text="invalid origin")
        return await handler(request)

    return require_local_request


def build_app(cfg: WsLocalServiceConfig, ws_local_hub=None) -> web.Application:
    # TODO: Bound concurrent pre-auth handshakes to cap local KDF work.
    app = web.Application(middlewares=[_require_local_request(cfg)])
    app[WS_LOCAL_HUB_KEY] = ws_local_hub
    app.router.add_get(WS_LOCAL_PATH, handle_ws_local)
    return app


async def start_ws_local_server(
    cfg: WsLocalServiceConfig, ws_local_hub=None,
) -> web.AppRunner | None:
    if not cfg.enabled:
        logger.info("ws-local: disabled in daemon.yml; not starting")
        return None
    if not _is_loopback_bind_host(cfg.bind_host):
        # TODO: Expose rejected bind configuration through daemon status.
        logger.warning(
            "ws-local: refusing non-loopback bind host %r; not starting",
            cfg.bind_host,
        )
        return None
    runner = web.AppRunner(
        build_app(cfg, ws_local_hub),
        access_log=logging.getLogger("puffo_agent.portal.ws_local.access"),
        access_log_format="%r -> %s (%Tf s)",
    )
    await runner.setup()
    site = web.TCPSite(runner, host=cfg.bind_host, port=cfg.port)
    try:
        await site.start()
    except OSError as exc:
        logger.warning(
            "ws-local: failed to bind %s:%d (%s); continuing without it",
            cfg.bind_host,
            cfg.port,
            exc,
        )
        await runner.cleanup()
        return None
    logger.info("ws-local: listening on ws://%s:%d%s", cfg.bind_host, cfg.port, WS_LOCAL_PATH)
    return runner


async def stop_ws_local_server(runner: web.AppRunner | None) -> None:
    if runner is None:
        return
    try:
        await runner.cleanup()
    except Exception as exc:
        logger.warning("ws-local: cleanup failed: %s", exc)
