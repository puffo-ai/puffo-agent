"""Installation and runtime attestation for the Pi Puffo tool bridge.

Pi has no MCP, so Puffo's tools reach a Pi agent only through the TypeScript
extension shipped alongside this module. Two separate facts have to hold, and
conflating them is the failure this module exists to prevent:

* the bridge is **installed** -- a file is on disk where Pi auto-discovers it;
* the bridge is **loaded** -- *this* Pi process registered the tools.

A file check answers only the first. An extension that throws on load, is
disabled, or belongs to a previous run leaves the file in place and the agent
mute, so readiness is attested per spawn with a nonce the driver mints and
clears.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

from ...driver import McpServerSpec

BRIDGE_CONFIG_ENV = "PUFFO_PI_BRIDGE_CONFIG"
BRIDGE_READY_FILE_ENV = "PUFFO_PI_BRIDGE_READY_FILE"
BRIDGE_NONCE_ENV = "PUFFO_PI_BRIDGE_NONCE"

BRIDGE_FILENAME = "puffo-tools.ts"
_BRIDGE_SOURCE = Path(__file__).with_name(BRIDGE_FILENAME)

# Pi auto-discovers <config dir>/extensions/*.ts.
EXTENSIONS_SUBDIR = "extensions"


def bridge_source_text() -> str:
    return _BRIDGE_SOURCE.read_text(encoding="utf-8")


def bridge_install_path(pi_agent_dir: Path) -> Path:
    return Path(pi_agent_dir) / EXTENSIONS_SUBDIR / BRIDGE_FILENAME


def install_pi_tool_bridge(pi_agent_dir: Path) -> Path:
    """Install or refresh the bridge, rewriting only when the content changed.

    Rewriting unconditionally would touch the file on every spawn, which makes
    "when did this change?" unanswerable during an incident.
    """
    target = bridge_install_path(pi_agent_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    desired = bridge_source_text()
    try:
        if target.read_text(encoding="utf-8") == desired:
            return target
    except (OSError, UnicodeDecodeError):
        pass
    temp = target.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(desired, encoding="utf-8")
    os.replace(temp, target)
    return target


def mint_bridge_nonce() -> str:
    return secrets.token_hex(16)


def ready_file_path(pi_agent_dir: Path) -> Path:
    return Path(pi_agent_dir) / "puffo-bridge-ready.json"


def build_bridge_environment(
    *,
    mcp: McpServerSpec,
    ready_file: Path,
    nonce: str,
) -> dict[str, str]:
    """Controlled env for the Pi child; the bridge never discovers a path."""
    return {
        BRIDGE_CONFIG_ENV: json.dumps(
            {
                "command": mcp.command,
                "args": list(mcp.args),
                "environment": dict(mcp.environment),
            },
            separators=(",", ":"),
        ),
        BRIDGE_READY_FILE_ENV: str(ready_file),
        BRIDGE_NONCE_ENV: nonce,
    }


def clear_ready_file(path: Path) -> None:
    """Remove a previous run's attestation before spawning.

    Load-bearing: the nonce lives in the spec and therefore survives a restart,
    so a stale file left in place would attest a bridge this process never
    loaded.
    """
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return


def read_ready_attestation(path: Path, nonce: str) -> int | None:
    """Return the registered tool count, or None when not attested."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("nonce") != nonce:
        return None
    tools = payload.get("tools")
    if isinstance(tools, bool) or not isinstance(tools, int) or tools <= 0:
        # A zero-tool attestation is a mute agent that reported success.
        return None
    return tools


async def await_bridge_ready(
    path: Path,
    nonce: str,
    *,
    timeout_seconds: float = 20.0,
    poll_seconds: float = 0.1,
) -> int | None:
    """Bounded wait for this spawn's attestation; None on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        tools = read_ready_attestation(path, nonce)
        if tools is not None:
            return tools
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(poll_seconds)
