"""Derive an agent's owner from its on-disk identity material.

Each agent's identity_cert carries an ``OperatorAttestation``
binding its identity_cert to the operator's root public key. The
bridge reads ``identity_cert.declared_operator_public_key`` and
matches against the paired user's root pubkey to decide ownership.

The cert binding is the source of truth — agent.yml deliberately
stores no ``owner_slug`` field that could be forged.
"""

from __future__ import annotations

import json
from typing import Optional

from ...crypto.keystore import KeyStore
from ..state import AgentConfig


def agent_owner_root_pubkey(agent_id: str) -> Optional[str]:
    """Return the base64url operator root pubkey baked into this
    agent's identity_cert, or ``None`` when there is none — in which
    case the bridge surfaces the agent read-only to every paired
    user.
    """
    try:
        cfg = AgentConfig.load(agent_id)
    except Exception:
        return None
    slug = cfg.puffo_core.slug
    if not slug:
        return None
    try:
        identity = KeyStore.for_agent(agent_id).load_identity(slug)
    except (FileNotFoundError, OSError):
        return None
    raw = identity.identity_cert_json
    if not raw:
        return None
    try:
        cert = json.loads(raw)
    except (TypeError, ValueError):
        return None
    op_pk = cert.get("declared_operator_public_key") if isinstance(cert, dict) else None
    return op_pk if isinstance(op_pk, str) and op_pk else None


def is_owner(agent_id: str, paired_root_pubkey: str) -> bool:
    """True when this agent's declared operator pubkey matches the
    paired user's root pubkey. Drives secret redaction in handlers."""
    owner_pk = agent_owner_root_pubkey(agent_id)
    return owner_pk is not None and owner_pk == paired_root_pubkey
