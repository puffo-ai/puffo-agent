"""aiohttp app + lifecycle helpers for the bridge.

Runs in the daemon's event loop alongside the reconcile loop and
is bound to ``cfg.bind_host`` (always loopback in practice) so the
API is never exposed on the LAN.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..state import BridgeConfig
from .auth import make_auth_middleware
from .cors import make_cors_middleware
from . import handlers as h

logger = logging.getLogger(__name__)


# aiohttp's default 1 MiB cap is below ``MAX_AVATAR_BYTES`` (4 MiB
# raw → ~5.4 MiB after base64 + JSON envelope). 8 MiB covers the
# avatar with headroom for identity-bundle bodies. Per-handler caps
# still apply on top.
BRIDGE_MAX_REQUEST_BYTES = 8 * 1024 * 1024


def build_app(cfg: BridgeConfig) -> web.Application:
    app = web.Application(
        middlewares=[
            make_cors_middleware(cfg),
            make_auth_middleware(),
        ],
        client_max_size=BRIDGE_MAX_REQUEST_BYTES,
    )
    app.router.add_get("/v1/info", h.info)
    app.router.add_post("/v1/pair", h.pair)
    app.router.add_delete("/v1/pairing", h.disconnect)
    app.router.add_get("/v1/agents", h.list_agents)
    app.router.add_post("/v1/agents", h.create_agent)
    app.router.add_get("/v1/agents/{id}", h.get_agent)
    app.router.add_delete("/v1/agents/{id}", h.delete_agent)
    app.router.add_get("/v1/agents/{id}/runtime", h.get_runtime_state)
    app.router.add_patch("/v1/agents/{id}/runtime", h.update_runtime)
    app.router.add_patch("/v1/agents/{id}/profile", h.update_profile)
    app.router.add_post("/v1/agents/{id}/restart", h.restart_agent)
    app.router.add_post("/v1/agents/{id}/pause", h.pause_agent)
    app.router.add_post("/v1/agents/{id}/resume", h.resume_agent)
    app.router.add_post("/v1/agents/{id}/archive", h.archive_agent)
    app.router.add_get("/v1/agents/{id}/log", h.get_log)
    app.router.add_get("/v1/agents/{id}/files", h.list_files)
    app.router.add_get("/v1/agents/{id}/files/raw", h.read_file)
    app.router.add_get("/v1/agents/{id}/claude-md", h.get_claude_md)
    return app


async def start_api_server(cfg: BridgeConfig) -> web.AppRunner | None:
    """Start the bridge HTTP server. Returns the runner for cleanup,
    or None when disabled in daemon.yml."""
    if not cfg.enabled:
        logger.info("bridge: disabled in daemon.yml; not starting")
        return None
    app = build_app(cfg)
    # Route aiohttp access log through the daemon logger.
    access_logger = logging.getLogger("puffo_agent.portal.api.access")
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
        # Port-in-use is the common failure. The bridge isn't
        # load-bearing for chat replies, so keep the daemon alive.
        logger.warning(
            "bridge: failed to bind %s:%d (%s); continuing without it",
            cfg.bind_host, cfg.port, exc,
        )
        await runner.cleanup()
        return None
    logger.info(
        "bridge: listening on http://%s:%d (allowed_origins=%s)",
        cfg.bind_host, cfg.port, cfg.allowed_origins,
    )
    return runner


async def stop_api_server(runner: web.AppRunner | None) -> None:
    if runner is None:
        return
    try:
        await runner.cleanup()
    except Exception as exc:
        logger.warning("bridge: cleanup failed: %s", exc)
