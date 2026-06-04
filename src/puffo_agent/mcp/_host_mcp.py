"""Filesystem + catalog plumbing for install_host_mcp / sync_host_mcp.

Kept in a dedicated module so puffo_core_tools.py stays focused on
the MCP tool decorators. The two ``_impl`` entry points are awaited
straight from the tool bodies in puffo_core_tools.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .puffo_core_tools import PuffoCoreToolsConfig


def _read_claude_json(path: Path) -> dict[str, Any]:
    """Read a ``.claude.json``. Returns ``{}`` on missing / unreadable /
    non-object. Callers MUST treat an unreadable file as "leave it
    alone" — we don't want to clobber operator state on parse error.
    """
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _atomic_write_claude_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` atomically: tmp file then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _spec_from_template(template: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce a catalog ``mcp_template`` row into the
    ``mcpServers[<id>]`` shape Claude Code reads. Mirrors
    ``desired_install.normalize_mcp_spec`` for the same wire shape.
    """
    transport = str(template.get("type") or "").lower()
    args = template.get("args") or []
    env = template.get("env") or {}
    args_list = [str(a) for a in args] if isinstance(args, list) else []
    env_map = (
        {str(k): str(v) for k, v in env.items()}
        if isinstance(env, dict) else {}
    )
    if transport == "stdio":
        command = template.get("command")
        if not isinstance(command, str) or not command:
            return None
        return {
            "type": "stdio",
            "command": command,
            "args": args_list,
            "env": env_map,
        }
    if transport in ("sse", "http"):
        url = template.get("url")
        if not isinstance(url, str) or not url:
            return None
        return {"type": transport, "url": url, "env": env_map}
    return None


def _build_operator_dm_body(
    template: dict[str, Any], template_id: str, spec: dict[str, Any],
) -> str:
    """Operator-facing DM body — describes what was just installed on
    their host and what env / OAuth they need to populate.
    """
    name = template.get("name") or template_id
    description = template.get("description") or ""
    env_map = spec.get("env") or {}
    missing = [k for k, v in env_map.items() if not str(v)]
    parts = [
        f"I just installed {name} into your host ~/.claude.json as "
        f"mcpServers[{template_id!r}].",
    ]
    if description:
        parts.append(description)
    if missing:
        env_lines = "\n".join(f"  - {k}" for k in missing)
        parts.append(f"This MCP needs you to populate these env values:\n{env_lines}")
        parts.append(
            "Complete the OAuth or paste the API key(s) on your host "
            "(the MCP package's own setup flow), then ping me back."
        )
    else:
        parts.append("No env setup is required — let me know when you're ready.")
    parts.append(
        f"Once host is ready I'll sync it into my own config and "
        f"refresh — no further action needed from you."
    )
    return "\n\n".join(parts)


async def _send_dm_to_operator(
    cfg: "PuffoCoreToolsConfig", recipient_slug: str, text: str,
) -> str:
    """DM ``recipient_slug`` with ``text``. Returns the envelope_id on
    success. Raises on any failure (cert resolve, sign, POST). Mirrors
    the DM path of ``send_message`` minus the channel-routing case.
    """
    from ..crypto.keystore import decode_secret
    from ..crypto.message import EncryptInput, encrypt_message
    from ..crypto.primitives import Ed25519KeyPair
    from .puffo_core_tools import _fetch_device_keys

    sess = cfg.keystore.load_session(cfg.slug)
    signing_key = Ed25519KeyPair.from_secret_bytes(
        decode_secret(sess.subkey_secret_key)
    )
    # Fan to the recipient + our own other devices so the operator's
    # other clients see the DM too.
    devices = await _fetch_device_keys(
        cfg.http_client, [cfg.slug, recipient_slug],
    )
    if not devices:
        raise RuntimeError(
            f"no recipient devices resolved for @{recipient_slug}"
        )
    inp = EncryptInput(
        envelope_kind="dm",
        sender_slug=cfg.slug,
        sender_subkey_id=sess.subkey_id,
        is_visible_to_human=True,
        space_id=None,
        channel_id=None,
        recipient_slug=recipient_slug,
        thread_root_id="",
        content_type="text/plain",
        content=text,
        recipients=devices,
    )
    envelope = encrypt_message(inp, signing_key)
    await cfg.http_client.post("/messages", envelope)
    return str(envelope.get("envelope_id") or "?")


