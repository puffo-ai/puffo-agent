"""Certificate verification used by remote agent provisioning."""

from __future__ import annotations

from ...crypto.canonical import canonicalize_for_signing
from ...crypto.encoding import base64url_decode
from ...crypto.primitives import ed25519_verify


class CertError(Exception):
    pass


def _decode(value, field: str, length: int) -> bytes:
    if not isinstance(value, str):
        raise CertError(f"{field} missing")
    try:
        decoded = base64url_decode(value)
    except Exception as exc:
        raise CertError(f"{field} decode: {exc}") from exc
    if len(decoded) != length:
        raise CertError(f"{field} must be {length} bytes")
    return decoded


def verify_identity_cert(cert: dict) -> bytes:
    if not isinstance(cert, dict):
        raise CertError("identity_cert must be an object")
    if cert.get("type") != "identity_cert":
        raise CertError(f"unexpected cert_type {cert.get('type')!r}")
    root_key = _decode(cert.get("root_public_key"), "identity_cert root_public_key", 32)
    signature = _decode(cert.get("self_signature"), "identity_cert self_signature", 64)
    if not ed25519_verify(root_key, canonicalize_for_signing(cert), signature):
        raise CertError("identity_cert self_signature verification failed")
    return root_key


def verify_slug_binding(binding: dict, root_key: bytes) -> str:
    if not isinstance(binding, dict):
        raise CertError("slug_binding must be an object")
    if binding.get("type") != "slug_binding":
        raise CertError(f"unexpected slug_binding type {binding.get('type')!r}")
    slug = binding.get("slug")
    if not isinstance(slug, str) or not slug:
        raise CertError("slug_binding missing slug")
    declared_root = _decode(
        binding.get("root_public_key"), "slug_binding root_public_key", 32,
    )
    if declared_root != root_key:
        raise CertError("slug_binding.root_public_key does not match identity_cert")
    signature = _decode(binding.get("self_signature"), "slug_binding self_signature", 64)
    if not ed25519_verify(root_key, canonicalize_for_signing(binding), signature):
        raise CertError("slug_binding self_signature verification failed")
    return slug


def verify_device_cert(cert: dict, root_key: bytes) -> bytes:
    if not isinstance(cert, dict):
        raise CertError("device_cert must be an object")
    if cert.get("type") != "device_cert":
        raise CertError(f"unexpected cert_type {cert.get('type')!r}")
    declared_root = _decode(cert.get("root_public_key"), "device_cert root_public_key", 32)
    if declared_root != root_key:
        raise CertError("device_cert.root_public_key does not match identity_cert")
    keys = cert.get("keys")
    signing = keys.get("signing") if isinstance(keys, dict) else None
    if not isinstance(signing, dict):
        raise CertError("device_cert.keys.signing missing")
    if signing.get("algorithm") != "ed25519":
        raise CertError(
            "device_cert.keys.signing.algorithm must be 'ed25519', "
            f"got {signing.get('algorithm')!r}"
        )
    if not isinstance(cert.get("device_id"), str) or not cert["device_id"]:
        raise CertError("device_cert missing device_id")
    signing_key = _decode(
        signing.get("public_key"), "device_cert signing_public_key", 32,
    )
    signature = _decode(cert.get("signature"), "device_cert signature", 64)
    if not ed25519_verify(root_key, canonicalize_for_signing(cert), signature):
        raise CertError("device_cert signature verification failed")
    return signing_key
