"""Daemon-side implementation of ``install_host_mcp`` /
``sync_host_mcp``.

The two MCP tools call into here via the data service (loopback
HTTP). Lives on the daemon process so:

  - cli-docker agents work too — the operator's ``~/.claude.json``
    isn't bind-mounted into the container, but the daemon runs on
    host and can read+write it directly.
  - host writes happen in one process — no two-writer races on the
    operator's config file when multiple agents drive installs.
  - DM signing reuses each agent's already-warm keystore + http
    client; no extra credential plumbing into MCP subprocesses.

The MCP-side wrapper in ``mcp.puffo_core_tools`` is now a thin RPC
that POSTs to the data service routes registered below.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..crypto.encoding import base64url_decode
from ..crypto.http_client import PuffoCoreHttpClient
from ..crypto.keystore import KeyStore, decode_secret
from ..crypto.message import EncryptInput, RecipientDevice, encrypt_message
from ..crypto.primitives import Ed25519KeyPair

logger = logging.getLogger(__name__)


_VALID_TRANSPORTS = {"stdio", "sse", "http"}


@dataclass
class HostMcpContext:
    """Per-agent dispatch context the daemon assembles when a data
    service host-mcp request arrives. All fields come from the
    running ``PuffoCoreMessageClient`` for that agent plus the
    operator's real ``Path.home()`` (the daemon process itself).
    """
    agent_id: str
    slug: str               # agent's own puffo slug
    operator_slug: str
    host_home: Path         # operator's real ~/
    agent_home: Path        # ~/.puffo-agent/agents/<agent_id>/
    keystore: KeyStore
    http_client: PuffoCoreHttpClient


# ── filesystem helpers (host & agent .claude.json) ─────────────────


def _read_claude_json(path: Path) -> dict[str, Any]:
    """Best-effort read. Returns ``{}`` on missing / unreadable /
    non-object body — never raises so callers can decide whether
    "nothing there yet" or "leave it alone" is the right answer."""
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


# ── catalog + adhoc spec normalisation ─────────────────────────────


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


def _validate_adhoc_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Coerce an agent-supplied spec into the on-disk mcpServers[<id>]
    shape. Raises with a clear message on shape issues so the agent
    sees what's wrong."""
    transport = str(spec.get("type") or "stdio").lower()
    if transport not in _VALID_TRANSPORTS:
        raise RuntimeError(
            f"install_host_mcp: spec.type must be one of "
            f"{sorted(_VALID_TRANSPORTS)}, got {transport!r}"
        )
    args_raw = spec.get("args") or []
    env_raw = spec.get("env") or {}
    if not isinstance(args_raw, list):
        raise RuntimeError("install_host_mcp: spec.args must be a list of strings")
    if not isinstance(env_raw, dict):
        raise RuntimeError(
            "install_host_mcp: spec.env must be an object mapping env "
            "var name to string value (use empty string for placeholders)"
        )
    args_list = [str(a) for a in args_raw]
    env_map = {str(k): str(v) for k, v in env_raw.items()}
    if transport == "stdio":
        command = spec.get("command")
        if not isinstance(command, str) or not command:
            raise RuntimeError(
                "install_host_mcp: spec.command is required for stdio "
                "transport"
            )
        return {
            "type": "stdio",
            "command": command,
            "args": args_list,
            "env": env_map,
        }
    url = spec.get("url")
    if not isinstance(url, str) or not url:
        raise RuntimeError(
            f"install_host_mcp: spec.url is required for {transport!r} transport"
        )
    return {"type": transport, "url": url, "env": env_map}


# ── DM body + send ─────────────────────────────────────────────────


def _build_operator_dm_body(*, name: str, display_name: str) -> str:
    """Minimal DM body — one bold-stamped line confirming the
    install. Operator's host file is their source of truth for what
    to populate next; the agent sends a follow-up itself for
    setup-context (docs URLs, env hints) so this stays predictable.
    """
    return (
        f"I just installed **{display_name}** into your host "
        f"~/.claude.json as mcpServers[{name!r}]."
    )


async def _fetch_device_keys(
    http_client: PuffoCoreHttpClient, slugs: list[str],
) -> list[RecipientDevice]:
    """Paginate ``/certs/sync?slugs=...`` and collect
    ``(device_id, kem_pk)`` for every returned device_cert. Same
    shape as ``mcp.puffo_core_tools._fetch_device_keys`` but pulled
    onto the daemon side so DM send doesn't reach back into the MCP
    module."""
    if not slugs:
        return []
    slugs_param = ",".join(slugs)
    devices: list[RecipientDevice] = []
    seen_ids: set[str] = set()
    since = 0
    while True:
        data = await http_client.get(
            f"/certs/sync?slugs={slugs_param}&since={since}"
        )
        for entry in data.get("entries", []):
            if entry.get("kind") == "device_cert":
                cert = entry.get("cert", {})
                dev_id = cert.get("device_id", "")
                keys_block = cert.get("keys") or {}
                enc_block = keys_block.get("encryption") or {}
                kem_b64 = (
                    enc_block.get("public_key")
                    or cert.get("kem_public_key", "")
                )
                if dev_id and kem_b64 and dev_id not in seen_ids:
                    try:
                        devices.append(RecipientDevice(
                            device_id=dev_id,
                            kem_public_key=base64url_decode(kem_b64),
                        ))
                        seen_ids.add(dev_id)
                    except Exception:
                        pass
            since = entry.get("seq", since)
        if not data.get("has_more"):
            break
    return devices


