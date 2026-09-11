"""Small support types and constants used by the Puffo message client."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..crypto.encoding import base64url_decode
from ..crypto.http_client import PuffoCoreHttpClient

DM_GATE_SENDER_ACK = (
    "Thanks — your message has reached me. I've asked my operator to "
    "approve our conversation, and I'll reply as soon as they do."
)

# Stored in place of the agent's own foreign-DM approval prompt when the
# server echoes it back. The prompt quotes the held stranger's message for
# the operator's benefit; storing that echo verbatim would put the withheld
# body into the model's prior context, defeating the gate that withheld it.
DM_GATE_PROMPT_PLACEHOLDER = (
    "[foreign-DM approval prompt — held message withheld from context]"
)

# Stored in place of the agent's own keyless invitation prompt when the
# server echoes it back. The echo is an operator-control artifact, not
# model context; storing the operator-visible prompt verbatim would let the
# agent "hear itself" ask to accept an invite it is waiting to decide.
INVITE_PROMPT_PLACEHOLDER = "[invitation prompt — operator decision pending]"


class AgentLogger(logging.LoggerAdapter):
    """Prefix daemon logs with the active agent slug."""

    def process(self, msg: Any, kwargs: Any) -> tuple[str, Any]:
        return f"[{self.extra['agent']}] {msg}", kwargs


def parse_operator_pubkey(identity_cert_json: str | None) -> bytes | None:
    """Extract the declared 32-byte operator root key from an agent cert."""
    if not identity_cert_json:
        return None
    try:
        cert = json.loads(identity_cert_json)
    except Exception:
        return None
    encoded_key = cert.get("declared_operator_public_key")
    if not isinstance(encoded_key, str) or not encoded_key:
        return None
    try:
        public_key = base64url_decode(encoded_key)
    except Exception:
        return None
    return public_key if len(public_key) == 32 else None


class DeviceKeyCache:
    """Cache every known signing subkey for an inbound sender."""

    def __init__(self, http_client: PuffoCoreHttpClient):
        self._http = http_client
        self._cache: dict[str, list[bytes]] = {}

    async def get_signing_keys(self, slug: str) -> list[bytes]:
        if slug in self._cache:
            return self._cache[slug]
        public_keys: list[bytes] = []
        since = 0
        while True:
            data = await self._http.get(f"/certs/sync?slugs={slug}&since={since}")
            for entry in data.get("entries", []):
                key = _entry_signing_key(entry)
                if key is not None:
                    public_keys.append(key)
                since = entry.get("seq", since)
            if not data.get("has_more"):
                break
        if not public_keys:
            raise ValueError(f"no subkey_cert entries for {slug}")
        self._cache[slug] = public_keys
        return public_keys

    def invalidate(self, slug: str) -> None:
        self._cache.pop(slug, None)


def _entry_signing_key(entry: dict[str, Any]) -> bytes | None:
    if entry.get("kind") != "subkey_cert":
        return None
    encoded_key = (entry.get("cert") or {}).get("subkey_public_key", "")
    if not encoded_key:
        return None
    try:
        return base64url_decode(encoded_key)
    except Exception:
        return None
