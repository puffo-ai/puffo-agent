"""Shared fixtures and helpers for the bridge test suites."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Make the in-tree src importable without a pip install.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from puffo_agent.crypto.canonical import canonicalize_for_signing
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.http_auth import sign_request
from puffo_agent.crypto.primitives import Ed25519KeyPair


@dataclass
class TestUser:
    slug: str        # disambiguated slug, e.g. "alice-0001"
    username: str    # bare username, e.g. "alice"
    root_key: Ed25519KeyPair
    device_signing_key: Ed25519KeyPair
    device_id: str
    identity_cert: dict
    device_cert: dict
    slug_binding: dict


def make_user(
    slug: str = "alice-0001",
    device_id: str = "dev_test_one",
    *,
    username: str | None = None,
) -> TestUser:
    """Generate a Human identity + slug_binding + device_cert triple
    signed by a fresh root key. ``username`` is a test-side label only.
    """
    if username is None:
        username = slug.rsplit("-", 1)[0] if "-" in slug else slug

    root_key = Ed25519KeyPair.generate()
    device_signing_key = Ed25519KeyPair.generate()
    device_kem_key = Ed25519KeyPair.generate()  # filler; not used by the bridge
    root_pk_b64 = base64url_encode(root_key.public_key_bytes())

    identity_cert = {
        "type": "identity_cert",
        "version": 1,
        "root_public_key": root_pk_b64,
        "identity_type": "human",
        # Must serialize as ``null`` (not be omitted) for humans.
        "declared_operator_public_key": None,
    }
    identity_cert["self_signature"] = base64url_encode(
        root_key.sign(canonicalize_for_signing(identity_cert)),
    )

    slug_binding = {
        "type": "slug_binding",
        "version": 1,
        "root_public_key": root_pk_b64,
        "slug": slug,
        "issued_at": int(time.time() * 1000),
    }
    slug_binding["self_signature"] = base64url_encode(
        root_key.sign(canonicalize_for_signing(slug_binding)),
    )

    device_cert = {
        "type": "device_cert",
        "version": 1,
        "device_id": device_id,
        "root_public_key": root_pk_b64,
        "keys": {
            "signing": {
                "algorithm": "ed25519",
                "public_key": base64url_encode(device_signing_key.public_key_bytes()),
            },
            "encryption": {
                "algorithm": "x25519",
                "public_key": base64url_encode(device_kem_key.public_key_bytes()),
            },
        },
        "issued_at": int(time.time() * 1000),
        "expires_at": None,
    }
    device_cert["signature"] = base64url_encode(
        root_key.sign(canonicalize_for_signing(device_cert)),
    )

    return TestUser(
        slug=slug,
        username=username,
        root_key=root_key,
        device_signing_key=device_signing_key,
        device_id=device_id,
        identity_cert=identity_cert,
        device_cert=device_cert,
        slug_binding=slug_binding,
    )


def signed_headers(
    user: TestUser, method: str, path: str, body: bytes = b"",
) -> dict[str, str]:
    """Build x-puffo-* headers signed with the user's device signing key.
    The bridge expects the device_id as ``signer_id`` (no subkey
    indirection).
    """
    auth = sign_request(
        signing_key=user.device_signing_key,
        slug=user.slug,
        signer_id=user.device_id,
        method=method,
        path=path,
        body=body,
    )
    return auth.to_dict()


def pair_request_body(user: TestUser) -> bytes:
    """Body for ``POST /v1/pair`` carrying all three certs."""
    return json.dumps({
        "identity_cert": user.identity_cert,
        "slug_binding": user.slug_binding,
        "device_cert": user.device_cert,
    }).encode("utf-8")


# ── Daemon-home fixture ──────────────────────────────────────────────


def isolated_home() -> str:
    """Create a fresh temp dir and point both home env vars at it.

    ``portal/state.py`` reads ``PUFFO_AGENT_HOME`` while
    ``crypto/keystore.py`` reads ``PUFFO_HOME``. Tests must set both
    so keystore and agent.yml resolve to the same place.
    """
    home = tempfile.mkdtemp(prefix="puffo-agent-test-")
    os.environ["PUFFO_AGENT_HOME"] = home
    os.environ["PUFFO_HOME"] = home
    Path(home, "agents").mkdir(parents=True, exist_ok=True)
    return home


def write_test_agent(
    home: str, agent_id: str, *, owner_root_pubkey: str | None = None,
    workspace_files: dict[str, str] | None = None,
) -> Path:
    """Materialise an agent directory under ``<home>/agents/<id>/`` with
    a minimal agent.yml and optional identity_cert in the keystore
    declaring the given owner. Returns the workspace path.
    """
    import yaml

    adir = Path(home) / "agents" / agent_id
    adir.mkdir(parents=True, exist_ok=True)

    workspace = adir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    slug = f"{agent_id}-bot"
    cfg = {
        "id": agent_id,
        "state": "running",
        "display_name": agent_id,
        "puffo_core": {
            "server_url": "http://localhost:3000",
            "slug": slug,
            "device_id": "dev_agent",
            "space_id": "sp_test",
        },
        "runtime": {
            "kind": "chat-local",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api_key": "sk-ant-test-secret",
            "harness": "claude-code",
            "permission_mode": "bypassPermissions",
        },
        "profile": "profile.md",
        "memory_dir": "memory",
        "workspace_dir": "workspace",
        "triggers": {"on_mention": True, "on_dm": True},
    }
    (adir / "agent.yml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    (adir / "profile.md").write_text("# test profile\n", encoding="utf-8")
    (adir / "memory").mkdir(exist_ok=True)

    if owner_root_pubkey is not None:
        keys_dir = adir / "keys"
        keys_dir.mkdir(exist_ok=True)
        identity_cert = {
            "type": "identity_cert",
            "version": 1,
            "root_public_key": "agent-root-pk-placeholder",
            "identity_type": "agent",
            "declared_operator_public_key": owner_root_pubkey,
        }
        # Deliberately unsigned: the ownership check only reads
        # declared_operator_public_key and doesn't re-verify the
        # agent's own self_signature.
        keys_dir.joinpath(f"{slug}.json").write_text(json.dumps({
            "slug": slug,
            "device_id": "dev_agent",
            "root_secret_key": base64url_encode(b"\x01" * 32),
            "device_signing_secret_key": base64url_encode(b"\x02" * 32),
            "kem_secret_key": base64url_encode(b"\x03" * 32),
            "server_url": "http://localhost:3000",
            "identity_cert_json": json.dumps(identity_cert),
        }, indent=2), encoding="utf-8")

    if workspace_files:
        for rel, contents in workspace_files.items():
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            # write_bytes (not write_text) so Windows doesn't translate
            # ``\n`` to ``\r\n`` and break exact-content comparisons.
            target.write_bytes(contents.encode("utf-8"))

    return workspace
