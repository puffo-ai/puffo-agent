"""Daemon-side agent identity generation for self-service ws-local create.

The daemon mints the agent's own keys + self-signed certs (identity / device /
slug_binding). The operator signs only the OperatorAttestation; registration
runs server-side (POST /agents → pending_token, then POST /certs/slug_binding).
Cert wire shapes match portal/api/certs.py (the daemon's own verifier) and the
puffo-server `core-v2/crates/types/src/cert.rs` producer.
"""

from __future__ import annotations

import asyncio
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


@dataclass
class _PendingCreate:
    identity: AgentIdentity
    operator_slug: str
    server_url: str
    passcode: str


class CreateRegistry:
    """request_id-keyed pending creates + command_id-keyed results. A
    machine-initiated create returns immediately with a request_id; the
    operator's later approval command (command_id == request_id) finalizes it,
    and ``wait_result`` lets a caller block on completion."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingCreate] = {}
        self._results: dict[str, dict] = {}
        self._waiters: dict[str, list[asyncio.Future]] = {}

    def put_pending(self, request_id: str, pc: _PendingCreate) -> None:
        self._pending[request_id] = pc

    def pop_pending(self, request_id: str) -> "_PendingCreate | None":
        return self._pending.pop(request_id, None)

    def record_result(self, command_id: str, result: dict) -> None:
        self._results[command_id] = result
        for fut in self._waiters.pop(command_id, []):
            if not fut.done():
                fut.set_result(result)

    def peek_result(self, command_id: str) -> "dict | None":
        return self._results.get(command_id)

    async def wait_result(self, command_id: str, timeout: float) -> dict:
        existing = self._results.get(command_id)
        if existing is not None:
            return existing
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._waiters.setdefault(command_id, []).append(fut)
        return await asyncio.wait_for(fut, timeout)


_REGISTRY = CreateRegistry()


def get_registry() -> CreateRegistry:
    return _REGISTRY


async def start_create(
    operator_slug: str, passcode: str, *, username: str = "", message: str = ""
) -> dict:
    """Mint the agent identity, stash it under a fresh request_id, send the
    operator the approval request, and return immediately (non-blocking). The
    operator's approval command (command_id == request_id) finalizes it.
    ``message`` is free text the requesting agent shows the operator for context."""
    from .reporter import get_reporter
    from .store import get_pairing

    pairing = get_pairing(operator_slug)
    if pairing is None:
        raise ValueError(f"operator {operator_slug!r} is not linked to this machine")
    ident = gen_agent_identity(pairing.operator_root_pubkey)
    request_id = f"acr_{uuid.uuid4().hex}"
    get_registry().put_pending(
        request_id, _PendingCreate(ident, operator_slug, pairing.server_url, passcode)
    )
    await get_reporter().send_to_operator(
        operator_slug,
        {
            "type": "agent.create_request",
            "request_id": request_id,
            "username": username or "agent",
            "message": message,
            "identity_cert": ident.identity_cert,
            "device_cert": ident.device_cert,
            "agent_root_public_key": ident.root_public_key,
        },
    )
    return {"request_id": request_id, "agent_root_public_key": ident.root_public_key}


async def finalize_from_command(request_id: str, params: dict) -> dict:
    """Run on the operator's approval command (command_id == request_id): pull the
    stashed identity, finalize against the server, write + pack. ``params`` carry
    the operator-chosen profile + the server-minted slug + pending_token."""
    pc = get_registry().pop_pending(request_id)
    if pc is None:
        raise ValueError(f"no pending create for request {request_id!r}")

    async def _finalize(binding: dict, pending_token: str) -> None:
        await post_slug_binding(pc.server_url, binding, pending_token)

    return await finalize_and_pack(
        pc.identity,
        slug=str(params["agent_slug"]),
        pending_token=str(params["pending_token"]),
        operator_slug=pc.operator_slug,
        server_url=pc.server_url,
        passcode=pc.passcode,
        finalize_fn=_finalize,
        profile=params,
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
    profile: dict | None = None,
) -> dict:
    """After the operator approves and the server mints ``slug`` + ``pending_token``:
    sign + register the slug_binding, write the ws-local agent.yml + keystore, and
    pack the ``.puffoagent`` bundle. ``profile`` carries the operator-chosen
    name / avatar / role / soul / space_id from the approval. Returns the stdout
    result for the caller."""
    from ...crypto.keystore import KeyStore, StoredIdentity
    from ..export import pack
    from ..state import AgentConfig, PuffoCoreConfig, RuntimeConfig, agent_dir

    profile = profile or {}
    name = str(profile.get("name") or display_name or slug)
    soul = str(profile.get("soul") or "")

    agent_id = slug
    binding = build_slug_binding(ident.root_keypair, slug)
    await finalize_fn(binding, pending_token)  # POST /certs/slug_binding; raises on failure

    target = agent_dir(agent_id)
    target.mkdir(parents=True, exist_ok=True)
    (target / "memory").mkdir(exist_ok=True)
    (target / "profile.md").write_text(
        soul or _DEFAULT_WS_LOCAL_PROFILE.format(name=name), encoding="utf-8"
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
        display_name=name,
        avatar_url=str(profile.get("avatar") or ""),
        role=str(profile.get("role") or ""),
        puffo_core=PuffoCoreConfig(
            server_url=server_url,
            slug=slug,
            device_id=ident.device_id,
            operator_slug=operator_slug,
            space_id=str(profile.get("space_id") or ""),
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


async def post_slug_binding(server_url: str, slug_binding: dict, pending_token: str) -> None:
    """POST /certs/slug_binding to finalize the pending agent identity (the
    token + self-signature are the gate; no auth header)."""
    import aiohttp

    url = f"{server_url.rstrip('/')}/certs/slug_binding"
    body = {"pending_token": pending_token, "slug_binding": slug_binding}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(url, json=body) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"slug_binding finalize HTTP {resp.status}: {text[:200]}")


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
