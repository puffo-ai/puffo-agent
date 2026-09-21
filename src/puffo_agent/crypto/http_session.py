from __future__ import annotations

import os
import ssl
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

import aiohttp
import certifi
from aiohttp_socks import ProxyConnector


_SOCKS_SCHEMES = {"socks4", "socks4a", "socks5", "socks5h"}


def _env_proxy_for_url(url: str) -> str | None:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if host and proxy_bypass(host):
        return None
    proxies = {k.lower(): v for k, v in getproxies().items()}
    return proxies.get(scheme) or proxies.get("all") or proxies.get("socks")


def _is_socks_proxy(proxy_url: str) -> bool:
    return urlsplit(proxy_url).scheme.lower() in _SOCKS_SCHEMES


def trust_store_fingerprint() -> tuple[tuple[str, int, int], ...]:
    """Identity of the trust store a fresh context would load, cheaply.

    ``(path, mtime_ns, size)`` for the system CA bundle
    (``ssl.get_default_verify_paths()``) and certifi's. A sandbox that is
    resumed on a different E2B node gets that node's proxy CA written into
    the system bundle (PUF-377); the file's mtime moves, and that is the
    signal a cached session must not outlive. Missing files fingerprint as
    absent rather than raising — the store is read on every request.
    """
    paths = ssl.get_default_verify_paths()
    out = []
    for path in dict.fromkeys(
        p for p in (paths.cafile, paths.openssl_cafile, certifi.where()) if p
    ):
        try:
            st = os.stat(path)
            out.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((path, -1, -1))
    return tuple(out)


def create_remote_ssl_context() -> ssl.SSLContext:
    """Fresh trust store shared by remote HTTP and WebSocket transports."""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.load_verify_locations(cafile=certifi.where())
    return ssl_ctx


def create_remote_http_session(
    base_url: str,
    *,
    timeout: aiohttp.ClientTimeout | None = None,
) -> aiohttp.ClientSession:
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout

    # Fresh TLS trust per session (PUF-192). aiohttp caches its default verified
    # SSLContext at import (a process-wide singleton). E2B's egress proxy
    # TLS-intercepts the relay and (re)generates its per-sandbox proxy CA on a
    # cold restore, so a reused cached context can't verify the intercepted cert
    # and every request to the relay fails until the process restarts. Building
    # the context here means a session recreated after a CA change picks the new
    # CA up — the same fix bridge_client applies at the WS.
    ssl_ctx = create_remote_ssl_context()

    proxy_url = _env_proxy_for_url(base_url)
    if proxy_url and _is_socks_proxy(proxy_url):
        connector = ProxyConnector.from_url(proxy_url, ssl=ssl_ctx)
        return aiohttp.ClientSession(connector=connector, trust_env=False, **kwargs)

    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    return aiohttp.ClientSession(connector=connector, trust_env=True, **kwargs)
