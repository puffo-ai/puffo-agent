"""Read-only LingTai discovery for an authenticated machine operator."""

from __future__ import annotations

from .lingtai_profile import read_source_profile

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

from ..state import AgentConfig, discover_agents, home_dir
from .ownership import is_owner

_MAX_OUTPUT = 1024 * 1024
_MAX_ROOTS = 16


def _absolute(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise ValueError(f"LingTai {field} must be an absolute path on this machine")
    return Path(value).resolve(strict=True)


def _known_paths(operator: str) -> tuple[list[Path], list[Path], list[str]]:
    executables: list[Path] = []
    roots = [Path.home() / ".lingtai"]
    warnings: list[str] = []
    registry = _registry_entries()
    # Reuse only this operator's existing associations; do not expose another
    # paired operator's agent configuration through the automatic inventory.
    for agent_id in discover_agents():
        if not is_owner(agent_id, operator):
            continue
        try:
            cfg = AgentConfig.load(agent_id, allow_invalid_runtime=True)
        except (OSError, ValueError, RuntimeError):
            warnings.append("invalid_candidate")
            continue
        argv = cfg.runtime.harness_command
        if len(argv) < 4 or argv[1:4] != ["acp", "--profile", "puffo-v1"]:
            continue
        executables.append(Path(argv[0]))
        workspace = cfg.resolve_workspace_dir()
        roots.extend([workspace, workspace / ".lingtai"])
        if "--runtime-id" in argv:
            index = argv.index("--runtime-id") + 1
            entry = registry.get(argv[index]) if index < len(argv) else None
            if isinstance(entry, dict) and isinstance(entry.get("agent_dir"), str):
                directory = Path(entry["agent_dir"])
                if directory.is_absolute():
                    roots.append(directory)
    return executables, roots, warnings



def _registry_entries() -> dict:
    # Location hints only; LingTai's discover remains the authority on state.
    path = home_dir().resolve() / "lingtai" / "runtime-registry.json"
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_OUTPUT + 1)
        if len(data) > _MAX_OUTPUT:
            return {}
        payload = json.loads(data)
        entries = payload.get("runtimes") if isinstance(payload, dict) else None
        return entries if isinstance(entries, dict) else {}
    except (OSError, ValueError):
        return {}


def _executable_paths(known: list[Path]) -> list[str]:
    found = shutil.which("lingtai-agent")
    candidates = ([Path(found)] if found else []) + known + [
        Path(sys.executable).parent / "lingtai-agent",
        Path.home() / ".local" / "bin" / "lingtai-agent",
        Path("/opt/homebrew/bin/lingtai-agent"),
        Path("/usr/local/bin/lingtai-agent"),
    ]
    result = []
    for candidate in candidates:
        try:
            # Keep the executable symlink spelling: virtualenv launchers may
            # derive their environment from it.
            path = str(candidate.absolute())
            if candidate.is_file() and os.access(candidate, os.X_OK) and path not in result:
                result.append(path)
        except OSError:
            continue
    return result


def _search_executable_folder(value: object) -> dict:
    root = _absolute(value, "executable_root")
    if not root.is_dir():
        raise ValueError("LingTai executable search location must be a directory")
    result = {"ok": True, "executables": [], "executable": None, "agents": [],
              "roots": [], "warnings": [], "searched_executable_root": str(root)}
    deadline = time.monotonic() + 3
    pending = [(root, 0)]
    visited = 0
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 10000 or time.monotonic() >= deadline:
                        result["warnings"].append("results_truncated")
                        return _bounded_result(result)
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in {".git", "node_modules", "__pycache__"}:
                            continue
                        if depth < 8:
                            pending.append((Path(entry.path), depth + 1))
                        else:
                            result["warnings"].append("results_truncated")
                    elif entry.name in {"lingtai-agent", "lingtai-agent.exe"}:
                        if entry.is_file() and os.access(entry.path, os.X_OK):
                            result["executables"].append(entry.path)
                            if len(result["executables"]) >= 8:
                                result["warnings"].append("results_truncated")
                                return _bounded_result(result)
        except OSError:
            result["warnings"].append("search_unreadable")
    return _bounded_result(result)


async def _query(executable: str, root: Path, registry: Path) -> list[dict]:
    process = await asyncio.create_subprocess_exec(
        executable, "puffo-v0", "discover", "--root", str(root),
        "--registry", str(registry), "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        limit=_MAX_OUTPUT + 1,
    )
    try:
        async with asyncio.timeout(10):
            assert process.stdout is not None
            output = await process.stdout.readexactly(_MAX_OUTPUT + 1)
            raise ValueError("LingTai discovery result is too large")
    except asyncio.IncompleteReadError as exc:
        output = exc.partial
        # EOF can precede process exit; bound this wait separately too.
        await asyncio.wait_for(process.wait(), timeout=2)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    if process.returncode:
        raise ValueError("LingTai discovery failed; check the installation and search folder")
    payload = json.loads(output)
    rows = payload.get("runtimes") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("LingTai discovery returned an invalid result")
    return rows


