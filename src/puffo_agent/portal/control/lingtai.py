"""Operator-selected LingTai folders at the machine creation boundary."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from ...agent.harness.support.cleanup_errors import collect_cleanup_errors, raise_collected_errors
from ...agent.harness.support.subprocess_io import (
    abandon_process_transport, process_group_spawn_kwargs, shutdown_process_tree,
)
from ...tasks import spawn
from ..state import home_dir


@dataclass(frozen=True)
class LingtaiLaunch:
    executable: Path
    agent_dir: Path
    workspace: Path
    registry: Path
    runtime_id: str

    def argv(self) -> list[str]:
        return [
            str(self.executable), "acp", "--profile", "puffo-v1",
            "--runtime-id", self.runtime_id, "--registry", str(self.registry),
        ]


def parse_lingtai_launch(raw: object) -> LingtaiLaunch | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("runtime.lingtai must be an object")
    paths = {}
    for key in ("executable", "agent_dir", "workspace"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"LingTai {key} is required")
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"LingTai {key} must be an absolute path on this machine")
        resolved = path.resolve(strict=True)
        # Keep the selected executable spelling through provision and ACP.
        paths[key] = path if key == "executable" else resolved
    if not paths["executable"].is_file() or not os.access(paths["executable"], os.X_OK):
        raise ValueError("LingTai executable must be an executable file")
    if not paths["agent_dir"].is_dir() or not (paths["agent_dir"] / "init.json").is_file():
        raise ValueError("LingTai agent_dir must contain init.json")
    if not paths["workspace"].is_dir():
        raise ValueError("LingTai workspace must be an existing directory")
    return LingtaiLaunch(
        **paths,
        registry=home_dir().resolve() / "lingtai" / "runtime-registry.json",
        runtime_id="puffo-" + uuid.uuid4().hex,
    )


async def provision_lingtai(launch: LingtaiLaunch) -> None:
    await _command(launch, [
        "puffo-v0", "provision", "--runtime-id", launch.runtime_id,
        "--agent-dir", str(launch.agent_dir), "--workspace", str(launch.workspace),
        "--registry", str(launch.registry),
    ])


async def revoke_lingtai(launch: LingtaiLaunch) -> None:
    await _command(launch, [
        "puffo-v0", "revoke", "--runtime-id", launch.runtime_id,
        "--registry", str(launch.registry),
    ])


async def _command(launch: LingtaiLaunch, args: list[str]) -> None:
    process = await asyncio.create_subprocess_exec(
        str(launch.executable), *args, cwd=launch.workspace,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        limit=8193,
        **process_group_spawn_kwargs(),
    )
    waiter = spawn(process.wait(), name="lingtai.command.wait")
    errors: list[BaseException] = []
    try:
        async with asyncio.timeout(30):
            assert process.stderr is not None
            try:
                error = await process.stderr.readexactly(8193)
                raise ValueError("LingTai error output exceeded the size limit")
            except asyncio.IncompleteReadError as exc:
                error = exc.partial
            code = await asyncio.shield(waiter)
    except BaseException as exc:
        errors.append(exc)
    await _close_command(process, waiter, errors)
    if code:
        detail = error.decode("utf-8", errors="replace").strip()
        raise ValueError(detail or f"LingTai command failed with exit code {code}")


async def _close_command(process, waiter: asyncio.Task, errors: list[BaseException]) -> None:
    # Join cleanup despite repeated request cancellation. The waiter starts at
    # spawn time, before a short-lived parent can exit with inherited pipes open.
    # Native Windows taskkill startup can exceed one second.
    await collect_cleanup_errors(
        shutdown_process_tree(process, waiter=waiter, timeout=3 if os.name == "nt" else 1,
                              task_name="lingtai.command.shutdown"),
        errors, timeout=10,
    )
    if not waiter.done():
        abandon_process_transport(process)
        waiter.cancel()
    raise_collected_errors("LingTai command and cleanup failed", errors)
