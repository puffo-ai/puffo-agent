"""One agent's credentials under design v2 (read side).

In-process only; a restart refetches (amendment 9). Revocation is absence
from the list, but a *failed* list drops nothing (amendment 4). Every
invalidation bumps a generation to fence fetches that raced it (amendment 10,
Jeff 226108). Anything that does not open is refused. Refresh (§5.5) spends
this agent's S2 share.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..crypto.credential_keys import (
    CredentialKeyError,
    compute_credential_wrap_aad,
    credential_id,
    create_credential_key_cert,
    derive_credential_kem_keypair,
    open_credential,
)
from ..crypto.encoding import base64url_decode, base64url_encode
from ..crypto.http_client import HttpError
from ..crypto.primitives import Ed25519KeyPair

logger = logging.getLogger(__name__)

KEY_VERSION = 1

# 403/404: not held. 410: INACTIVATED. All three: stop using it.
_GONE = (403, 404, 410)


@dataclass(frozen=True)
class HeldCredential:
    id: str
    type: str
    index: int
    version: int
    expire_at: datetime | None
    # The secret, or the S2 share for a split type. Out of repr.
    value: bytes = field(repr=False)


class AgentCredentials:
    def __init__(self, http: Any, slug: str, owner_slug: str, root_secret: bytes) -> None:
        if not owner_slug:
            raise ValueError("owner_slug is required")  # see _open
        self._http = http
        self._slug = slug
        self._owner = owner_slug
        self._root = Ed25519KeyPair.from_secret_bytes(root_secret)
        self._kem = derive_credential_kem_keypair(root_secret, KEY_VERSION)
        self._held: dict[str, HeldCredential] = {}
        self._generation = 0

    async def register(self) -> None:
        """Publish this agent's credential key. Idempotent; run every start."""
        cert = create_credential_key_cert(
            self._root, self._slug, self._kem.public_key_bytes(), KEY_VERSION,
            int(time.time() * 1000),
        )
        await self._http.put("/v2/identities/me/credential-key", {"cert_json": cert})

    async def get(self, credential_type: str, index: int) -> HeldCredential | None:
        """The current value, from memory or the server.

        ``None`` is "not usable now"; asking again is always safe.
        """
        cached = self._find(credential_type, index)
        if cached is not None and not _expired(cached):
            return cached

        generation = self._generation
        try:
            data = await self._http.get(f"/v2/credentials/{credential_type}/{index}")
        except HttpError as exc:
            if exc.status in _GONE:
                # A revoke / INACTIVATE / lapsed attestation is otherwise silent.
                logger.info(
                    "credential %s/%s not available (%s)", credential_type, index, exc.status
                )
                self._invalidate(lambda c: c.type == credential_type and c.index == index)
                return None
            raise
        held = self._open(data, credential_type, index)
        if generation != self._generation or _expired(held):
            return None
        # Fetches can land out of order: keep the newer version, but not if
        # it has since expired. Only _invalidate lowers what is held.
        current = self._held.get(held.id)
        if current is not None and current.version > held.version:
            return None if _expired(current) else current
        self._held[held.id] = held
        return held

    async def reconcile(self) -> None:
        """Drop everything the server no longer lists as held and current.

        Deletes only: a stale list costs a refetch, never a resurrection.
        """
        data = await self._http.get("/v2/credentials")
        current = {
            item["id"]: (item["version"], item["state"])
            for item in data["credentials"]
        }
        self._invalidate(lambda c: current.get(c.id) != (c.version, "ACTIVATED"))

    async def refresh(self, rt_type: str, index: int) -> HeldCredential | None:
        """A fresh access token for the OAuth credential at ``rt_type/index``.

        Not cached: it arrives in plaintext, not as a wrap bound to this agent.
        """
        at_type = _access_token_type(rt_type)
        rt = await self.get(rt_type, index)
        if rt is None:
            return None
        try:
            data = await self._http.post(
                f"/v2/credentials/{rt_type}/{index}/refresh",
                {"version": rt.version, "share": base64url_encode(rt.value)},
            )
        except HttpError as exc:
            if exc.status in _GONE:
                self._invalidate(lambda c: c.index == index and c.type in (rt_type, at_type))
                return None
            if exc.status == 409:
                # Another holder rotated first; drop so the next call refetches.
                self._invalidate(lambda c: c.id == rt.id)
            raise
        # Refresh bumps the AT version; a rotation bumps the RT's too.
        self._invalidate(
            lambda c: c.index == index
            and (c.type == at_type or (data["rotated"] and c.type == rt_type))
        )
        expire_at = data.get("expires_at")
        return HeldCredential(
            id=credential_id(self._owner, at_type, index),
            type=at_type,
            index=index,
            version=data["at_version"],
            expire_at=datetime.fromisoformat(expire_at) if expire_at else None,
            value=data["access_token"].encode(),
        )

    def _open(self, data: dict, credential_type: str, index: int) -> HeldCredential:
        """Open the response as the credential that was asked for, or refuse.

        The id is derived here and the AAD built from the request, not the
        response, so B's genuine row cannot answer for A (Boris 226286).
        """
        expected_id = credential_id(self._owner, credential_type, index)
        if (data["id"], data["type"], data["index"]) != (expected_id, credential_type, index):
            raise CredentialKeyError("server answered with a different credential")
        aad = compute_credential_wrap_aad(
            credential_id=expected_id,
            version=data["version"],
            recipient_slug=self._slug,
            credential_type=credential_type,
            key_version=KEY_VERSION,
        )
        value = open_credential(self._kem, aad, base64url_decode(data["blob"]))
        expire_at = data.get("expire_at")
        return HeldCredential(
            id=expected_id,
            type=credential_type,
            index=index,
            version=data["version"],
            expire_at=datetime.fromisoformat(expire_at) if expire_at else None,
            value=value,
        )

    def _find(self, credential_type: str, index: int) -> HeldCredential | None:
        for held in self._held.values():
            if held.type == credential_type and held.index == index:
                return held
        return None

    def _invalidate(self, drop) -> None:
        """Drop matching entries and fence off every fetch already in flight.

        The fence goes up even when nothing was cached.
        """
        for key in [key for key, held in self._held.items() if drop(held)]:
            del self._held[key]
        self._generation += 1