def _normalize(row: object, root: Path) -> dict:
    if not isinstance(row, dict):
        raise ValueError("LingTai discovery returned an invalid agent")
    directory = _absolute(row.get("agent_dir"), "agent_dir")
    if not directory.is_relative_to(root) or not (directory / "init.json").is_file():
        raise ValueError("LingTai discovery returned an agent outside the search folder")
    status = row.get("status")
    if not isinstance(status, str):
        raise ValueError("LingTai discovery returned an invalid agent state")
    workspace = row.get("workspace")
    if workspace is None:
        workspace = str(directory)
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("LingTai discovery returned an invalid working folder")
    workspace_path = Path(workspace)
    # Keep drifted registrations visible even when their workspace is missing.
    if status == "available" and not workspace_path.is_dir():
        raise ValueError("LingTai discovery returned a missing working folder")
    source = read_source_profile(directory)
    return {
        "source_dir_name": directory.name, "agent_dir": str(directory),
        "agent_name": source.agent_name, "name_source_file": source.name_source_file,
        "profile_read_error": source.profile_read_error,
        "import_display_name": source.import_display_name,
        "description": None, "profile_source": "lingtai",
        "workspace": str(workspace_path), "status": status,
        "runtime_id": row.get("runtime_id"),
        "formerly_bound_runtime_id": row.get("formerly_bound_runtime_id"),
    }


async def discover_lingtai(params: dict, *, operator: str | None) -> dict:
    if not operator:
        return {"ok": False, "error": "LingTai discovery requires a paired operator"}
    try:
        return await _discover(params, operator)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def _discover(params: dict, operator: str) -> dict:
    if "executable_root" in params:
        return await asyncio.to_thread(_search_executable_folder, params["executable_root"])
    known, default_roots, warnings = await asyncio.to_thread(_known_paths, operator)
    executables = await asyncio.to_thread(_executable_paths, known)
    if params.get("executable"):
        explicit = _absolute(params["executable"], "executable")
        if not explicit.is_file() or not os.access(explicit, os.X_OK):
            raise ValueError("LingTai executable must be an executable file")
        executable = str(explicit)
        executables = list(dict.fromkeys([executable, *executables]))
    else:
        executable = executables[0] if executables else None
    roots = default_roots
    if params.get("root"):
        selected = _absolute(params["root"], "root")
        roots = [selected, selected / ".lingtai"]
    roots = list(dict.fromkeys(path.resolve() for path in roots if path.is_dir()))
    if len(roots) > _MAX_ROOTS or len(executables) > 8:
        warnings.append("results_truncated")
    roots, executables = roots[:_MAX_ROOTS], executables[:8]
    result = {
        "ok": True, "executables": executables, "executable": executable,
        "roots": [str(root) for root in roots], "agents": [], "warnings": warnings,
    }
    if not executable:
        return _bounded_result(result)
    registry = home_dir().resolve() / "lingtai" / "runtime-registry.json"
    agents = {}
    # A single request has a bounded total budget even with many known roots.
    try:
        async with asyncio.timeout(25):
            for root in roots:
                try:
                    rows = await _query(executable, root, registry)
                    for row in rows:
                        try:
                            agent = _normalize(row, root)
                            agents[agent["agent_dir"]] = agent
                        except (OSError, ValueError):
                            result["warnings"].append("invalid_candidate")
                except (OSError, ValueError, TimeoutError):
                    result["warnings"].append("discovery_failed")
    except TimeoutError:
        result["warnings"].append("discovery_timeout")
    result["agents"] = list(agents.values())
    return _bounded_result(result)


def _bounded_result(result: dict) -> dict:
    # The server persists at most 16 KiB per command result. Stay below that
    # budget so a large inventory is partial rather than replaced by a marker.
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    result["partial"] = bool(result["warnings"])
    result["truncated"] = "results_truncated" in result["warnings"]
    for field in ("agents", "roots", "executables"):
        while len(json.dumps(result).encode()) > 12 * 1024 and result[field]:
            result[field].pop()
            result["partial"] = True
            result["truncated"] = True
            if "results_truncated" not in result["warnings"]:
                result["warnings"].append("results_truncated")
    return result
