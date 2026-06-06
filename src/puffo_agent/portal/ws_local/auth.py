"""Handshake authentication by ``.puffoagent`` decryption.

The tool restored its identity from a ``.puffoagent`` export and proves
ownership by sending the export blob plus the password the user set at
creation. The daemon authenticates by decrypting: if ``export.unpack``
succeeds, the connector holds both the blob and the password — that IS
the credential. No subkey signature, nonce, or clock window is involved;
the bundle is the source of truth (the pinned-subkey decision).

The bundle stream is plaintext and the daemon owns all crypto, so the
keys inside the export are only an authentication artifact here.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..export import ImportPackError, unpack


class AuthError(Exception):
    """Handshake rejected: wrong password, corrupt blob, or a manifest
    that isn't a single-agent export."""


@dataclass(frozen=True)
class AuthedAgent:
    agent_id: str
    slug: str
    display_name: str


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
    return AuthedAgent(
        agent_id=agent_id, slug=slug, display_name=entry.get("display_name", "")
    )