async def keep_registering(credentials: AgentCredentials, *, sleep=asyncio.sleep) -> bool:
    """Register this agent's key, retrying until the server has it.

    An unpublished key cannot be shared with, so this may not give up until a
    restart: transient failures back off, a 404 (no v2 endpoint) polls hourly
    so a running agent picks v2 up on deploy, any other 4xx rejects the cert.
    Returns whether it registered.
    """
    delay = 5.0
    waiting_for_v2 = False
    while True:
        try:
            await credentials.register()
            return True
        except HttpError as exc:
            if exc.status == 404:
                if not waiting_for_v2:
                    logger.info("credential key: server has no v2 endpoint yet; checking hourly")
                    waiting_for_v2 = True
                await sleep(3600.0)
                continue
            if 400 <= exc.status < 500 and exc.status not in (408, 429):
                logger.warning("credential key rejected (%s); not retrying", exc.status)
                return False
            logger.warning("credential key registration failed (%s); retrying", exc.status)
        except Exception as exc:  # noqa: BLE001 - a background task must not die of it
            logger.warning("credential key registration failed: %s; retrying", exc)
        await sleep(delay)
        delay = min(delay * 4, 300.0)


def _access_token_type(rt_type: str) -> str:
    """``PUFFO_<P>_OAUTH_v1`` -> ``PUFFO_<P>_OAUTH_AT_v1`` (server ``types.rs``)."""
    if not rt_type.endswith("_OAUTH_v1"):
        raise ValueError(f"{rt_type} is not a refreshable credential type")
    return rt_type.removesuffix("_OAUTH_v1") + "_OAUTH_AT_v1"


def _expired(held: HeldCredential) -> bool:
    return held.expire_at is not None and held.expire_at <= datetime.now(timezone.utc)
