"""Move a LingTai binding out of Puffo's pre-attach registry.

Agents imported before attach existed were provisioned into
``<puffo home>/lingtai/runtime-registry.json`` and their harness argv names
that file. A running LingTai Agent only accepts an attach whose registry is
its own (``LINGTAI_PUFFO_V0_REGISTRY`` or ``~/.lingtai/puffo-v0/...``) and
answers anything else with ``runtime_registry_mismatch``. So such an agent
could be started by Puffo but never attached to the Agent the user has open.

The move keeps the runtime id: LingTai provisions the same id, directory and
workspace into the resident's registry, then the argv is pointed there. The
old entry is left as it was; Puffo never revokes a live binding, and leaving
it means an older argv that still names it keeps starting.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..state import AgentConfig, home_dir
from .lingtai import _last_option, _run, lingtai_registry_path
from .lingtai_discovery import _query

logger = logging.getLogger(__name__)

_MAX_REGISTRY_BYTES = 1024 * 1024


def legacy_registry_path() -> Path:
    return home_dir().resolve() / "lingtai" / "runtime-registry.json"


def names_legacy_registry(harness_command: list[str]) -> bool:
    registry = _last_option(tuple(harness_command), "--registry")
    if not registry or not Path(registry).is_absolute():
        return False
    try:
        return Path(registry).resolve() == legacy_registry_path() != lingtai_registry_path()
    except (OSError, RuntimeError, ValueError):
        return False


async def move_to_resident_registry(agent_id: str, harness_command: list[str]) -> list[str]:
    """Return the argv to use: moved to the resident registry when it named
    the legacy one, otherwise unchanged. Raises ValueError when the move
    cannot be made safely; the caller keeps the old argv."""
    if not names_legacy_registry(harness_command):
        return harness_command
    command = tuple(harness_command)
    runtime_id = _last_option(command, "--runtime-id")
    if not runtime_id:
        raise ValueError("LingTai harness command has no --runtime-id")
    executable = Path(command[0])
    legacy, resident = legacy_registry_path(), lingtai_registry_path()
    agent_dir = _registered_agent_dir(legacy, runtime_id)
    if agent_dir is None:
        raise ValueError(f"legacy LingTai registry has no agent directory for {runtime_id}")

    # LingTai, not this file, decides whether the old binding is live: a
    # revoked or tampered entry must not be copied into a registry that would
    # make it usable again.
    row = _row_for(await _query(str(executable), agent_dir, legacy), agent_dir)
    if row is None or row.get("status") != "bound" or row.get("runtime_id") != runtime_id:
        raise ValueError(f"legacy LingTai binding {runtime_id} is not active")
    workspace = row.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError(f"legacy LingTai binding {runtime_id} has no workspace")

    # Already moved by an earlier start that stopped before saving the argv.
    # Anything else goes to provision, which refuses every conflict itself.
    placed = _row_for(await _query(str(executable), agent_dir, resident), agent_dir)
    if placed and placed.get("status") == "bound" and placed.get("runtime_id") == runtime_id:
        placed_workspace = placed.get("workspace")
        if (not isinstance(placed_workspace, str)
                or not Path(placed_workspace).is_absolute()
                or Path(placed_workspace).resolve() != Path(workspace).resolve()):
            raise ValueError(f"resident LingTai binding {runtime_id} has a different workspace")
    else:
        await _run(executable, Path(workspace), [
            "puffo-v0", "provision", "--runtime-id", runtime_id,
            "--agent-dir", str(agent_dir), "--workspace", workspace,
            "--registry", str(resident),
        ])

    moved = _with_registry(harness_command, resident)
    current = AgentConfig.load(agent_id)
    # Another writer changed the argv since this start read it; leave theirs.
    if current.runtime.harness_command != list(harness_command):
        return harness_command
    current.runtime.harness_command = moved
    current.save()
    logger.info("agent %s: moved LingTai runtime %s to %s", agent_id, runtime_id, resident)
    return moved


def _with_registry(harness_command: list[str], registry: Path) -> list[str]:
    # Rewrite every occurrence so the argparse last-wins value is the new one.
    result: list[str] = []
    replace_next = False
    for arg in harness_command:
        if replace_next:
            result.append(str(registry))
            replace_next = False
        elif arg == "--registry":
            result.append(arg)
            replace_next = True
        elif arg.startswith("--registry="):
            result.append(f"--registry={registry}")
        else:
            result.append(arg)
    return result


def _row_for(rows: list[dict], agent_dir: Path) -> dict | None:
    for row in rows:
        directory = row.get("agent_dir") if isinstance(row, dict) else None
        if isinstance(directory, str) and Path(directory).resolve() == agent_dir.resolve():
            return row
    return None


def _registered_agent_dir(registry: Path, runtime_id: str) -> Path | None:
    try:
        with registry.open("rb") as stream:
            data = stream.read(_MAX_REGISTRY_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"LingTai registry is unreadable: {exc.strerror}") from None
    if len(data) > _MAX_REGISTRY_BYTES:
        raise ValueError("LingTai registry exceeds its size limit")
    try:
        entry = json.loads(data)["runtimes"].get(runtime_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("LingTai registry is invalid") from None
    directory = entry.get("agent_dir") if isinstance(entry, dict) else None
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        return None
    return Path(directory)