async def _send_dm_to_operator(
    ctx: HostMcpContext, text: str,
) -> str:
    """DM ``ctx.operator_slug`` from ``ctx.slug``. Returns the
    envelope_id on success. Raises on any failure."""
    sess = ctx.keystore.load_session(ctx.slug)
    signing_key = Ed25519KeyPair.from_secret_bytes(
        decode_secret(sess.subkey_secret_key)
    )
    devices = await _fetch_device_keys(
        ctx.http_client, [ctx.slug, ctx.operator_slug],
    )
    if not devices:
        raise RuntimeError(
            f"no recipient devices resolved for @{ctx.operator_slug}"
        )
    inp = EncryptInput(
        envelope_kind="dm",
        sender_slug=ctx.slug,
        sender_subkey_id=sess.subkey_id,
        is_visible_to_human=True,
        space_id=None,
        channel_id=None,
        recipient_slug=ctx.operator_slug,
        thread_root_id="",
        content_type="text/plain",
        content=text,
        recipients=devices,
    )
    envelope = encrypt_message(inp, signing_key)
    await ctx.http_client.post("/messages", envelope)
    return str(envelope.get("envelope_id") or "?")


# ── handler entry points ───────────────────────────────────────────


async def install(
    ctx: HostMcpContext,
    *,
    name: str,
    template_id: str = "",
    spec: dict[str, Any] | None = None,
) -> str:
    """Run an install against the operator's host ``~/.claude.json``
    and DM them. Returns the message body the data service hands
    back to the calling agent."""
    if not ctx.operator_slug:
        raise RuntimeError(
            "install_host_mcp: agent has no operator_slug bound — "
            "this agent isn't owned by an operator account."
        )
    if not name or not isinstance(name, str):
        raise RuntimeError("install_host_mcp: name is required")
    if bool(template_id) == bool(spec):
        raise RuntimeError(
            "install_host_mcp: pass exactly one of `template_id` (look "
            "up from puffo-server catalog) or `spec` (inline MCP "
            "config dict from the MCP's own docs)"
        )

    display_name = name
    if template_id:
        try:
            template = await ctx.http_client.get(
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
        normalized = _spec_from_template(template)
        if normalized is None:
            raise RuntimeError(
                f"install_host_mcp: catalog entry for {template_id!r} "
                f"has an unsupported transport or is missing a "
                f"required field"
            )
        spec_to_write = normalized
        display_name = str(template.get("name") or name)
    else:
        spec_to_write = _validate_adhoc_spec(spec or {})

    host_claude_json = ctx.host_home / ".claude.json"
    data = _read_claude_json(host_claude_json)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if name in servers:
        return (
            f"{name!r} is already registered in the host's "
            f"~/.claude.json — left untouched. Call "
            f"sync_host_mcp({name!r}) to pull the operator's "
            f"populated entry into your own config."
        )

    servers[name] = spec_to_write
    data["mcpServers"] = servers
    try:
        _atomic_write_claude_json(host_claude_json, data)
    except OSError as exc:
        raise RuntimeError(
            f"install_host_mcp: failed to write host's "
            f"~/.claude.json: {exc}"
        ) from exc

    dm_body = _build_operator_dm_body(name=name, display_name=display_name)
    try:
        envelope_id = await _send_dm_to_operator(ctx, dm_body)
    except Exception as exc:
        return (
            f"Installed {name!r} into host's ~/.claude.json, "
            f"BUT sending the setup-instructions DM to "
            f"@{ctx.operator_slug} failed ({exc}). Retry by sending "
            f"this yourself:\n\n"
            f"send_message(channel='@{ctx.operator_slug}', "
            f"is_visible_to_human=True, text=<<<\n{dm_body}\n>>>)"
        )

    return (
        f"Installed {name!r} into host's ~/.claude.json AND "
        f"DM'd @{ctx.operator_slug} the setup steps (envelope_id "
        f"{envelope_id}). They'll ping you when host setup is done; "
        f"call sync_host_mcp({name!r}) then refresh()."
    )


async def sync(ctx: HostMcpContext, *, template_id: str) -> str:
    """Mirror the operator's host ``mcpServers[<id>]`` into the
    agent's ``<agent_home>/.claude.json``. For cli-docker the agent
    home is bind-mounted into the container, so the container's
    claude sees the update on the next ``refresh()``."""
    if not template_id or not isinstance(template_id, str):
        raise RuntimeError("sync_host_mcp: template_id is required")

    host_claude_json = ctx.host_home / ".claude.json"
    host_data = _read_claude_json(host_claude_json)
    host_servers = host_data.get("mcpServers")
    if not isinstance(host_servers, dict) or template_id not in host_servers:
        return (
            f"sync_host_mcp: no entry for {template_id!r} in the host's "
            f"~/.claude.json. Call install_host_mcp({template_id!r}) "
            f"first and DM the operator the setup steps."
        )
    entry = host_servers[template_id]

    agent_claude_json = ctx.agent_home / ".claude.json"
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
