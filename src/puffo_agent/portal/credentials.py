"""One agent's view of its credentials under design v2 (phase 1, read side).

Values live in this process only; a restart fetches again (amendment 9).
Three rules keep that safe:

- **Absence from the list is revocation**, but a list call that *failed*
  deletes nothing (amendment 4): a timeout is not a revoke.
- **A result may not outlive a revocation it raced.** Revoking does not bump
  the version, so a response that crossed one still looks current. Every
  invalidation bumps a generation counter and fences those fetches off
  (amendment 10, Jeff 226108).
- **Anything that does not open is refused**, never retried as something
  else: the AAD binds id, version, slug, type and key version.

Refresh (§5.5) trades this agent's S2 share for a fresh access token; the
daemon only has to forget whatever the server's version bumps made stale.
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

# 403/404: not held (the server refuses to say which). 410: INACTIVATED.
# All three mean this agent must not keep using what it has.
_GONE = (403, 404, 410)


@dataclass(frozen=True)
class HeldCredential:
    id: str
    type: str
    index: int
    version: int
    expire_at: datetime | None
    # The secret, or the S2 share for a split type (``type`` says which).
    # Out of repr so a traceback cannot print it.
    value: bytes = field(repr=False)


class AgentCredentials:
    def __init__(self, http: Any, slug: str, owner_slug: str, root_secret: bytes) -> None:
        if not owner_slug:
            # Without it the expected id cannot be derived, and then nothing
            # stops one credential being served as another.
            raise ValueError("owner_slug is required")
        self._http = http
        self._slug = slug
        self._owner = owner_slug
        self._root = Ed25519KeyPair.from_secret_bytes(root_secret)
        self._kem = derive_credential_kem_keypair(root_secret, KEY_VERSION)
        self._held: dict[str, HeldCredential] = {}
        self._generation = 0

    async def register(self) -> None:
        """Publish this agent's credential key. Idempotent; run every start.

        The key is a function of the root, so nothing is stored for it.
        """
        cert = create_credential_key_cert(
            self._root, self._slug, self._kem.public_key_bytes(), KEY_VERSION,
            int(time.time() * 1000),
        )
        await self._http.put("/v2/identities/me/credential-key", {"cert_json": cert})

    async def get(self, credential_type: str, index: int) -> HeldCredential | None:
        """The current value, from memory if held, else from the server.

        ``None`` means "not usable now": not held, INACTIVATED, or revoked
        while this fetch was in flight. Asking again is always safe.
        """
        cached = self._find(credential_type, index)
        if cached is not None and not _expired(cached):
            return cached

        generation = self._generation
        try:
            data = await self._http.get(f"/v2/credentials/{credential_type}/{index}")
        except HttpError as exc:
            if exc.status in _GONE:
                # A revoke, an INACTIVATE and a lapsed operator attestation
                # are otherwise indistinguishable from nothing happening.
                logger.info(
                    "credential %s/%s not available (%s)", credential_type, index, exc.status
                )
                self._invalidate(lambda c: c.type == credential_type and c.index == index)
                return None
            raise
        held = self._open(data, credential_type, index)
        if generation != self._generation or _expired(held):
            return None
        # Overlapping fetches can land out of order, and only a revoke lowers
        # what is held (via _invalidate, not here). Keeping the newer one still
        # means handing back nothing once it has expired.
        current = self._held.get(held.id)
        if current is not None and current.version > held.version:
            return None if _expired(current) else current
        self._held[held.id] = held
        return held

    async def reconcile(self) -> None:
        """Drop everything the server no longer lists as held and current.

        Deletes only, so a stale list can cost a re-fetch but never resurrect
        a revoked value.
        """
        data = await self._http.get("/v2/credentials")
        current = {
            item["id"]: (item["version"], item["state"])
            for item in data["credentials"]
        }
        self._invalidate(lambda c: current.get(c.id) != (c.version, "ACTIVATED"))

    async def refresh(self, rt_type: str, index: int) -> HeldCredential | None:
        """A fresh access token for the OAuth credential at ``rt_type/index``.

        ``None`` means the same as for ``get``. The token is not cached: it
        arrived in plaintext, not as a wrap the AAD ties to this agent.
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
                # Most often another holder rotated first and this share is
                # dead. Dropping it makes the next call fetch the current one.
                self._invalidate(lambda c: c.id == rt.id)
            raise
        # Every refresh bumps the access token's version; a rotation bumps
        # the refresh token's too, and the old share will never work again.
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

        Answering a request for A with B's genuine row would otherwise pass,
        and B's secret would come back as A's (Boris 226286). So the id is
        derived here and the AAD built from the request, not the response.
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

        The fence goes up even when nothing was cached: the fetch being fenced
        may be the first one for a credential that was just revoked.
        """
        for key in [key for key, held in self._held.items() if drop(held)]:
            del self._held[key]
        self._generation += 1


async def keep_registering(credentials: AgentCredentials, *, sleep=asyncio.sleep) -> bool:
    """Register this agent's key, retrying until the server has it.

    An owner can only share with an agent whose key is published, so nothing
    may leave it unpublished until a restart: transient failures back off, and
    a 404 (no v2 endpoint yet) is polled hourly so a running agent picks v2 up
    when it deploys. Any other 4xx rejects the cert itself, which retrying will
    not change. Returns whether it was registered.
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
