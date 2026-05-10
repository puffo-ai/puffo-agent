"""Auth middleware for the bridge.

Uses the ``x-puffo-*`` request-signing scheme but with two
simplifications: no subkey rotation (device root key signs directly)
and single pairing (one ``(slug, device_id)`` at a time). ``GET
/v1/info`` is unauthenticated and ``POST /v1/pair`` does its own
cert verification; everything else flows through here. Replay
protection is a per-process nonce cache scoped to the 5-minute
timestamp-skew window.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict

from aiohttp import web

from ...crypto.encoding import base64url_decode
from ...crypto.http_auth import VerifyError, is_timestamp_fresh, verify_request
from .pairing import load_pairing

logger = logging.getLogger(__name__)


# /v1/info is public discovery; /v1/pair handles its own crypto.
PUBLIC_PATHS = {"/v1/info"}
PAIR_PATH = "/v1/pair"


class _NonceCache:
    """LRU-ish nonce cache. Keys are ``(signer_id, nonce)``. TTL
    matches the timestamp-freshness window — anything older would
    already have been rejected by the timestamp check."""

    def __init__(self, ttl_ms: int = 5 * 60 * 1000, max_entries: int = 10_000):
        self._ttl_ms = ttl_ms
        self._max = max_entries
        self._seen: OrderedDict[tuple[str, str], int] = OrderedDict()

    def check_and_record(self, signer_id: str, nonce: str) -> bool:
        """True if fresh (and records it); False if already seen
        within the TTL window."""
        now = int(time.time() * 1000)
        self._evict(now)
        key = (signer_id, nonce)
        if key in self._seen:
            return False
        self._seen[key] = now
        if len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return True

    def _evict(self, now_ms: int) -> None:
        cutoff = now_ms - self._ttl_ms
        while self._seen:
            oldest_key, oldest_ts = next(iter(self._seen.items()))
            if oldest_ts >= cutoff:
                return
            self._seen.popitem(last=False)


def make_auth_middleware():
    nonces = _NonceCache()

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        path = request.path
        if path in PUBLIC_PATHS:
            return await handler(request)

        # The pair handler does full cert verification itself; gate
        # only on a fresh timestamp here so a pair request can't be
        # captured and replayed later.
        if path == PAIR_PATH and request.method == "POST":
            ts = request.headers.get("x-puffo-timestamp", "")
            if not is_timestamp_fresh(ts):
                return _unauth("stale or missing x-puffo-timestamp")
            return await handler(request)

        pairing = load_pairing()
        if pairing is None:
            return _unauth("not paired")

        slug = request.headers.get("x-puffo-slug", "")
        signer_id = request.headers.get("x-puffo-signer-id", "")
        ts = request.headers.get("x-puffo-timestamp", "")
        nonce = request.headers.get("x-puffo-nonce", "")
        sig = request.headers.get("x-puffo-signature", "")
        if not (slug and signer_id and ts and nonce and sig):
            return _unauth("missing x-puffo-* headers")

        # signer_id is the device_id (no subkey indirection). The
        # slug + device_id must match the active pairing — otherwise
        # a different identity is trying to drive this daemon.
        if slug != pairing.slug or signer_id != pairing.device_id:
            return _unauth("not the paired identity")

        if not is_timestamp_fresh(ts):
            return _unauth("stale or missing timestamp")
        if not nonces.check_and_record(signer_id, nonce):
            return _unauth("nonce already used")

        try:
            device_pk = base64url_decode(pairing.device_signing_public_key)
        except Exception:
            return _unauth("paired device pubkey corrupt")

        body = await request.read()
        try:
            verify_request(
                public_key=device_pk,
                method=request.method,
                # path_qs binds the query string into the signature so
                # a captured GET on ``/v1/agents/x/files?path=safe``
                # can't be replayed as ``?path=../../etc``.
                path=request.path_qs,
                timestamp=ts,
                nonce=nonce,
                body=body,
                signature_b64=sig,
            )
        except VerifyError as exc:
            return _unauth(str(exc))

        # Stash for handlers: root pubkey decides ownership, slug
        # populates ``owned`` flags on responses.
        request["paired_root_pubkey"] = pairing.root_public_key
        request["paired_slug"] = pairing.slug
        return await handler(request)

    return auth_middleware


def _unauth(reason: str) -> web.Response:
    logger.warning("bridge: 401 %s", reason)
    return web.json_response({"error": reason}, status=401)
