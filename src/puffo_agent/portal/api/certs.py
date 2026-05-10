"""Identity / device cert verification for the bridge pair handshake.

The bridge verifies identity / device / slug-binding certs on its
own to bind a paired (slug, device_id) to a verified
``root_public_key``. Both certs are RFC 8785-canonicalised via
``canonicalize_for_signing`` (which strips ``signature`` and
``self_signature``), matching the Rust producer's wire format.
v2 device_cert nests pubkeys under ``keys.signing`` /
``keys.encryption``, each tagged with an algorithm string.
"""

from __future__ import annotations

from ...crypto.canonical import canonicalize_for_signing
from ...crypto.encoding import base64url_decode
from ...crypto.primitives import ed25519_verify


class CertError(Exception):
    """Any cert-shape or signature failure during pair verification."""


def verify_identity_cert(cert: dict) -> bytes:
    """Verify ``self_signature`` against the declared
    ``root_public_key``. Returns the 32-byte root pubkey on success.
    Raises ``CertError`` on any failure. Does NOT enforce
    identity_type — caller decides Human vs Agent.
    """
    if not isinstance(cert, dict):
        raise CertError("identity_cert must be an object")
    # Wire field is ``type`` (Rust serialises ``cert_type`` via
    # serde rename), not ``cert_type``.
    if cert.get("type") != "identity_cert":
        raise CertError(f"unexpected cert_type {cert.get('type')!r}")
    root_pk_b64 = cert.get("root_public_key")
    sig_b64 = cert.get("self_signature")
    if not isinstance(root_pk_b64, str) or not isinstance(sig_b64, str):
        raise CertError("identity_cert missing root_public_key/self_signature")
    try:
        root_pk = base64url_decode(root_pk_b64)
    except Exception as exc:
        raise CertError(f"identity_cert root_public_key decode: {exc}") from exc
    if len(root_pk) != 32:
        raise CertError("identity_cert root_public_key must be 32 bytes")
    try:
        sig = base64url_decode(sig_b64)
    except Exception as exc:
        raise CertError(f"identity_cert self_signature decode: {exc}") from exc
    if len(sig) != 64:
        raise CertError("identity_cert self_signature must be 64 bytes")
    canonical = canonicalize_for_signing(cert)
    if not ed25519_verify(root_pk, canonical, sig):
        raise CertError("identity_cert self_signature verification failed")
    return root_pk


def verify_slug_binding(binding: dict, root_pk: bytes) -> str:
    """Verify the slug_binding cert against the root pubkey from the
    identity_cert. Returns the slug string on success.

    The disambiguated slug (e.g. ``"alice-a62c"``) used by the chat
    protocol is cryptographically bound to a root pubkey via this
    cert — identity_cert itself no longer carries a username field.
    """
    if not isinstance(binding, dict):
        raise CertError("slug_binding must be an object")
    if binding.get("type") != "slug_binding":
        raise CertError(f"unexpected slug_binding type {binding.get('type')!r}")
    declared_root_b64 = binding.get("root_public_key")
    sig_b64 = binding.get("self_signature")
    slug = binding.get("slug")
    if not isinstance(declared_root_b64, str) or not isinstance(sig_b64, str):
        raise CertError("slug_binding missing root_public_key/self_signature")
    if not isinstance(slug, str) or not slug:
        raise CertError("slug_binding missing slug")
    try:
        declared_root = base64url_decode(declared_root_b64)
    except Exception as exc:
        raise CertError(f"slug_binding root_public_key decode: {exc}") from exc
    if declared_root != root_pk:
        raise CertError("slug_binding.root_public_key does not match identity_cert")
    try:
        sig = base64url_decode(sig_b64)
    except Exception as exc:
        raise CertError(f"slug_binding self_signature decode: {exc}") from exc
    if len(sig) != 64:
        raise CertError("slug_binding self_signature must be 64 bytes")
    canonical = canonicalize_for_signing(binding)
    if not ed25519_verify(root_pk, canonical, sig):
        raise CertError("slug_binding self_signature verification failed")
    return slug


def verify_device_cert(cert: dict, root_pk: bytes) -> bytes:
    """Verify ``signature`` against ``root_pk`` (lifted from the
    identity_cert). Returns the 32-byte device signing pubkey on
    success.

    Enforces that the cert's own ``root_public_key`` matches the
    supplied ``root_pk`` — otherwise a malicious client could pair an
    identity_cert for slug A with a device_cert for slug B (different
    root chain) and the daemon would verify request sigs against the
    wrong device. ``signing.algorithm`` MUST be ``"ed25519"``.
    """
    if not isinstance(cert, dict):
        raise CertError("device_cert must be an object")
    if cert.get("type") != "device_cert":
        raise CertError(f"unexpected cert_type {cert.get('type')!r}")
    declared_root_b64 = cert.get("root_public_key")
    sig_b64 = cert.get("signature")
    keys = cert.get("keys")
    device_id = cert.get("device_id")
    if not isinstance(declared_root_b64, str) or not isinstance(sig_b64, str):
        raise CertError("device_cert missing root_public_key/signature")
    if not isinstance(keys, dict):
        raise CertError("device_cert missing keys (v2 nested {signing, encryption})")
    signing_block = keys.get("signing")
    if not isinstance(signing_block, dict):
        raise CertError("device_cert.keys.signing missing")
    signing_pk_b64 = signing_block.get("public_key")
    signing_alg = signing_block.get("algorithm")
    if not isinstance(signing_pk_b64, str):
        raise CertError("device_cert.keys.signing.public_key missing")
    if signing_alg != "ed25519":
        raise CertError(
            f"device_cert.keys.signing.algorithm must be 'ed25519', got {signing_alg!r}"
        )
    if not isinstance(device_id, str) or not device_id:
        raise CertError("device_cert missing device_id")
    try:
        declared_root = base64url_decode(declared_root_b64)
    except Exception as exc:
        raise CertError(f"device_cert root_public_key decode: {exc}") from exc
    if declared_root != root_pk:
        raise CertError("device_cert.root_public_key does not match identity_cert")
    try:
        sig = base64url_decode(sig_b64)
    except Exception as exc:
        raise CertError(f"device_cert signature decode: {exc}") from exc
    if len(sig) != 64:
        raise CertError("device_cert signature must be 64 bytes")
    try:
        signing_pk = base64url_decode(signing_pk_b64)
    except Exception as exc:
        raise CertError(f"device_cert signing_public_key decode: {exc}") from exc
    if len(signing_pk) != 32:
        raise CertError("device_cert signing_public_key must be 32 bytes")
    canonical = canonicalize_for_signing(cert)
    if not ed25519_verify(root_pk, canonical, sig):
        raise CertError("device_cert signature verification failed")
    return signing_pk
