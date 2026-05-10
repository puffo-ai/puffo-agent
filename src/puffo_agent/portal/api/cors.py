"""CORS + Private Network Access middleware.

The bridge runs over plain HTTP on loopback. HTTPS pages reach it
via the PNA preflight (``Access-Control-Allow-Private-Network``),
gated on a strict origin allowlist — missing ``Access-Control-
Allow-Origin`` causes the browser to drop the real request.

DNS-rebinding defence: every real request must arrive with ``Host``
in the loopback allowlist. A page whose DNS was rebound to 127.0.0.1
still sends its original host header, so we reject before any
handler runs.
"""

from __future__ import annotations

from aiohttp import hdrs, web

from ..state import BridgeConfig

_ALLOWED_HEADERS = (
    "x-puffo-version, x-puffo-slug, x-puffo-signer-id, "
    "x-puffo-timestamp, x-puffo-nonce, x-puffo-signature, content-type"
)
_ALLOWED_METHODS = "GET, POST, PATCH, DELETE, OPTIONS"


def _loopback_hosts(port: int) -> set[str]:
    return {
        "127.0.0.1", "localhost",
        f"127.0.0.1:{port}", f"localhost:{port}",
    }


def make_cors_middleware(cfg: BridgeConfig):
    allowed_origins = set(cfg.allowed_origins)
    loopback = _loopback_hosts(cfg.port)

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        # DNS-rebinding mitigation: reject any non-loopback Host
        # before auth runs.
        host = (request.headers.get(hdrs.HOST) or "").lower()
        if host not in loopback:
            return web.Response(status=403, text="invalid host")

        origin = request.headers.get(hdrs.ORIGIN)
        origin_allowed = origin in allowed_origins if origin else False

        if request.method == hdrs.METH_OPTIONS:
            # CORS + PNA preflight. Echo the origin only if allowed;
            # missing Allow-Origin causes the browser to drop the
            # subsequent real request.
            headers = {
                "Access-Control-Allow-Methods": _ALLOWED_METHODS,
                "Access-Control-Allow-Headers": _ALLOWED_HEADERS,
                "Access-Control-Max-Age": "600",
                "Vary": "Origin, Access-Control-Request-Headers",
            }
            if origin_allowed:
                headers["Access-Control-Allow-Origin"] = origin
                # Required when crossing from a public-network page
                # to a private-network destination.
                headers["Access-Control-Allow-Private-Network"] = "true"
            return web.Response(status=204, headers=headers)

        # Catch HTTPException so 4xx/5xx responses still carry CORS
        # headers — without this the browser blocks the page from
        # reading the body and reports a misleading "No Allow-Origin"
        # error instead of the actual server message.
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        if origin_allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
        # Vary unconditionally so caches don't poison cross-origin
        # responses with a previous origin's headers.
        existing_vary = response.headers.get("Vary")
        response.headers["Vary"] = (
            f"{existing_vary}, Origin" if existing_vary else "Origin"
        )
        return response

    return cors_middleware
