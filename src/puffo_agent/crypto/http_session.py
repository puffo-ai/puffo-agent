from __future__ import annotations

import ssl
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

import aiohttp
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
    ssl_ctx = ssl.create_default_context()

    proxy_url = _env_proxy_for_url(base_url)
    if proxy_url and _is_socks_proxy(proxy_url):
        connector = ProxyConnector.from_url(proxy_url, ssl=ssl_ctx)
        return aiohttp.ClientSession(connector=connector, trust_env=False, **kwargs)

    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    return aiohttp.ClientSession(connector=connector, trust_env=True, **kwargs)
