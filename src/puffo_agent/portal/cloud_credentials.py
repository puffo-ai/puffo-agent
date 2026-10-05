"""Credential reads for a sandbox-resident (cloud) agent.

A cloud agent holds no keys: its root secret lives in puffo-server's keystore
and its outbound HTTP is the keyless ``/v2/cloud-agents/*`` surface, where the
E2B egress proxy adds ``x-sandbox-token``. So it cannot derive the KEM keypair
``AgentCredentials`` derives, and it cannot call the signed ``/v2/credentials``
routes at all (puffo-server #434 / #437).

The server therefore hands over the KEY, not the opened credential:

    POST /v2/cloud-agents/credential-kem-secret   → {slug, key_version, kem_secret_key}
    GET  /v2/cloud-agents/credentials?type=T      → {credentials: [{id,type,index,version,state,alias}]}
    GET  /v2/cloud-agents/credentials/{type}/{i}  → {id,type,index,version,blob,expire_at?}

The two reads delegate to the same inner functions the signed handlers use,
so their bodies are ``AgentCredentials``'s bodies. The unwrap stays here, with
the same ``open_credential`` and the same AAD, so the wrap format has one
implementation and no plaintext credential crosses the boundary.

Uses the http client's UNSIGNED methods: ``get``/``post`` are the signed
path and would try to sign with a subkey a cloud agent does not hold
(``worker_run`` branches the same way on ``http.keyless``).

Duck-typed to ``AgentCredentials`` (``get`` / ``held``); nothing isinstance-
checks either. What this holds — the sandbox token (in the http client), the
KEM secret, the opened value — must never reach a tool result, a message or a
log line. ``HeldCredential.value`` is already out of repr.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..crypto.credential_keys import (
    CredentialKeyError,
    compute_credential_wrap_aad,
    credential_id,
    open_credential,
)
from ..crypto.encoding import base64url_decode
from ..crypto.http_client import HttpError
from ..crypto.primitives import KemKeyPair
from .credentials import HeldCredential

logger = logging.getLogger(__name__)

_GONE = (403, 404, 410)
_KEM_ROUTE = "/v2/cloud-agents/credential-kem-secret"
_LIST_ROUTE = "/v2/cloud-agents/credentials"


class CloudAgentCredentials:
    """``AgentCredentials`` for an agent whose keys the server holds."""

    def __init__(self, http: Any, slug: str, owner_slug: str) -> None:
        if not owner_slug:
            raise ValueError("owner_slug is required")  # ids derive from it
        self._http = http
        self._slug = slug
        self._owner = owner_slug
        self._kem: KemKeyPair | None = None
        self._key_version: int | None = None
        self._held: dict[str, HeldCredential] = {}

    async def _ensure_kem(self, *, refresh: bool = False) -> tuple[KemKeyPair, int]:
        """The KEM keypair at the version the server registered, cached.

        POST, as the server insists — the key is a secret and must not sit in
        anything that caches a GET. Refreshed when a wrap refuses to open, in
        case the registered version moved under us.
        """
        if self._kem is not None and self._key_version is not None and not refresh:
            return self._kem, self._key_version
        data = await self._http.post_unsigned(_KEM_ROUTE, {})
        secret = data.get("kem_secret_key") if isinstance(data, dict) else None
        version = data.get("key_version") if isinstance(data, dict) else None
        if not isinstance(secret, str) or not isinstance(version, int) or version < 1:
            raise CredentialKeyError("server answered the KEM request with no usable key")
        self._kem = KemKeyPair.from_secret_bytes(base64url_decode(secret))
        self._key_version = version
        return self._kem, version

    async def get(self, credential_type: str, index: int) -> HeldCredential | None:
        """The current value, from memory or the server. None = not usable now."""
        cached = self._find(credential_type, index)
        if cached is not None and not _expired(cached):
            return cached
        try:
            data = await self._http.get_unsigned(f"{_LIST_ROUTE}/{credential_type}/{index}")
        except HttpError as exc:
            if exc.status in _GONE:
                logger.info("credential %s/%s not available (%s)", credential_type, index, exc.status)
                self._drop(credential_type, index)
                return None
            raise
        try:
            held = await self._open(data, credential_type, index)
        except CredentialKeyError:
            # The one legitimate reason a genuine wrap refuses: our cached key
            # is at an older version than the server's. Re-ask once.
            await self._ensure_kem(refresh=True)
            held = await self._open(data, credential_type, index)
        if _expired(held):
            return None
        self._held[held.id] = held
        return held

    async def held(self, credential_type: str) -> list[tuple[int, str]]:
        """``(index, alias)`` of every ACTIVATED credential of this type the
        server says this agent holds. Asks the server; caches nothing."""
        data = await self._http.get_unsigned(f"{_LIST_ROUTE}?type={credential_type}")
        return sorted(
            (item["index"], item.get("alias") or "")
            for item in data["credentials"]
            if item["type"] == credential_type and item["state"] == "ACTIVATED"
        )

    async def _open(self, data: dict, credential_type: str, index: int) -> HeldCredential:
        """Open as the credential ASKED FOR, or refuse — ids and AAD come from
        the request, not the response, so another row cannot answer for it."""
        kem, key_version = await self._ensure_kem()
        expected_id = credential_id(self._owner, credential_type, index)
        if (data.get("id"), data.get("type"), data.get("index")) != (expected_id, credential_type, index):
            raise CredentialKeyError("server answered with a different credential")
        aad = compute_credential_wrap_aad(
            credential_id=expected_id,
            version=data["version"],
            recipient_slug=self._slug,
            credential_type=credential_type,
            key_version=key_version,
        )
        value = open_credential(kem, aad, base64url_decode(data["blob"]))
        expire_at = data.get("expire_at")
        return HeldCredential(
            id=expected_id,
            type=credential_type,
            index=index,
            version=data["version"],
            expire_at=datetime.fromisoformat(expire_at) if expire_at else None,
            value=value,
        )

    def forget(self, credential_type: str, index: int) -> None:
        """Drop the cached value so the next ``get`` asks the server.

        Used when the owner files a NEWER version: ``get`` serves memory while
        the held value is unexpired, so without this a re-filed credential
        would be read as "the server is behind" forever.
        """
        self._drop(credential_type, index)

    def _find(self, credential_type: str, index: int) -> HeldCredential | None:
        for held in self._held.values():
            if held.type == credential_type and held.index == index:
                return held
        return None

    def _drop(self, credential_type: str, index: int) -> None:
        for key in [k for k, h in self._held.items() if h.type == credential_type and h.index == index]:
            del self._held[key]


def _expired(held: HeldCredential) -> bool:
    return held.expire_at is not None and held.expire_at <= datetime.now(timezone.utc)
