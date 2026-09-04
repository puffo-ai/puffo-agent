"""OpenCode native model-access readiness checks.

OpenCode has no standalone ``auth check`` command.  Its native ``models``
command is nevertheless the right authority: it lists public models plus the
providers unlocked by the current credential store, and a provider-filtered
query fails when that provider is not configured.  Reuse that output for both
the picker and create-time preflight so discovery cannot claim more than the
runtime can actually launch.
"""

from __future__ import annotations

import re
import subprocess
from typing import Literal

from .harness.support.child_env import build_child_environment


class OpenCodeProbeError(RuntimeError):
    """OpenCode could not produce a trustworthy model-access verdict."""


OpenCodeModelStatus = Literal[
    "ready", "need_login", "model_not_available",
]


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def list_opencode_models(
    executable: str,
    *,
    provider: str = "",
    timeout_seconds: float = 5.0,
) -> tuple[str, ...]:
    """Return model IDs visible to OpenCode's current native credential view."""
    command = [executable, "models"]
    if provider:
        command.append(provider)
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

    diagnostic = _ANSI_ESCAPE.sub("", completed.stdout + completed.stderr)
    if completed.returncode != 0:
        if provider and "Provider not found:" in diagnostic:
            return ()
        raise OpenCodeProbeError("OpenCode model check failed")

    models: list[str] = []
    seen: set[str] = set()
    for line in completed.stdout.splitlines():
        model = line.strip()
        if "/" not in model or any(char.isspace() for char in model):
            continue
        if model not in seen:
            seen.add(model)
            models.append(model)
    return tuple(models)


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
