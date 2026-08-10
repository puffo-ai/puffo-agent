"""Validation for web-signed Agent provisioning bundles."""

from __future__ import annotations

from typing import Any

from ...crypto.encoding import base64url_decode
from ..state import RuntimeConfig, is_valid_agent_id
from .certs import (
    CertError,
    verify_device_cert,
    verify_identity_cert,
    verify_slug_binding,
)
from .profile_content import MAX_ROLE_LEN, MAX_ROLE_SHORT_LEN, derive_role_short

# Preserve the name used by the original handler implementation.
_derive_role_short = derive_role_short


def _verify_attestation(att: dict, agent_root_pk: bytes, paired_root_pk: bytes) -> None:
    """Verify operator_attestation: signature valid (signed by the
    operator root key) and both pubkeys match the bound expectations.
    Raises ``CertError`` on mismatch.
    """
    from ...crypto.canonical import canonicalize_for_signing
    from ...crypto.primitives import ed25519_verify

    if not isinstance(att, dict):
        raise CertError("operator_attestation must be an object")
    if att.get("type") != "operator_attestation":
        raise CertError(f"unexpected attestation type {att.get('type')!r}")
    op_pk_b64 = att.get("operator_root_public_key")
    agent_pk_b64 = att.get("agent_root_public_key")
    sig_b64 = att.get("signature")
    if (
        not isinstance(op_pk_b64, str)
        or not isinstance(agent_pk_b64, str)
        or not isinstance(sig_b64, str)
    ):
        raise CertError("operator_attestation missing required fields")

    try:
        op_pk = base64url_decode(op_pk_b64)
        agent_pk = base64url_decode(agent_pk_b64)
        sig = base64url_decode(sig_b64)
    except Exception as exc:
        raise CertError(f"attestation field decode: {exc}") from exc
    if op_pk != paired_root_pk:
        raise CertError("attestation.operator_root_public_key != paired user")
    if agent_pk != agent_root_pk:
        raise CertError("attestation.agent_root_public_key != agent identity_cert")
    canonical = canonicalize_for_signing(
        {k: v for k, v in att.items() if k != "signature"}
    )
    if not ed25519_verify(op_pk, canonical, sig):
        raise CertError("attestation signature verification failed")


