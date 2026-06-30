"""The daemon-minted agent identity must pass the daemon's own cert verifiers
(portal/api/certs.py), which mirror the puffo-server producer's wire format."""

from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.primitives import Ed25519KeyPair
from puffo_agent.portal.api.certs import (
    verify_device_cert,
    verify_identity_cert,
    verify_slug_binding,
)
from puffo_agent.portal.control.agent_create import build_slug_binding, gen_agent_identity


def _operator_root() -> str:
    return base64url_encode(Ed25519KeyPair.generate().public_key_bytes())


def test_generated_certs_pass_daemon_verifiers():
    operator_root = _operator_root()
    ident = gen_agent_identity(operator_root)

    root_pk = verify_identity_cert(ident.identity_cert)
    assert base64url_encode(root_pk) == ident.root_public_key
    assert ident.identity_cert["declared_operator_public_key"] == operator_root
    assert ident.identity_cert["identity_type"] == "agent"

    # device_cert chains to the same agent root.
    verify_device_cert(ident.device_cert, root_pk)

    # slug_binding is built once the server assigns the slug.
    binding = build_slug_binding(ident.root_keypair, "helper-1234")
    assert verify_slug_binding(binding, root_pk) == "helper-1234"


def test_device_id_derived_from_signing_key():
    ident = gen_agent_identity(_operator_root())
    assert ident.device_id.startswith("dev_")
    assert ident.device_cert["device_id"] == ident.device_id
    assert ident.device_cert["keys"]["signing"]["algorithm"] == "ed25519"
    assert ident.device_cert["keys"]["encryption"]["algorithm"] == "x25519"
