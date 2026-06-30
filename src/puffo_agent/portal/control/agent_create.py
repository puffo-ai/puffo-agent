"""Daemon-side agent identity generation for self-service ws-local create.

The daemon mints the agent's own keys + self-signed certs (identity / device /
slug_binding). The operator signs only the OperatorAttestation; registration
runs server-side (POST /agents → pending_token, then POST /certs/slug_binding).
Cert wire shapes match portal/api/certs.py (the daemon's own verifier) and the
puffo-server `core-v2/crates/types/src/cert.rs` producer.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

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


_DEFAULT_WS_LOCAL_PROFILE = "# {name}\n\nA ws-local agent driven by an attached tool.\n"

# async (slug_binding, pending_token) -> None; raises on failure.
FinalizeFn = Callable[[dict, str], Awaitable[None]]
# async (operator_slug, payload) -> None — sends the request machine_message.
SendRequestFn = Callable[[str, dict], Awaitable[None]]
# async (request_id) -> {agent_slug, pending_token} — resolves on the approval command.
AwaitApprovalFn = Callable[[str], Awaitable[dict]]


async def create_ws_local_agent(
    operator_slug: str,
    passcode: str,
    *,
    send_request_fn: SendRequestFn,
    await_approval_fn: AwaitApprovalFn,
    finalize_fn: FinalizeFn,
    display_name: str = "",
) -> dict:
    """Full daemon-side flow: validate the operator is linked, mint the agent
    identity, request the operator's approval over the reverse channel, then
    register + write + pack on approval. All I/O is injected for testability."""
    from .store import get_pairing

    pairing = get_pairing(operator_slug)
    if pairing is None:
        raise ValueError(f"operator {operator_slug!r} is not linked to this machine")

    ident = gen_agent_identity(pairing.operator_root_pubkey)
    request_id = f"acr_{uuid.uuid4().hex}"
    await send_request_fn(
        operator_slug,
        {
            "type": "agent.create_request",
            "request_id": request_id,
            "username": display_name or "agent",
            "identity_cert": ident.identity_cert,
            "device_cert": ident.device_cert,
            "agent_root_public_key": ident.root_public_key,
        },
    )
    approval = await await_approval_fn(request_id)
    return await finalize_and_pack(
        ident,
        slug=str(approval["agent_slug"]),
        pending_token=str(approval["pending_token"]),
        operator_slug=operator_slug,
        server_url=pairing.server_url,
        passcode=passcode,
        finalize_fn=finalize_fn,
        display_name=display_name,
    )


async def finalize_and_pack(
    ident: AgentIdentity,
    *,
    slug: str,
    pending_token: str,
    operator_slug: str,
    server_url: str,
    passcode: str,
    finalize_fn: FinalizeFn,
    display_name: str = "",
) -> dict:
    """After the operator approves and the server mints ``slug`` + ``pending_token``:
    sign + register the slug_binding, write the ws-local agent.yml + keystore, and
    pack the ``.puffoagent`` bundle. Returns the stdout result for the caller."""
    from ...crypto.keystore import KeyStore, StoredIdentity
    from ..export import pack
    from ..state import AgentConfig, PuffoCoreConfig, RuntimeConfig, agent_dir

    agent_id = slug
    binding = build_slug_binding(ident.root_keypair, slug)
    await finalize_fn(binding, pending_token)  # POST /certs/slug_binding; raises on failure

    target = agent_dir(agent_id)
    target.mkdir(parents=True, exist_ok=True)
    (target / "memory").mkdir(exist_ok=True)
    (target / "profile.md").write_text(
        _DEFAULT_WS_LOCAL_PROFILE.format(name=display_name or slug), encoding="utf-8"
    )

    KeyStore.for_agent(agent_id).save_identity(
        StoredIdentity(
            slug=slug,
            device_id=ident.device_id,
            root_secret_key=base64url_encode(ident.root_keypair.secret_bytes()),
            device_signing_secret_key=base64url_encode(ident.device_signing_keypair.secret_bytes()),
            kem_secret_key=base64url_encode(ident.device_kem_keypair.secret_bytes()),
            server_url=server_url,
            slug_binding_json=json.dumps(binding),
            identity_cert_json=json.dumps(ident.identity_cert),
        )
    )

    AgentConfig(
        id=agent_id,
        display_name=display_name or slug,
        puffo_core=PuffoCoreConfig(
            server_url=server_url, slug=slug, device_id=ident.device_id, operator_slug=operator_slug
        ),
        runtime=RuntimeConfig(kind="ws-local"),
        created_at=int(time.time()),
    ).save()

    bundle_path = target.parent.parent / f"{agent_id}.puffoagent"
    bundle_path.write_bytes(pack([agent_id], passcode, exported_by_slug=slug))

    return {
        "agent_slug": slug,
        "agent_id": agent_id,
        "bundle_path": str(bundle_path),
        "passcode": passcode,
    }


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