class ProvisionError(Exception):
    """A create-agent bundle was inconsistent or could not be written.
    ``fields`` carry structured context for the HTTP handler's log line."""

    def __init__(self, reason: str, **fields: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fields = fields


def _verify_agent_bundle(payload: dict, paired_root_pubkey_b64: str) -> dict:
    """Verify a create-agent bundle (certs, attestation, puffo_core, runtime)
    against the paired operator. Returns a context dict the writer consumes.
    No disk writes, no network. Raises ``ProvisionError`` on any mismatch."""
    paired_root_pk = _decode_paired_root_key(paired_root_pubkey_b64)
    bundle, pc, rt = _required_sections(payload)
    verified = _verify_identity_bundle(bundle, paired_root_pubkey_b64, paired_root_pk)
    core = _validated_puffo_core(pc, verified["slug"], verified["device_id"])
    agent_id = core["slug"]
    if not is_valid_agent_id(agent_id):
        raise ProvisionError(f"slug {agent_id!r} is not a valid agent id")
    profile = _validated_profile(payload, agent_id)
    runtime = _validated_runtime(rt)
    desired_skills, desired_mcps = _validated_desired_templates(payload)

    return {
        "agent_id": agent_id,
        **profile,
        **core,
        "runtime": runtime,
        "desired_skills": list(desired_skills),
        "desired_mcps": list(desired_mcps),
        "bundle": bundle,
    }


def _decode_paired_root_key(paired_root_pubkey_b64: str) -> bytes:
    try:
        return base64url_decode(paired_root_pubkey_b64)
    except Exception as exc:
        raise ProvisionError(f"paired root pubkey decode: {exc}") from exc


def _required_sections(payload: dict) -> tuple[dict, dict, dict]:
    if not isinstance(payload, dict):
        raise ProvisionError("body must be a JSON object")
    bundle, pc, rt = (
        payload.get("identity_bundle"),
        payload.get("puffo_core"),
        payload.get("runtime"),
    )
    if not all(isinstance(value, dict) for value in (bundle, pc, rt)):
        raise ProvisionError(
            "identity_bundle, puffo_core, and runtime are required",
            bundle_type=type(bundle).__name__,
            pc_type=type(pc).__name__,
            rt_type=type(rt).__name__,
        )
    return bundle, pc, rt


def _verify_identity_bundle(
    bundle: dict, paired_root_b64: str, paired_root_pk: bytes
) -> dict:
    identity, device, attestation, binding = (
        bundle.get("identity_cert"),
        bundle.get("device_cert"),
        bundle.get("operator_attestation"),
        bundle.get("slug_binding"),
    )
    if not all(
        isinstance(value, dict) for value in (identity, device, attestation, binding)
    ):
        raise ProvisionError(
            "identity_bundle missing one of identity_cert/device_cert/operator_attestation/slug_binding"
        )
    root_key = _verified_identity_root(identity, paired_root_b64)
    _verify_bundle_device(device, root_key)
    slug = _verify_bundle_slug_binding(binding, root_key)
    _verify_bundle_attestation(attestation, root_key, paired_root_pk)
    return {"slug": slug, "device_id": device.get("device_id")}


def _verified_identity_root(identity: dict, paired_root_b64: str) -> bytes:
    try:
        root_key = verify_identity_cert(identity)
    except CertError as exc:
        raise ProvisionError(f"identity_cert: {exc}") from exc
    if identity.get("identity_type") != "agent":
        raise ProvisionError(
            "identity_cert.identity_type must be 'agent'",
            got=identity.get("identity_type"),
        )
    declared = identity.get("declared_operator_public_key")
    if not isinstance(declared, str) or not declared:
        raise ProvisionError(
            "identity_cert.declared_operator_public_key required for agent identity"
        )
    if declared != paired_root_b64:
        raise ProvisionError(
            "identity_cert.declared_operator_public_key does not match paired operator",
            cert=declared,
            paired=paired_root_b64,
        )
    return root_key


def _verify_bundle_device(device: dict, root_key: bytes) -> None:
    try:
        verify_device_cert(device, root_key)
    except CertError as exc:
        raise ProvisionError(f"device_cert: {exc}") from exc


def _verify_bundle_slug_binding(binding: dict, root_key: bytes) -> str:
    try:
        return verify_slug_binding(binding, root_key)
    except CertError as exc:
        raise ProvisionError(f"slug_binding: {exc}") from exc


def _verify_bundle_attestation(
    attestation: dict, root_key: bytes, paired_root: bytes
) -> None:
    try:
        _verify_attestation(attestation, root_key, paired_root)
    except CertError as exc:
        raise ProvisionError(f"operator_attestation: {exc}") from exc


def _validated_puffo_core(
    pc: dict, slug_from_binding: str, device_from_cert: object
) -> dict:
    core = {
        key: (pc.get(key) or "").strip()
        for key in ("server_url", "slug", "device_id", "space_id", "operator_slug")
    }
    if not all(core[key] for key in ("server_url", "slug", "device_id", "space_id")):
        raise ProvisionError(
            "puffo_core block must include server_url, slug, device_id, space_id",
            keys_present=sorted(pc.keys()),
        )
    if core["slug"] != slug_from_binding:
        raise ProvisionError(
            "puffo_core.slug != slug_binding.slug",
            puffo_core=core["slug"],
            slug_binding=slug_from_binding,
        )
    if core["device_id"] != device_from_cert:
        raise ProvisionError(
            "puffo_core.device_id != device_cert.device_id",
            puffo_core=core["device_id"],
            device_cert=device_from_cert,
        )
    return core


def _validated_profile(payload: dict, agent_id: str) -> dict:
    role = (payload.get("role") or "").strip()
    role_short_raw = payload.get("role_short")
    if role_short_raw is not None and not isinstance(role_short_raw, str):
        raise ProvisionError("role_short must be a string")
    if role and len(role) > MAX_ROLE_LEN:
        raise ProvisionError(f"role must be at most {MAX_ROLE_LEN} characters")
    if isinstance(role_short_raw, str) and len(role_short_raw) > MAX_ROLE_SHORT_LEN:
        raise ProvisionError(
            f"role_short must be at most {MAX_ROLE_SHORT_LEN} characters"
        )
    if not role and role_short_raw:
        raise ProvisionError("role_short cannot be set without role")
    profile_text = payload.get("profile")
    if not isinstance(profile_text, str) or not profile_text.strip():
        raise ProvisionError("profile (markdown body) is required")
    return {
        "display_name": (payload.get("display_name") or agent_id).strip() or agent_id,
        "avatar_url": (payload.get("avatar_url") or "").strip(),
        "role": role,
        "role_short": role_short_raw.strip()
        if isinstance(role_short_raw, str)
        else _derive_role_short(role)
        if role
        else "",
        "profile_text": profile_text,
    }


def _validated_runtime(raw: dict) -> RuntimeConfig:
    runtime = RuntimeConfig(
        kind=str(raw.get("kind", "cli-local")),
        provider=str(raw.get("provider", "")),
        model=str(raw.get("model", "")),
        api_key=str(raw.get("api_key", "")),
        harness=str(raw.get("harness", "claude-code")),
        permission_mode=str(raw.get("permission_mode", "bypassPermissions")),
    )
    from ..runtime_matrix import validate_triple

    validation = validate_triple(runtime.kind, runtime.provider, runtime.harness)
    if not validation.ok:
        raise ProvisionError(f"runtime: {validation.error}")
    return runtime


def _validated_desired_templates(payload: dict) -> tuple[list[str], list[str]]:
    skills, mcps = (
        payload.get("desired_skills") or [],
        payload.get("desired_mcps") or [],
    )
    if not isinstance(skills, list) or not all(
        isinstance(item, str) and item for item in skills
    ):
        raise ProvisionError(
            "desired_skills must be a list of non-empty template-id strings"
        )
    if not isinstance(mcps, list) or not all(
        isinstance(item, str) and item for item in mcps
    ):
        raise ProvisionError(
            "desired_mcps must be a list of non-empty template-id strings"
        )
    return list(skills), list(mcps)


verify_agent_bundle = _verify_agent_bundle
