"""Validation and serialization for bridge runtime edits."""

from __future__ import annotations

from typing import Any

from ..runtime_matrix import normalize_inference_level, validate_triple
from ..state import RuntimeConfig

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")


def apply_runtime_patch(runtime: RuntimeConfig, payload: dict[str, Any]) -> str | None:
    """Apply the existing bridge-editable fields and validate the result."""
    for field in (
        "kind",
        "provider",
        "harness",
        "model",
        "api_key",
        "permission_mode",
        "docker_image",
    ):
        if field in payload:
            setattr(runtime, field, str(payload[field]))

    if "sandbox" in payload:
        sandbox = str(payload["sandbox"])
        if sandbox not in SANDBOX_MODES:
            return "sandbox must be one of: " + ", ".join(SANDBOX_MODES)
        runtime.sandbox = sandbox

    error = _apply_worker_limits(runtime, payload)
    if error is not None:
        return error

    # A harness/provider swap can strip the level the old harness supported;
    # normalizing here keeps the saved agent.yml loadable.
    runtime.inference_level = normalize_inference_level(
        runtime.kind, runtime.provider, runtime.harness, runtime.inference_level
    )

    result = validate_triple(runtime.kind, runtime.provider, runtime.harness)
    return None if result.ok else f"runtime: {result.error}"


def _apply_worker_limits(
    runtime: RuntimeConfig, payload: dict[str, Any]
) -> str | None:
    """Apply ``allowed_tools`` / ``max_turns`` with the base API's typing."""
    if "allowed_tools" in payload:
        tools = payload["allowed_tools"]
        if not isinstance(tools, list):
            return "allowed_tools must be a list of strings"
        runtime.allowed_tools = [str(t) for t in tools]

    if "max_turns" in payload:
        try:
            runtime.max_turns = int(payload["max_turns"])
        except (TypeError, ValueError):
            return "max_turns must be an integer"

    return None


def runtime_response(runtime: RuntimeConfig) -> dict[str, Any]:
    return {
        "kind": runtime.kind,
        "provider": runtime.provider,
        "model": runtime.model,
        "api_key_set": bool(runtime.api_key),
        "permission_mode": runtime.permission_mode,
        "sandbox": runtime.sandbox,
        "harness": runtime.harness,
        "allowed_tools": list(runtime.allowed_tools),
        "docker_image": runtime.docker_image,
        "max_turns": runtime.max_turns,
    }
