"""MCP config builders for the puffo-core agent runtime.

Every adapter (cli-local, cli-docker, sdk-local) spawns the same
puffo_core MCP server through slightly different transports. This
module centralises env-var names, the MCP subprocess command line,
and the JSON config shape claude-code expects.

Tool names appear as ``mcp__puffo__<tool>`` when invoked from
claude-code; allowlists / ``--permission-prompt-tool`` reference
that form.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


MCP_SERVER_NAME = "puffo"


PUFFO_CORE_TOOL_NAMES = (
    "send_message",
    "upload_file",
    "list_channels",
    "list_channel_members",
    "get_channel_history",
    "fetch_channel_files",
    "get_post",
    "get_user_info",
    "whoami",
    "reload_system_prompt",
    "install_skill",
    "uninstall_skill",
    "list_skills",
    "install_mcp_server",
    "uninstall_mcp_server",
    "list_mcp_servers",
    "refresh",
)
PUFFO_CORE_TOOL_FQNS = tuple(
    f"mcp__{MCP_SERVER_NAME}__{t}" for t in PUFFO_CORE_TOOL_NAMES
)


def cli_mcp_config_doc(
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
) -> dict:
    """Build the document for claude-code's ``--mcp-config`` flag
    (top-level ``mcpServers`` key, stdio schema)."""
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "type": "stdio",
                "command": command,
                "args": list(args),
                "env": dict(env),
            }
        }
    }


def write_cli_mcp_config(
    dest: Path,
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
) -> Path:
    """Serialise the CLI MCP config to ``dest``. Returns the path."""
    doc = cli_mcp_config_doc(command=command, args=args, env=env)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return dest


def default_python_executable() -> str:
    """Path to the daemon's own interpreter — the right choice for
    SDK / cli-local, where the MCP runs in the same interpreter tree.
    """
    return sys.executable or "python3"


# ── puffo-core config builders ────────────────────────────────────


def puffo_core_mcp_env(
    *,
    slug: str,
    device_id: str,
    server_url: str,
    space_id: str = "",
    keystore_dir: str,
    workspace: str,
    agent_id: str = "",
    data_service_url: str = "http://127.0.0.1:63386",
    runtime_kind: str = "",
    harness: str = "",
) -> dict[str, str]:
    """Env dict for the puffo-core MCP subprocess.

    ``data_service_url`` defaults to the daemon's loopback data
    service (port 63386). cli-docker rewrites it to
    ``http://host.docker.internal:63386`` so the container can
    reach the host loopback. The MCP never opens ``messages.db``
    directly — the daemon is the sole owner.
    """
    env: dict[str, str] = {
        "PUFFO_CORE_SLUG": slug,
        "PUFFO_CORE_DEVICE_ID": device_id,
        "PUFFO_CORE_SERVER_URL": server_url,
        "PUFFO_CORE_KEYSTORE_DIR": keystore_dir,
        "PUFFO_WORKSPACE": workspace,
        "PUFFO_DATA_SERVICE_URL": data_service_url,
    }
    if agent_id:
        env["PUFFO_AGENT_ID"] = agent_id
    if space_id:
        env["PUFFO_CORE_SPACE_ID"] = space_id
    if runtime_kind:
        env["PUFFO_RUNTIME_KIND"] = runtime_kind
    if harness:
        env["PUFFO_HARNESS"] = harness
    return env


def puffo_core_stdio_sdk_config(
    *,
    python: str,
    slug: str,
    device_id: str,
    server_url: str,
    space_id: str = "",
    keystore_dir: str,
    workspace: str,
    agent_id: str,
) -> dict:
    """Return the ``mcp_servers`` config dict for the SDK adapter."""
    return {
        MCP_SERVER_NAME: {
            "type": "stdio",
            "command": python,
            "args": ["-m", "puffo_agent.mcp.puffo_core_server"],
            "env": puffo_core_mcp_env(
                slug=slug,
                device_id=device_id,
                server_url=server_url,
                space_id=space_id,
                keystore_dir=keystore_dir,
                workspace=workspace,
                agent_id=agent_id,
                runtime_kind="sdk-local",
            ),
        }
    }
