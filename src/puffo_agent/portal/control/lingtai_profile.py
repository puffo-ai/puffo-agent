"""Source-owned LingTai profile metadata and portal edit policy."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from ..state import AgentConfig, RuntimeConfig

_MAX_INIT_BYTES = 1024 * 1024
_PROFILE_FIELDS = frozenset({"display_name", "role", "role_short", "soul", "profile"})


def source_agent_name(directory: Path) -> str | None:
    """Read a bounded regular init file; directory labels are never identity."""
    try:
        fd = os.open(directory / "init.json", os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            data = stream.read(_MAX_INIT_BYTES + 1)
        if len(data) > _MAX_INIT_BYTES:
            return None
        document = json.loads(data)
        manifest = document.get("manifest") if isinstance(document, dict) else None
        name = manifest.get("agent_name") if isinstance(manifest, dict) else None
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            return None
        return name
    except (OSError, ValueError, RecursionError):
        return None


def is_lingtai_runtime(runtime: RuntimeConfig) -> bool:
    from ...agent.harness.drivers.acp import _lingtai_constrained_profile

    return bool(_lingtai_constrained_profile(tuple(runtime.harness_command)))


def validate_import_profile(payload: dict, directory: Path) -> None:
    name = source_agent_name(directory)
    if name is None:
        raise ValueError("LingTai source agent_name is unavailable; check init.json")
    if payload.get("display_name") != name:
        raise ValueError("LingTai source name changed or does not match; refresh discovery")
    if any(payload.get(key) not in (None, "") for key in ("role", "role_short", "soul")):
        raise ValueError("LingTai owns its role and description; persona overrides are not allowed")
    if payload.get("profile") != f"# {name}\n":
        raise ValueError("LingTai import requires the generated technical bridge profile")


def guarded_edit_params(cfg: AgentConfig, params: dict) -> dict:
    """Reject source edits before writes, removing accepted unchanged values."""
    if not is_lingtai_runtime(cfg.runtime):
        return params
    from ..profile_sync import extract_soul_body

    result = dict(params)
    protected = _PROFILE_FIELDS.intersection(params)
    profile = ""
    if protected.intersection({"soul", "profile"}):
        try:
            profile = cfg.resolve_profile_path().read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("LingTai profile cannot be verified for an unchanged edit") from exc
    current = {"display_name": cfg.display_name, "role": cfg.role,
               "role_short": cfg.role_short, "profile": profile,
               "soul": extract_soul_body(profile)}
    if any(params[key] != current[key] for key in protected):
        raise ValueError("LingTai owns this agent's profile; edit it in LingTai")
    for key in protected:
        result.pop(key)
    runtime = params.get("runtime")
    if isinstance(runtime, dict):
        current_runtime = {"kind": cfg.runtime.kind, "provider": cfg.runtime.provider,
                           "harness": cfg.runtime.harness,
                           "harness_command": cfg.runtime.harness_command,
                           "model": cfg.runtime.model,
                           "inference_level": cfg.runtime.inference_level}
        if any(key in runtime and runtime[key] != value for key, value in current_runtime.items()):
            raise ValueError("LingTai import runtime cannot be replaced through profile editing")
    return result
