"""Handshake authentication by ``.puffoagent`` decryption and identity binding.

The tool restored its identity from a ``.puffoagent`` export and proves
ownership by sending the export blob plus the password the user set at
creation. Decryption proves possession of that export; authorization then
binds its Agent root public key and id to the daemon's registered attach
point. A caller-created bundle with a copied slug is therefore insufficient.

The bundle stream is plaintext and the daemon owns all crypto, so the
keys inside the export are only an authentication artifact here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ...crypto.keystore import decode_secret
from ...crypto.primitives import Ed25519KeyPair
from ..export import ImportPackError, unpack


class AuthError(Exception):
    """Handshake rejected: wrong password, corrupt blob, or a manifest
    that isn't a single-agent export."""


@dataclass(frozen=True)
class AuthedAgent:
    agent_id: str
    slug: str
    display_name: str
    root_public_key: bytes = b""


def authenticate_bundle(blob: bytes, password: str) -> AuthedAgent:
    """Decrypt a ``.puffoagent`` blob and return the agent it identifies,
    or raise ``AuthError``."""
    try:
        bundle = unpack(blob, password)
    except ImportPackError as exc:
        raise AuthError(str(exc)) from exc

    agents = bundle.manifest.get("agents") or []
    if len(agents) != 1:
        raise AuthError("ws-local connect requires a single-agent export")

    entry = agents[0]
    slug = entry.get("slug") or ""
    agent_id = entry.get("id") or ""
    if not slug or not agent_id:
        raise AuthError("export manifest missing slug/id")
    files = bundle.agents.get(agent_id)
    if not isinstance(files, dict):
        raise AuthError("export is missing the declared agent payload")
    identity_path = f"keys/{slug}.json"
    identity_bytes = files.get(identity_path)
    if not isinstance(identity_bytes, bytes):
        raise AuthError("export is missing the declared agent identity")
    try:
        identity = json.loads(identity_bytes.decode("utf-8"))
        if not isinstance(identity, dict):
            raise ValueError("identity must be an object")
        if identity.get("slug") != slug:
            raise ValueError("identity slug mismatch")
        root_secret = decode_secret(identity["root_secret_key"])
        root_public_key = Ed25519KeyPair.from_secret_bytes(
            root_secret
        ).public_key_bytes()
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError(f"export agent identity is invalid: {exc}") from exc
    return AuthedAgent(
        agent_id=agent_id,
        slug=slug,
        display_name=entry.get("display_name", ""),
        root_public_key=root_public_key,
    )
