"""Resolve an agent's operator from its signed identity certificate."""

from __future__ import annotations

import json

from ...crypto.keystore import KeyStore
from ..state import AgentConfig


def agent_owner_root_pubkey(agent_id: str) -> str | None:
    try:
        cfg = AgentConfig.load(agent_id)
    except Exception:
        return None
    if not cfg.puffo_core.slug:
        return None
    try:
        identity = KeyStore.for_agent(agent_id).load_identity(cfg.puffo_core.slug)
    except (FileNotFoundError, OSError):
        return None
    try:
        cert = json.loads(identity.identity_cert_json)
    except (TypeError, ValueError):
        return None
    operator_key = cert.get("declared_operator_public_key") if isinstance(cert, dict) else None
    return operator_key if isinstance(operator_key, str) and operator_key else None


def is_owner(agent_id: str, operator_root_pubkey: str) -> bool:
    return agent_owner_root_pubkey(agent_id) == operator_root_pubkey
