"""Daemon-side agent identity generation for self-service ws-local create.

The daemon mints the agent's own keys + self-signed certs (identity / device /
slug_binding). The operator signs only the OperatorAttestation; registration
runs server-side (POST /agents → pending_token, then POST /certs/slug_binding).
Cert wire shapes match portal/api/certs.py (the daemon's own verifier) and the
puffo-server `core-v2/crates/types/src/cert.rs` producer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ...crypto.canonical import canonicalize_for_signing
from ...crypto.certs import derive_public_key_id
from ...crypto.encoding import base64url_encode
from ...crypto.primitives import Ed25519KeyPair, KemKeyPair

_CERT_VERSION = 1


def _now_ms() -> int:
    return int(time.time() * 1000)


def _self_sign(cert: dict, root: Ed25519KeyPair, field: str) -> dict:
    """Fill ``cert[field]`` with the agent root's signature over the
    canonical (signature-stripped) cert."""
    cert[field] = ""
    sig = root.sign(canonicalize_for_signing(cert))
    cert[field] = base64url_encode(sig)
    return cert


@dataclass
class AgentIdentity:
    agent_id: str
    device_id: str
    root_keypair: Ed25519KeyPair
    device_signing_keypair: Ed25519KeyPair
    device_kem_keypair: KemKeyPair
    identity_cert: dict
    device_cert: dict

    @property
    def root_public_key(self) -> str:
        return base64url_encode(self.root_keypair.public_key_bytes())


def gen_agent_identity(operator_root_pubkey: str) -> AgentIdentity:
    """Mint a fresh agent identity declaring ``operator_root_pubkey`` as its
    operator. Produces the identity_cert + device_cert (both agent-root signed);
    the slug_binding is deferred until the server assigns the slug."""
    root = Ed25519KeyPair.generate()
    device_signing = Ed25519KeyPair.generate()
    device_kem = KemKeyPair.generate()

    root_pk_b64 = base64url_encode(root.public_key_bytes())
    device_id = derive_public_key_id("dev", device_signing.public_key_bytes())

    identity_cert = _self_sign(
        {
            "type": "identity_cert",
            "version": _CERT_VERSION,
            "root_public_key": root_pk_b64,
            "identity_type": "agent",
            "declared_operator_public_key": operator_root_pubkey,
        },
        root,
        "self_signature",
    )

    device_cert = _self_sign(
        {
            "type": "device_cert",
            "version": _CERT_VERSION,
            "device_id": device_id,
            "root_public_key": root_pk_b64,
            "keys": {
                "signing": {
                    "algorithm": "ed25519",
                    "public_key": base64url_encode(device_signing.public_key_bytes()),
                },
                "encryption": {
                    "algorithm": "x25519",
                    "public_key": base64url_encode(device_kem.public_key_bytes()),
                },
            },
            "issued_at": _now_ms(),
            "expires_at": None,
        },
        root,
        "signature",
    )

    return AgentIdentity(
        agent_id="",
        device_id=device_id,
        root_keypair=root,
        device_signing_keypair=device_signing,
        device_kem_keypair=device_kem,
        identity_cert=identity_cert,
        device_cert=device_cert,
    )


def build_slug_binding(root: Ed25519KeyPair, slug: str) -> dict:
    """Agent-root-signed binding of ``slug`` to the agent root — built once the
    server assigns the slug, then POSTed to /certs/slug_binding to finalize."""
    return _self_sign(
        {
            "type": "slug_binding",
            "version": _CERT_VERSION,
            "slug": slug,
            "root_public_key": base64url_encode(root.public_key_bytes()),
            "issued_at": _now_ms(),
        },
        root,
        "self_signature",
    )