async def _install_host_mcp_impl(
    cfg: "PuffoCoreToolsConfig", template_id: str,
) -> str:
    if not cfg.host_home:
        raise RuntimeError(
            "install_host_mcp unavailable — PUFFO_HOST_HOME not set "
            "on this MCP runtime (only cli-local supports host writes)."
        )
    if not template_id or not isinstance(template_id, str):
        raise RuntimeError("install_host_mcp: template_id is required")

    # Catalog fetch — failure here is before any side effect, so just
    # surface to the agent as a tool error.
    try:
        template = await cfg.http_client.get(
            f"/v2/mcp-templates/{template_id}"
        )
    except Exception as exc:
        raise RuntimeError(
            f"install_host_mcp: catalog fetch failed for "
            f"{template_id!r}: {exc}"
        ) from exc
    if not isinstance(template, dict):
        raise RuntimeError(
            f"install_host_mcp: catalog returned a non-object body "
            f"for {template_id!r}"
        )
    spec = _spec_from_template(template)
    if spec is None:
        raise RuntimeError(
            f"install_host_mcp: catalog entry for {template_id!r} has "
            f"an unsupported transport or is missing a required field"
        )

    # Already-present guard. Host file is the source of truth — if it
    # has the entry we don't touch it AND we don't DM (operator
    # already configured it once).
    host_claude_json = Path(cfg.host_home) / ".claude.json"
    data = _read_claude_json(host_claude_json)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if template_id in servers:
        return (
            f"{template_id!r} is already registered in the host's "
            f"~/.claude.json — left untouched. Call "
            f"sync_host_mcp({template_id!r}) to pull the operator's "
            f"populated entry into your own config."
        )

    # Side effect: write the catalog spec to host's .claude.json. On
    # failure, bail before attempting the DM — the operator has
    # nothing to act on if the file didn't change.
    servers[template_id] = spec
    data["mcpServers"] = servers
    try:
        _atomic_write_claude_json(host_claude_json, data)
    except OSError as exc:
        raise RuntimeError(
            f"install_host_mcp: failed to write host's "
            f"~/.claude.json: {exc}"
        ) from exc

    dm_body = _build_operator_dm_body(template, template_id, spec)

    # No operator_slug → can't DM, hand the body back so the agent
    # can find the operator some other way.
    if not cfg.operator_slug:
        return (
            f"Installed {template_id!r} into host's ~/.claude.json. "
            f"No operator_slug is configured on this MCP runtime, so "
            f"I couldn't DM the setup steps automatically. Forward "
            f"this to the operator yourself:\n\n---\n{dm_body}\n---"
        )

    # Host write succeeded — try the auto-DM. On failure, return the
    # body so the agent can retry via send_message.
    try:
        envelope_id = await _send_dm_to_operator(
            cfg, cfg.operator_slug, dm_body,
        )
    except Exception as exc:
        return (
            f"Installed {template_id!r} into host's ~/.claude.json, "
            f"BUT sending the setup-instructions DM to "
            f"@{cfg.operator_slug} failed ({exc}). Retry by sending "
            f"this yourself:\n\n"
            f"send_message(channel='@{cfg.operator_slug}', "
            f"is_visible_to_human=True, text=<<<\n{dm_body}\n>>>)"
        )

    return (
        f"Installed {template_id!r} into host's ~/.claude.json AND "
        f"DM'd @{cfg.operator_slug} the setup steps (envelope_id "
        f"{envelope_id}). They'll ping you when host setup is done; "
        f"call sync_host_mcp({template_id!r}) then refresh()."
    )


async def _sync_host_mcp_impl(
    cfg: "PuffoCoreToolsConfig", template_id: str,
) -> str:
    if not cfg.host_home or not cfg.agent_home:
        raise RuntimeError(
            "sync_host_mcp unavailable — PUFFO_HOST_HOME / agent_home "
            "not set on this MCP runtime."
        )
    if not template_id or not isinstance(template_id, str):
        raise RuntimeError("sync_host_mcp: template_id is required")

    host_claude_json = Path(cfg.host_home) / ".claude.json"
    host_data = _read_claude_json(host_claude_json)
    host_servers = host_data.get("mcpServers")
    if not isinstance(host_servers, dict) or template_id not in host_servers:
        return (
            f"sync_host_mcp: no entry for {template_id!r} in the host's "
            f"~/.claude.json. Call install_host_mcp({template_id!r}) first "
            f"and DM the operator the setup steps."
        )
    entry = host_servers[template_id]

    agent_claude_json = Path(cfg.agent_home) / ".claude.json"
    agent_data = _read_claude_json(agent_claude_json)
    agent_servers = agent_data.get("mcpServers")
    if not isinstance(agent_servers, dict):
        agent_servers = {}
    agent_servers[template_id] = entry
    agent_data["mcpServers"] = agent_servers
    _atomic_write_claude_json(agent_claude_json, agent_data)
    return (
        f"Synced host's {template_id!r} entry into your "
        f"~/.claude.json. Call refresh() so claude respawns and "
        f"loads it."
    )
