"""Source-owned LingTai profile metadata and portal edit policy."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from dataclasses import dataclass

from ..state import AgentConfig, RuntimeConfig

_MAX_INIT_BYTES = 1024 * 1024
_PROFILE_FIELDS = frozenset({"display_name", "role", "role_short", "soul", "profile"})


@dataclass(frozen=True)
class SourceProfile:
    agent_name: str | None
    profile_name_source: str | None
    profile_read_error: str | None
    import_display_name: str | None


def _read_source_object(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("source metadata must be a regular file")
        data = stream.read(_MAX_INIT_BYTES + 1)
    if len(data) > _MAX_INIT_BYTES:
        raise ValueError("source metadata exceeds the size limit")
    document = json.loads(data)
    if not isinstance(document, dict):
        raise ValueError("source metadata must be an object")
    return document


def read_source_profile(directory: Path) -> SourceProfile:
    """Only an absent .agent.json allows the legacy init manifest fallback."""
    source = ".agent.json"
    try:
        try:
            metadata = _read_source_object(directory / source)
        except FileNotFoundError:
            try:
                (directory / ".agent.json.corrupt").lstat()
            except FileNotFoundError:
                pass
            else:
                raise ValueError("source metadata was quarantined as corrupt")
            source = "init.json"
            document = _read_source_object(directory / source)
            metadata = document.get("manifest", {})
            if not isinstance(metadata, dict):
                raise ValueError("manifest must be an object")
        name = metadata.get("agent_name")
        if name is not None and not isinstance(name, str):
            raise ValueError("agent_name must be a string or null")
        if isinstance(name, str):
            if len(name) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in name):
                raise ValueError("agent_name is not a safe display name")
            if not name.strip():
                name = None
        return SourceProfile(name, source, None, name or "Unnamed Agent")
    except FileNotFoundError:
        return SourceProfile(None, None, "source_unreadable", None)
    except (OSError, ValueError, RecursionError):
        return SourceProfile(None, source, "source_unreadable", None)


def is_lingtai_runtime(runtime: RuntimeConfig) -> bool:
    from ...agent.harness.drivers.acp import _lingtai_constrained_profile

    return bool(_lingtai_constrained_profile(tuple(runtime.harness_command)))


def validate_import_profile(payload: dict, directory: Path) -> None:
    source = read_source_profile(directory)
    if source.profile_read_error:
        raise ValueError("LingTai source profile cannot be read; check its metadata files")
    name = source.import_display_name
    selected = payload["runtime"]["lingtai"]
    if "agent_name" not in selected or selected["agent_name"] != source.agent_name:
        raise ValueError("LingTai source name changed or does not match; refresh discovery")
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
