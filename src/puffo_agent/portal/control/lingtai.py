"""Operator-selected LingTai folders at the machine creation boundary."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...agent.harness.support.cleanup_errors import collect_cleanup_errors, raise_collected_errors
from ...agent.harness.support.subprocess_io import (
    abandon_process_transport, process_group_spawn_kwargs, shutdown_process_tree,
)
from ...tasks import spawn

if TYPE_CHECKING:
    from ...agent.harness.drivers.acp_attach import AttachTarget


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


def lingtai_registry_path() -> Path:
    """Match the registry selected by an ordinary LingTai resident launch."""
    override = os.environ.get("LINGTAI_PUFFO_V0_REGISTRY")
    path = Path(override) if override else Path.home() / ".lingtai" / "puffo-v0" / "runtime-registry.json"
    if not path.is_absolute() or ".." in path.parts or path.parent == path.parent.parent:
        raise ValueError("LingTai registry must be an absolute path in a dedicated directory")
    return path


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
        registry=lingtai_registry_path(),
        runtime_id="puffo-" + uuid.uuid4().hex,
    )


async def provision_lingtai(launch: LingtaiLaunch) -> None:
    await _command(launch, [
        "puffo-v0", "provision", "--runtime-id", launch.runtime_id,
        "--agent-dir", str(launch.agent_dir), "--workspace", str(launch.workspace),
        "--registry", str(launch.registry),
    ])


async def resident_lingtai_available(launch: LingtaiLaunch) -> bool:
    """Select spawn only for an absent socket; reject ambiguous resident failures."""
    output = await _socket_path_output(launch.executable, launch.workspace, launch.agent_dir)
    lines = output.decode("utf-8", errors="strict").splitlines()
    if len(lines) != 1 or not Path(lines[0]).is_absolute():
        raise ValueError("LingTai acp-socket-path did not print one absolute path")
    return await _resident_socket_listening(Path(lines[0]), "retry import")


async def _resident_socket_listening(path: Path, retry: str) -> bool:
    """False only when no socket exists; a socket nobody answers is an error."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(mode):
        raise ValueError("LingTai resident ACP path is not a socket")
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=1,
        )
    except (OSError, TimeoutError) as exc:
        raise ValueError(
            "LingTai resident ACP socket is unavailable; start or restart "
            f"the LingTai Agent, then {retry}"
        ) from exc
    writer.close()
    await writer.wait_closed()
    return True


async def running_lingtai_target(harness_command: list[str]) -> AttachTarget | None:
    """The running LingTai Agent to attach to, or None when none is running.

    Asked on every start, so an Agent the user opened after import is picked
    up and one they closed is started by Puffo instead. Only ``lingtai run``
    serves this socket; the ``lingtai acp`` child Puffo starts does not, so a
    previous Puffo-started copy is never mistaken for a running Agent.
    """
    target = await resolve_attach_target(harness_command)
    listening = await _resident_socket_listening(target.socket_path, "restart this agent")
    return target if listening else None


async def revoke_lingtai(launch: LingtaiLaunch) -> None:
    await _command(launch, [
        "puffo-v0", "revoke", "--runtime-id", launch.runtime_id,
        "--registry", str(launch.registry),
    ])


_MAX_REGISTRY_BYTES = 1024 * 1024


