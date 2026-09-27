"""One agent's view of its credentials under design v2 (phase 1, read side).

The server holds nothing usable on its own, and this daemon holds nothing on
disk: every value lives in this process only, keyed by credential id, and a
restart simply fetches again (amendment 9). What makes that safe is the
order of three rules:

- **Absence from the list is revocation.** ``GET /v2/credentials`` lists what
  this agent currently holds; anything cached and not listed goes. A list
  call that failed deletes nothing (amendment 4) — a timeout is not a revoke.
- **A result may not outlive a revocation it raced.** Revoking does not bump
  the value version, so a GET that left before a revoke and lands after it
  carries a perfectly current-looking version. Every invalidation bumps a
  generation counter; a fetch that started under an older generation is
  thrown away rather than cached (amendment 10, Jeff 226108).
- **Anything that does not open is refused, not retried as something else.**
  The AAD binds id, version, this slug, type and key version, so a blob that
  was moved, or a version the server misreports, fails to open and the fetch
  fails with it.

Refresh is not here yet: its server contract lands with the server's step 5.
"""

from __future__ import annotations

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
from ..crypto.encoding import base64url_decode
from ..crypto.http_client import HttpError
from ..crypto.primitives import Ed25519KeyPair

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
    # The secret itself, or the S2 share for a split type; which one is a
    # function of ``type`` and is the caller's business. Kept out of repr so a
    # log line or traceback that prints one does not print the secret.
    value: bytes = field(repr=False)


class AgentCredentials:
    def __init__(self, http: Any, slug: str, owner_slug: str, root_secret: bytes) -> None:
        if not owner_slug:
            # Without it the id a response must carry cannot be derived, and
            # then nothing stops a response for one credential being served as
            # another.
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

        Re-deriving and re-registering on each start is what lets the daemon
        keep no state for it: the key is a function of the root.
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
                self._invalidate(lambda c: c.type == credential_type and c.index == index)
                return None
            raise
        held = self._open(data, credential_type, index)
        if generation != self._generation:
            return None
        self._held[held.id] = held
        return held

    async def reconcile(self) -> None:
        """Drop everything the server no longer lists as held and current.

        Only deletes. A stale list can at worst drop something that is then
        fetched again; it can never bring a revoked value back.
        """
        data = await self._http.get("/v2/credentials")
        current = {
            item["id"]: (item["version"], item["state"])
            for item in data["credentials"]
        }
        self._invalidate(lambda c: current.get(c.id) != (c.version, "ACTIVATED"))

    def _open(self, data: dict, credential_type: str, index: int) -> HeldCredential:
        """Open the response as the credential that was asked for, or refuse.

        The AAD stops a blob moving between rows or recipients, but it is built
        from whatever id the response names. A server that answered a request
        for A with B's genuine row would pass it, and B's secret would come back
        as A's (Boris 226286). So the id is derived here and the response must
        match it; the AAD is then built from what was asked, not what came back.
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

        The fence goes up even when nothing was cached: the fetch being
        fenced may be the first one for a credential that was just revoked.
        """
        for key in [key for key, held in self._held.items() if drop(held)]:
            del self._held[key]
        self._generation += 1


def _expired(held: HeldCredential) -> bool:
    return held.expire_at is not None and held.expire_at <= datetime.now(timezone.utc)
