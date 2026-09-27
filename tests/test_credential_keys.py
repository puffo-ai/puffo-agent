"""Credential v2 wire format, checked against the pinned vectors.

The vectors are the contract with the Rust server and the web client, so
these cells check the vectors as much as the code: a vector that only agreed
with the code that wrote it would pin nothing.
"""

import copy
import hashlib
import hmac
import json
import pathlib

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from puffo_agent.crypto.canonical import canonicalize_for_signing
from puffo_agent.crypto.credential_keys import (
    CredentialKeyError,
    compute_credential_wrap_aad,
    derive_credential_kem_keypair,
    open_credential,
    verify_credential_key_cert,
)
from puffo_agent.crypto.encoding import base64url_decode as d
from puffo_agent.crypto.primitives import KemKeyPair

V = json.loads(
    (pathlib.Path(__file__).parent.parent / "src/puffo_agent/crypto/credential_vectors.json").read_text()
)


def test_derivation_matches_an_hkdf_computed_without_the_module():
    """RFC 5869 by hand, from stdlib HMAC: the oracle shares no code with the module."""
    root = d(V["derivation"]["root_secret"])
    prk = hmac.new(b"\x00" * 32, root, hashlib.sha256).digest()
    okm = hmac.new(prk, V["derivation"]["hkdf_info_utf8"].encode() + b"\x01", hashlib.sha256).digest()
    public = X25519PrivateKey.from_private_bytes(okm).public_key().public_bytes_raw()

    assert okm == d(V["derivation"]["kem_secret_key"])
    assert public == d(V["derivation"]["kem_public_key"])
    assert derive_credential_kem_keypair(root, 1).public_key_bytes() == public


def test_the_cert_verifies_and_signs_exactly_the_pinned_bytes():
    c = V["cert"]
    assert canonicalize_for_signing(c["cert"]).decode() == c["signed_bytes_utf8"]
    assert verify_credential_key_cert(
        c["cert"], root_public_key=d(c["root_public_key"]), slug=c["slug"], key_version=1
    ) == d(V["derivation"]["kem_public_key"])


def test_a_substituted_cert_fails_against_the_callers_own_anchor():
    """Amendment 6. The positive control is that the same cert verifies against
    the root that did sign it — otherwise a verifier that rejects everything
    would pass this cell too."""
    evil = V["cert_substituted"]
    with pytest.raises(CredentialKeyError, match="signature"):
        verify_credential_key_cert(
            evil["cert"], root_public_key=d(V["cert"]["root_public_key"]),
            slug=V["cert"]["slug"], key_version=1,
        )
    verify_credential_key_cert(
        evil["cert"], root_public_key=d(evil["root_public_key"]),
        slug=V["cert"]["slug"], key_version=1,
    )


@pytest.mark.parametrize(
    "slug, key_version, refusal",
    [("agt-someone-else", 1, "different slug"), ("agt-vector-0001", 2, "different key_version")],
)
def test_a_genuine_cert_is_refused_when_it_does_not_match_the_request(slug, key_version, refusal):
    """A valid cert registered under another slug, or declared as another key
    version. The edited-cert cells cannot see these checks: the signature
    fails there first."""
    with pytest.raises(CredentialKeyError, match=refusal):
        verify_credential_key_cert(
            V["cert"]["cert"], root_public_key=d(V["cert"]["root_public_key"]),
            slug=slug, key_version=key_version,
        )


@pytest.mark.parametrize(
    "field, value",
    [
        ("slug", "agt-someone-else"),
        ("key_version", 2),
        ("kem_public_key", V["cert_substituted"]["cert"]["kem_public_key"]),
    ],
)
def test_a_cert_edited_after_signing_is_refused(field, value):
    cert = copy.deepcopy(V["cert"]["cert"])
    cert[field] = value
    with pytest.raises(CredentialKeyError):
        verify_credential_key_cert(
            cert, root_public_key=d(V["cert"]["root_public_key"]),
            slug=cert["slug"], key_version=cert["key_version"],
        )


def test_the_wrap_opens_under_the_pinned_aad():
    w = V["wrap"]
    aad = compute_credential_wrap_aad(**w["aad_fields"])
    assert aad.hex() == w["aad_hex"]
    recipient = KemKeyPair.from_secret_bytes(d(w["recipient_kem_secret_key"]))
    assert open_credential(recipient, aad, d(w["blob"])).decode() == w["plaintext_utf8"]


@pytest.mark.parametrize(
    "field, value",
    [
        ("credential_id", "00000000-0000-5000-8000-000000000000"),
        ("version", 2),
        ("recipient_slug", "agt-someone-else"),
        ("credential_type", "PUFFO_GOOGLE_OAUTH_v1"),
        ("key_version", 2),
    ],
)
def test_a_wrap_moved_to_another_row_or_recipient_does_not_open(field, value):
    w = V["wrap"]
    moved = dict(w["aad_fields"], **{field: value})
    recipient = KemKeyPair.from_secret_bytes(d(w["recipient_kem_secret_key"]))
    with pytest.raises(CredentialKeyError):
        open_credential(recipient, compute_credential_wrap_aad(**moved), d(w["blob"]))


def test_credential_id_matches_the_literal_the_rust_server_asserts():
    """puffo-server be49551 pins the same literal in its Rust test, so a drift
    on either side reds one of the two suites rather than every real fetch."""
    from puffo_agent.crypto.credential_keys import credential_id

    assert credential_id("agt-vector-0001", "CUSTOMIZED", 7) == "3b0fec1b-f7fa-5cc4-a18d-9d5ce8c401ce"
