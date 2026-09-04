"""OpenCode native model-access readiness checks.

OpenCode has no standalone ``auth check`` command.  Its native ``models``
command is nevertheless the right authority: it lists public models plus the
providers unlocked by the current credential store, and a provider-filtered
query fails when that provider is not configured.  Reuse that output for both
the picker and create-time preflight so discovery cannot claim more than the
runtime can actually launch.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Literal

from .harness.support.child_env import build_child_environment


class OpenCodeProbeError(RuntimeError):
    """OpenCode could not produce a trustworthy model-access verdict."""


OpenCodeModelStatus = Literal[
    "ready", "need_login", "model_not_available",
]


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True, slots=True)
class OpenCodeModel:
    """One model and the native variants advertised by OpenCode."""

    id: str
    variants: tuple[str, ...] = ()


def _run_opencode_models(
    executable: str,
    *,
    provider: str,
    verbose: bool,
    timeout_seconds: float,
) -> str:
    command = [executable, "models"]
    if provider:
        command.append(provider)
    if verbose:
        command.append("--verbose")
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=build_child_environment(),
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OpenCodeProbeError("OpenCode model check could not complete") from exc

    stdout = _ANSI_ESCAPE.sub("", completed.stdout)
    diagnostic = stdout + _ANSI_ESCAPE.sub("", completed.stderr)
    if completed.returncode != 0:
        if provider and "Provider not found:" in diagnostic:
            return ""
        raise OpenCodeProbeError("OpenCode model check failed")
    return stdout


def _is_model_id(value: str) -> bool:
    return (
        "/" in value
        and not any(char.isspace() or char in '{}[],"' for char in value)
    )


def _next_verbose_model_offset(output: str, offset: int) -> int:
    """Recover at the next bare model-id line after malformed metadata."""
    length = len(output)
    line_start = offset
    while line_start < length:
        line_end = output.find("\n", line_start)
        if line_end < 0:
            line_end = length
        raw_line = output[line_start:line_end]
        candidate = raw_line.strip()
        metadata_offset = line_end + 1
        while metadata_offset < length and output[metadata_offset].isspace():
            metadata_offset += 1
        if (
            raw_line == candidate
            and _is_model_id(candidate)
            and (metadata_offset == length or output[metadata_offset] == "{")
        ):
            return line_start
        line_start = line_end + 1
    return length


def _parse_verbose_models(output: str) -> tuple[OpenCodeModel, ...]:
    """Parse ``models --verbose``'s repeated ``id`` + JSON blocks.

    The model-id line is the launch authority. JSON is used only for optional
    variant metadata, so a malformed/missing block degrades to no variants
    without inventing support or dropping a runnable model.
    """
    decoder = json.JSONDecoder()
    models: list[OpenCodeModel] = []
    seen: set[str] = set()
    offset = 0
    length = len(output)
    while offset < length:
        line_end = output.find("\n", offset)
        if line_end < 0:
            line_end = length
        model_id = output[offset:line_end].strip()
        offset = line_end + 1
        if not _is_model_id(model_id) or model_id in seen:
            continue

        metadata: object = {}
        json_offset = offset
        while json_offset < length and output[json_offset].isspace():
            json_offset += 1
        if json_offset < length and output[json_offset] == "{":
            try:
                metadata, offset = decoder.raw_decode(output, json_offset)
            except json.JSONDecodeError:
                metadata = {}
                offset = _next_verbose_model_offset(output, offset)

        variants = metadata.get("variants") if isinstance(metadata, dict) else {}
        variant_names = tuple(
            key for key in variants
            if isinstance(key, str) and key
        ) if isinstance(variants, dict) else ()
        seen.add(model_id)
        models.append(OpenCodeModel(model_id, variant_names))
    return tuple(models)


def list_opencode_models(
    executable: str,
    *,
    provider: str = "",
    timeout_seconds: float = 5.0,
) -> tuple[str, ...]:
    """Return model IDs visible to OpenCode's current native credential view."""
    stdout = _run_opencode_models(
        executable,
        provider=provider,
        verbose=False,
        timeout_seconds=timeout_seconds,
    )
    models: list[str] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        model = line.strip()
        if not _is_model_id(model):
            continue
        if model not in seen:
            seen.add(model)
            models.append(model)
    return tuple(models)


def list_opencode_model_catalog(
    executable: str,
    *,
    provider: str = "",
    timeout_seconds: float = 5.0,
) -> tuple[OpenCodeModel, ...]:
    """Return visible models with their model-specific native variants."""
    try:
        stdout = _run_opencode_models(
            executable,
            provider=provider,
            verbose=True,
            timeout_seconds=timeout_seconds,
        )
    except OpenCodeProbeError:
        return tuple(
            OpenCodeModel(model_id)
            for model_id in list_opencode_models(
                executable,
                provider=provider,
                timeout_seconds=timeout_seconds,
            )
        )
    return _parse_verbose_models(stdout)


def opencode_model_status(executable: str, model: str) -> OpenCodeModelStatus:
    """Classify provider access separately from an unavailable model ID."""
    selected = model.strip()
    provider = selected.split("/", 1)[0] if "/" in selected else ""
    models = list_opencode_models(executable, provider=provider)
    if not models:
        return "need_login"
    if not selected:
        return "ready"
    if "/" in selected:
        available = selected in models
    else:
        available = any(
            candidate.rsplit("/", 1)[-1] == selected for candidate in models
        )
    return "ready" if available else "model_not_available"


def opencode_model_is_available(executable: str, model: str) -> bool:
    """Compatibility bool view of :func:`opencode_model_status`."""
    return opencode_model_status(executable, model) == "ready"
