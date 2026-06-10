from __future__ import annotations

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

    proxy_url = _env_proxy_for_url(base_url)
    if proxy_url and _is_socks_proxy(proxy_url):
        connector = ProxyConnector.from_url(proxy_url)
        return aiohttp.ClientSession(connector=connector, trust_env=False, **kwargs)

    return aiohttp.ClientSession(trust_env=True, **kwargs)