async def resolve_attach_target(harness_command: list[str]) -> AttachTarget:
    """Find the socket of the LingTai Agent this ``puffo-v1`` argv names.

    The runtime id and registry come from the argv provision wrote. The
    registry says which directory the runtime lives in, and LingTai itself
    turns that directory into a socket path, so the naming rule stays on
    LingTai's side. Nothing here is trusted: the Agent checks the runtime id,
    registry and directory again when Puffo attaches.
    """
    from ...agent.harness.drivers.acp import _lingtai_constrained_profile
    from ...agent.harness.drivers.acp_attach import AttachTarget

    command = tuple(harness_command)
    if _lingtai_constrained_profile(command) != "puffo-v1":
        raise ValueError("LingTai attach needs a puffo-v1 harness command")
    runtime_id = _last_option(command, "--runtime-id")
    registry = _last_option(command, "--registry")
    if not runtime_id or not registry or not Path(registry).is_absolute():
        raise ValueError("LingTai harness command lacks --runtime-id or an absolute --registry")
    agent_dir = _registered_agent_dir(Path(registry), runtime_id)
    output = await _socket_path_output(Path(command[0]), agent_dir, agent_dir)
    lines = output.decode("utf-8", errors="replace").splitlines()
    if len(lines) != 1 or not Path(lines[0]).is_absolute():
        raise ValueError("LingTai acp-socket-path did not print one absolute path")
    return AttachTarget(socket_path=Path(lines[0]), runtime_id=runtime_id,
                        registry=Path(registry))


async def _socket_path_output(executable: Path, cwd: Path, agent_dir: Path) -> bytes:
    try:
        return await _run(executable, cwd, ["acp-socket-path", str(agent_dir)], capture=True)
    except ValueError as exc:
        # Older LingTai binaries do not know this subcommand. Keep this distinct
        # from an existing socket that cannot accept connections: restarting an
        # old binary cannot add the attach capability.
        if "invalid choice: 'acp-socket-path'" in str(exc):
            raise ValueError(
                "LingTai kernel 1.0.9 or newer is required for Load Agent attach; "
                "upgrade the LingTai kernel, then retry import"
            ) from exc
        raise


def _last_option(command: tuple[str, ...], flag: str) -> str:
    # argparse keeps the last occurrence; read the same value LingTai would.
    value = ""
    for index, arg in enumerate(command):
        if arg == flag and index + 1 < len(command):
            value = command[index + 1]
        elif arg.startswith(flag + "="):
            value = arg.removeprefix(flag + "=")
    return value


def _registered_agent_dir(registry: Path, runtime_id: str) -> Path:
    try:
        with registry.open("rb") as stream:
            data = stream.read(_MAX_REGISTRY_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"LingTai registry is unreadable: {exc.strerror}") from None
    if len(data) > _MAX_REGISTRY_BYTES:
        raise ValueError("LingTai registry exceeds its size limit")
    try:
        runtimes = json.loads(data).get("runtimes")
        directory = runtimes[runtime_id]["agent_dir"]
    except (ValueError, AttributeError, KeyError, TypeError):
        raise ValueError(f"LingTai registry has no agent directory for {runtime_id}") from None
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise ValueError(f"LingTai registry has no agent directory for {runtime_id}")
    return Path(directory)


async def _command(launch: LingtaiLaunch, args: list[str]) -> None:
    await _run(launch.executable, launch.workspace, args)


async def _run(executable: Path, cwd: Path, args: list[str], *, capture: bool = False) -> bytes:
    # Captured output is buffered up to the stream limit while stderr is read;
    # a command that prints far more stalls and ends in the timeout.
    process = await asyncio.create_subprocess_exec(
        str(executable), *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        limit=8193,
        **process_group_spawn_kwargs(),
    )
    waiter = spawn(process.wait(), name="lingtai.command.wait")
    errors: list[BaseException] = []
    output = b""
    try:
        async with asyncio.timeout(30):
            assert process.stderr is not None
            try:
                error = await process.stderr.readexactly(8193)
                raise ValueError("LingTai error output exceeded the size limit")
            except asyncio.IncompleteReadError as exc:
                error = exc.partial
            code = await asyncio.shield(waiter)
            if capture:
                assert process.stdout is not None
                output = await process.stdout.read()
    except BaseException as exc:
        errors.append(exc)
    await _close_command(process, waiter, errors)
    if code:
        detail = error.decode("utf-8", errors="replace").strip()
        raise ValueError(detail or f"LingTai command failed with exit code {code}")
    if len(output) > 8192:
        raise ValueError("LingTai output exceeded the size limit")
    return output


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
