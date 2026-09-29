"""Credential v2 wire format, checked against the pinned vectors.

The vectors are checked as much as the code: one that only agreed with its
own writer would pin nothing.
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
    """RFC 5869 by hand from stdlib HMAC: the oracle shares no code."""
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
    """Amendment 6, with the positive control: the same cert verifies against
    the root that did sign it."""
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
    """The edited-cert cells cannot reach these: the signature fails first."""
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
    """puffo-server be49551 pins the same literal, so drift reds a suite."""
    from puffo_agent.crypto.credential_keys import credential_id

    assert credential_id("agt-vector-0001", "CUSTOMIZED", 7) == "3b0fec1b-f7fa-5cc4-a18d-9d5ce8c401ce"


# Sealed by the Rust server (puffo-server cd067dc CI), to the recipient in
# V["derivation"]. The file is kept byte-for-byte as delivered.
_RUST_PATH = pathlib.Path(__file__).parent / "vectors/rust_credential_vector_cd067dc.json"
R = json.loads(_RUST_PATH.read_text())


def _rust_recipient():
    recipient = KemKeyPair.from_secret_bytes(d(V["derivation"]["kem_secret_key"]))
    assert recipient.public_key_bytes() == d(R["recipient_kem_public_key"])
    return recipient


def test_a_rust_sealed_wrap_opens_under_an_aad_the_daemon_computes_itself():
    """AAD recomputed here, not read from the file's hex."""
    assert hashlib.sha256(_RUST_PATH.read_bytes()).hexdigest() == (
        "c9127965378ecfade172520cac240a68bd3a64317b7cef85a79c533f7396aa7a"
    )
    aad = compute_credential_wrap_aad(**R["aad_fields"])
    assert aad.hex() == R["aad_hex"]
    assert open_credential(_rust_recipient(), aad, d(R["blob"])).decode() == R["plaintext_utf8"]


def _flip_last_aad_byte(aad, blob):
    return aad[:-1] + bytes([aad[-1] ^ 1]), blob


def _swap_enc_and_ct(aad, blob):
    return aad, blob[32:] + blob[:32]


@pytest.mark.parametrize("damage", [_flip_last_aad_byte, _swap_enc_and_ct])
def test_the_rust_sealed_wrap_does_not_open_once_damaged(damage):
    aad, blob = damage(compute_credential_wrap_aad(**R["aad_fields"]), d(R["blob"]))
    with pytest.raises(CredentialKeyError):
        open_credential(_rust_recipient(), aad, blob)


def test_the_rust_id_vector_matches_the_daemons_derivation():
    from puffo_agent.crypto.credential_keys import credential_id

    v = R["id_vector"]
    assert credential_id(v["owner_slug"], v["credential_type"], v["index"]) == v["id"]


@pytest.mark.parametrize(
    "field, value, refusal",
    [
        ("type", "subkey_cert", "not a v1 credential key cert"),
        ("version", 2, "not a v1 credential key cert"),
        ("kem_public_key", "AAAA", "not 32 bytes"),
        ("signature", "!!not base64!!", "malformed cert"),
    ],
)
def test_a_cert_is_refused_before_its_signature_is_trusted(field, value, refusal):
    """Shape first: refused before reaching the verifier."""
    cert = copy.deepcopy(V["cert"]["cert"])
    cert[field] = value
    with pytest.raises(CredentialKeyError, match=refusal):
        verify_credential_key_cert(
            cert, root_public_key=d(V["cert"]["root_public_key"]),
            slug=V["cert"]["slug"], key_version=1,
        )


def test_a_cert_missing_a_required_field_is_refused():
    cert = copy.deepcopy(V["cert"]["cert"])
    del cert["signature"]
    with pytest.raises(CredentialKeyError, match="malformed cert"):
        verify_credential_key_cert(
            cert, root_public_key=d(V["cert"]["root_public_key"]),
            slug=V["cert"]["slug"], key_version=1,
        )


def test_a_blob_with_no_room_for_a_ciphertext_is_refused_without_decrypting():
    with pytest.raises(CredentialKeyError, match="too short"):
        open_credential(_rust_recipient(), b"aad", bytes(32))


def test_key_version_starts_at_one():
    with pytest.raises(ValueError, match="key_version starts at 1"):
        derive_credential_kem_keypair(bytes(32), 0)


@pytest.mark.parametrize(
    "override",
    [{"version": -1}, {"key_version": 1 << 63}],
)
def test_an_aad_integer_outside_i64_is_refused(override):
    """Fixed-width contract with Rust's i64: refuse rather than emit other bytes."""
    with pytest.raises(ValueError, match="i64"):
        compute_credential_wrap_aad(**{**V["wrap"]["aad_fields"], **override})


@pytest.mark.parametrize("field", ["recipient_slug", "credential_type"])
def test_an_empty_aad_string_is_refused(field):
    with pytest.raises(ValueError, match="1..65535"):
        compute_credential_wrap_aad(**{**V["wrap"]["aad_fields"], field: ""})
