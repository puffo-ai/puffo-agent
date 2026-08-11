"""Verify and materialize agent bundles received over the control plane."""

from __future__ import annotations

import json
import logging
import shutil
import time
from typing import Any

from ...crypto.canonical import canonicalize_for_signing
from ...crypto.encoding import base64url_decode
from ...crypto.primitives import ed25519_verify
from ...mcp.config import supported_inference_levels
from ..host_assets import _atomic_write_private, _ensure_private_directory
from ..runtime_matrix import (
    RUNTIME_CLI_LOCAL,
    migrate_legacy_kind,
    normalize_inference_level,
    resolve_effective_harness,
    resolve_effective_provider,
    validate_triple,
)
from ..state import (
    AgentConfig,
    PuffoCoreConfig,
    RuntimeConfig,
    TriggerRules,
    agent_dir,
    agent_yml_path,
    derive_role_short,
    is_valid_agent_id,
)
from .certs import CertError, verify_device_cert, verify_identity_cert, verify_slug_binding

logger = logging.getLogger(__name__)

MAX_ROLE_LEN = 140
MAX_ROLE_SHORT_LEN = 32


class ProvisionError(Exception):
    def __init__(self, reason: str, **fields: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fields = fields


def _verify_attestation(attestation: dict, agent_key: bytes, operator_key: bytes) -> None:
    if not isinstance(attestation, dict):
        raise CertError("operator_attestation must be an object")
    if attestation.get("type") != "operator_attestation":
        raise CertError(f"unexpected attestation type {attestation.get('type')!r}")
    try:
        declared_operator = base64url_decode(attestation["operator_root_public_key"])
        declared_agent = base64url_decode(attestation["agent_root_public_key"])
        signature = base64url_decode(attestation["signature"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CertError(f"attestation field decode: {exc}") from exc
    if declared_operator != operator_key:
        raise CertError("attestation.operator_root_public_key != paired user")
    if declared_agent != agent_key:
        raise CertError("attestation.agent_root_public_key != agent identity_cert")
    unsigned = {key: value for key, value in attestation.items() if key != "signature"}
    if not ed25519_verify(operator_key, canonicalize_for_signing(unsigned), signature):
        raise CertError("attestation signature verification failed")


def verify_agent_bundle(payload: dict, operator_root_key_b64: str) -> dict:
    try:
        operator_key = base64url_decode(operator_root_key_b64)
    except Exception as exc:
        raise ProvisionError(f"paired root pubkey decode: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProvisionError("body must be a JSON object")
    bundle = payload.get("identity_bundle")
    core = payload.get("puffo_core")
    runtime_input = payload.get("runtime")
    if not all(isinstance(item, dict) for item in (bundle, core, runtime_input)):
        raise ProvisionError("identity_bundle, puffo_core, and runtime are required")

    bound_slug, device_cert = _verify_identity_bundle(
        bundle,
        operator_key,
        operator_root_key_b64,
    )
    core_fields = _verify_core(core, bound_slug, device_cert)
    profile_fields = _verify_profile(payload, bound_slug)
    runtime = _verify_runtime(runtime_input)
    desired_skills, desired_mcps = _verify_desired(payload)
    server_url, slug, device_id, space_id, operator_slug = core_fields
    display_name, avatar_url, role, role_short, profile_text = profile_fields
    return {
        "agent_id": slug,
        "display_name": display_name,
        "avatar_url": avatar_url,
        "role": role,
        "role_short": role_short,
        "profile_text": profile_text,
        "server_url": server_url,
        "slug": slug,
        "device_id": device_id,
        "space_id": space_id,
        "operator_slug": operator_slug,
        "runtime": runtime,
        "desired_skills": desired_skills,
        "desired_mcps": desired_mcps,
        "bundle": bundle,
    }


def _verify_identity_bundle(
    bundle: dict,
    operator_key: bytes,
    operator_root_key_b64: str,
) -> tuple[str, dict]:
    """Validate the signed identity chain and return its bound slug/device."""

    identity_cert = bundle.get("identity_cert")
    device_cert = bundle.get("device_cert")
    attestation = bundle.get("operator_attestation")
    slug_binding = bundle.get("slug_binding")
    if not all(isinstance(item, dict) for item in (
        identity_cert, device_cert, attestation, slug_binding,
    )):
        raise ProvisionError(
            "identity_bundle missing one of identity_cert/device_cert/"
            "operator_attestation/slug_binding"
        )
    try:
        agent_key = verify_identity_cert(identity_cert)
    except CertError as exc:
        raise ProvisionError(f"identity_cert: {exc}") from exc
    if identity_cert.get("identity_type") != "agent":
        raise ProvisionError("identity_cert.identity_type must be 'agent'")
    declared_operator = identity_cert.get("declared_operator_public_key")
    if not isinstance(declared_operator, str) or not declared_operator:
        raise ProvisionError(
            "identity_cert.declared_operator_public_key required for agent identity"
        )
    if declared_operator != operator_root_key_b64:
        raise ProvisionError(
            "identity_cert.declared_operator_public_key does not match paired operator"
        )
    try:
        verify_device_cert(device_cert, agent_key)
        bound_slug = verify_slug_binding(slug_binding, agent_key)
        _verify_attestation(attestation, agent_key, operator_key)
    except CertError as exc:
        raise ProvisionError(str(exc)) from exc
    return bound_slug, device_cert


def _verify_core(
    core: dict,
    bound_slug: str,
    device_cert: dict,
) -> tuple[str, str, str, str, str]:
    """Validate the server addressing block against the signed identity."""

    server_url = str(core.get("server_url") or "").strip()
    slug = str(core.get("slug") or "").strip()
    device_id = str(core.get("device_id") or "").strip()
    space_id = str(core.get("space_id") or "").strip()
    operator_slug = str(core.get("operator_slug") or "").strip()
    if not all((server_url, slug, device_id, space_id)):
        raise ProvisionError(
            "puffo_core block must include server_url, slug, device_id, space_id"
        )
    if slug != bound_slug:
        raise ProvisionError("puffo_core.slug != slug_binding.slug")
    if device_id != device_cert.get("device_id"):
        raise ProvisionError("puffo_core.device_id != device_cert.device_id")
    if not is_valid_agent_id(slug):
        raise ProvisionError(f"slug {slug!r} is not a valid agent id")
    return server_url, slug, device_id, space_id, operator_slug


def _verify_profile(payload: dict, slug: str) -> tuple[str, str, str, str, str]:
    """Validate user-visible profile fields and derive the role chip."""

    display_name = str(payload.get("display_name") or slug).strip() or slug
    avatar_url = str(payload.get("avatar_url") or "").strip()
    role = str(payload.get("role") or "").strip()
    role_short_input = payload.get("role_short")
    if role_short_input is not None and not isinstance(role_short_input, str):
        raise ProvisionError("role_short must be a string")
    if len(role) > MAX_ROLE_LEN:
        raise ProvisionError(f"role must be at most {MAX_ROLE_LEN} characters")
    if isinstance(role_short_input, str) and len(role_short_input) > MAX_ROLE_SHORT_LEN:
        raise ProvisionError(f"role_short must be at most {MAX_ROLE_SHORT_LEN} characters")
    if not role and role_short_input:
        raise ProvisionError("role_short cannot be set without role")
    role_short = derive_role_short(role) if role else ""
    if (
        isinstance(role_short_input, str)
        and role_short_input.strip()
        and role_short_input.strip() != role_short
    ):
        logger.warning(
            "provision: ignoring deprecated role_short %r; derived %r for agent=%s",
            role_short_input,
            role_short,
            slug,
        )
    profile_text = payload.get("profile")
    if not isinstance(profile_text, str) or not profile_text.strip():
        raise ProvisionError("profile (markdown body) is required")
    return display_name, avatar_url, role, role_short, profile_text


def _verify_runtime(runtime_input: dict) -> RuntimeConfig:
    """Build a runtime config that is valid for its effective harness."""

    raw_kind = str(runtime_input.get("kind", RUNTIME_CLI_LOCAL))
    kind = migrate_legacy_kind(raw_kind, agent_id="control-provision")
    provider = str(runtime_input.get("provider", ""))
    harness = str(runtime_input.get("harness", "claude-code"))
    inference_level = str(runtime_input.get("inference_level", ""))
    if raw_kind != kind and not validate_triple(kind, provider, harness).ok:
        harness = resolve_effective_harness(kind, provider, "")
        inference_level = normalize_inference_level(
            kind, provider, harness, inference_level
        )
    runtime = RuntimeConfig(
        kind=kind,
        provider=provider,
        model=str(runtime_input.get("model", "")),
        api_key=str(runtime_input.get("api_key", "")),
        harness=harness,
        permission_mode=str(runtime_input.get("permission_mode", "bypassPermissions")),
        inference_level=inference_level,
        max_turns=int(runtime_input.get("max_turns", 10)),
    )
    validation = validate_triple(runtime.kind, runtime.provider, runtime.harness)
    if not validation.ok:
        raise ProvisionError(f"runtime: {validation.error}")
    effective_harness = resolve_effective_harness(
        runtime.kind,
        resolve_effective_provider(runtime.kind, runtime.provider),
        runtime.harness,
    )
    levels = supported_inference_levels(effective_harness)
    if runtime.inference_level and runtime.inference_level not in levels:
        raise ProvisionError(
            "runtime.inference_level must be one of: " + ", ".join(levels)
        )
    return runtime


def _verify_desired(payload: dict) -> tuple[list[str], list[str]]:
    """Validate operator-selected skill and MCP template identifiers."""
    desired_skills = payload.get("desired_skills") or []
    desired_mcps = payload.get("desired_mcps") or []
    if not isinstance(desired_skills, list) or not all(
        isinstance(value, str) and value for value in desired_skills
    ):
        raise ProvisionError("desired_skills must be a list of non-empty template-id strings")
    if not isinstance(desired_mcps, list) or not all(
        isinstance(value, str) and value for value in desired_mcps
    ):
        raise ProvisionError("desired_mcps must be a list of non-empty template-id strings")
    return list(desired_skills), list(desired_mcps)


def _write_keystore(context: dict) -> None:
    bundle = context["bundle"]
    target = agent_dir(context["agent_id"]) / "keys" / f"{context['slug']}.json"
    _ensure_private_directory(target.parent)
    stored = {
        "slug": context["slug"],
        "device_id": context["device_id"],
        "root_secret_key": bundle["root_secret_key"],
        "device_signing_secret_key": bundle["device_signing_secret_key"],
        "kem_secret_key": bundle["kem_secret_key"],
        "server_url": context["server_url"],
        "identity_cert_json": json.dumps(bundle["identity_cert"]),
        "slug_binding_json": json.dumps(bundle["slug_binding"]),
    }
    _atomic_write_private(target, json.dumps(stored, indent=2))


def write_agent_from_context(context: dict) -> dict:
    agent_id = context["agent_id"]
    if agent_yml_path(agent_id).exists():
        raise ProvisionError(f"agent {agent_id!r} already exists on this daemon")
    target = agent_dir(agent_id)
    if target.exists():
        raise ProvisionError(f"agent directory {target} already exists")
    try:
        _ensure_private_directory(target.parent)
        target.mkdir(mode=0o700, exist_ok=False)
        _ensure_private_directory(target)
        AgentConfig(
            id=agent_id,
            state="running",
            display_name=context["display_name"],
            avatar_url=context["avatar_url"],
            role=context["role"],
            role_short=context["role_short"],
            puffo_core=PuffoCoreConfig(
                server_url=context["server_url"],
                slug=context["slug"],
                device_id=context["device_id"],
                space_id=context["space_id"],
                operator_slug=context["operator_slug"],
            ),
            runtime=context["runtime"],
            triggers=TriggerRules(),
            desired_skills=context["desired_skills"],
            desired_mcps=context["desired_mcps"],
            created_at=int(time.time()),
        ).save()
        (target / "memory").mkdir(exist_ok=True)
        (target / "profile.md").write_text(context["profile_text"], encoding="utf-8")
        _write_keystore(context)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"agent_id": agent_id, "agent_dir": str(target)}


async def provision_agent_from_bundle(
    payload: dict, operator_root_key_b64: str, *, materialize=None,
) -> dict:
    context = verify_agent_bundle(payload, operator_root_key_b64)
    if materialize is not None:
        await materialize(context)
    result = write_agent_from_context(context)
    result.update(
        device_id=context["device_id"],
        role=context["role"],
        role_short=context["role_short"],
        runtime=context["runtime"],
    )
    return result
