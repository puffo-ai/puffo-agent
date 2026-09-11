"""Pi native credential readiness checks.

The model catalog is discovery data, not proof that the selected provider can
authenticate.  Keep this check on Pi's own auth resolver so OAuth refresh and
provider-specific credential formats stay Pi's responsibility.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .harness.support.child_env import build_child_environment


class PiAuthProbeError(RuntimeError):
    """Pi could not produce a trustworthy native auth verdict."""


@dataclass(frozen=True)
class PiAuthResult:
    status: str
    provider: str
    reason: str = ""


_STATUS_EXIT_CODES = {"ready": 0, "not_ready": 1}


def pi_auth_target(provider: str, model: str) -> tuple[str, str]:
    """Return the native provider/model selector for one runtime choice.

    A provider-qualified model is authoritative in Pi.  Passing a conflicting
    ``--provider`` alongside it can check a different credential than launch
    will use, so qualified models deliberately omit the runtime provider.
    """
    model = model.strip()
    if "/" in model:
        return "", model
    return provider.strip(), model


def check_pi_auth(
    executable: str,
    *,
    provider: str = "",
    model: str = "",
    config_dir: Path,
    timeout_seconds: float = 3.0,
) -> PiAuthResult:
    """Run Pi's side-effect-free native auth check and validate its verdict."""
    if not provider and not model:
        raise PiAuthProbeError("Pi auth check requires a provider or model")

    command = [executable, "auth", "check"]
    if provider:
        command.extend(("--provider", provider))
    if model:
        command.extend(("--model", model))
    command.extend(("--json", "--no-refresh"))
    env = build_child_environment(
        controlled={"PI_CODING_AGENT_DIR": str(config_dir)}
    )
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PiAuthProbeError("Pi auth check could not complete") from exc

    try:
        payload = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise PiAuthProbeError("Pi auth check returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PiAuthProbeError("Pi auth check returned a non-object result")

    status = payload.get("status")
    if not isinstance(status, str) or status not in _STATUS_EXIT_CODES:
        raise PiAuthProbeError("Pi auth check returned an unknown status")
    if completed.returncode != _STATUS_EXIT_CODES[status]:
        raise PiAuthProbeError("Pi auth check status disagrees with its exit code")

    result_provider = payload.get("provider", provider)
    reason = payload.get("reason", "")
    if not isinstance(result_provider, str) or not isinstance(reason, str):
        raise PiAuthProbeError("Pi auth check returned invalid fields")
    return PiAuthResult(status=status, provider=result_provider, reason=reason)


def pi_has_credentials(executable: str, *, home: Path | None = None) -> bool:
    """Whether any provider configured in the host Pi auth file is ready.

    Provider names are the only part read directly.  Credential validity is
    always decided by ``pi auth check``; secrets are neither parsed nor logged.
    """
    root = Path.home() if home is None else home
    config_dir = root / ".pi" / "agent"
    auth_path = config_dir / "auth.json"
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        raise PiAuthProbeError("Pi auth file could not be read") from exc
    if not isinstance(payload, dict):
        raise PiAuthProbeError("Pi auth file must contain an object")
    providers = sorted(key for key in payload if isinstance(key, str) and key)
    if not providers:
        return False
    for provider in providers:
        result = check_pi_auth(
            executable,
            provider=provider,
            config_dir=config_dir,
        )
        if result.status == "ready":
            return True
        if result.status != "not_ready":
            raise PiAuthProbeError("Pi auth configuration is invalid")
    return False


def list_pi_models(
    executable: str,
    *,
    config_dir: Path,
    timeout_seconds: float = 5.0,
) -> tuple[tuple[str, str, bool], ...]:
    """Return model ids, labels, and thinking support from Pi's native list."""
    env = build_child_environment(
        controlled={"PI_CODING_AGENT_DIR": str(config_dir)}
    )
    try:
        completed = subprocess.run(
            [executable, "--list-models"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PiAuthProbeError("Pi model list could not complete") from exc
    if completed.returncode != 0:
        raise PiAuthProbeError("Pi model list failed")

    rows = [line.split() for line in completed.stdout.splitlines() if line.strip()]
    if not rows:
        return ()
    header = [column.lower() for column in rows[0]]
    try:
        provider_index = header.index("provider")
        model_index = header.index("model")
        thinking_index = header.index("thinking")
    except ValueError:
        return ()

    models: list[tuple[str, str, bool]] = []
    thinking_offset = len(header) - thinking_index
    required_index = max(provider_index, model_index)
    for columns in rows[1:]:
        if len(columns) <= required_index or len(columns) < thinking_offset:
            continue
        provider, model = columns[provider_index], columns[model_index]
        thinking = columns[-thinking_offset]
        model_id = f"{provider}/{model}"
        models.append((
            model_id,
            f"{model} ({provider})",
            thinking.lower() == "yes",
        ))
    return tuple(models)
