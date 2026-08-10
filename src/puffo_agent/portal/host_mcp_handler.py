"""Daemon-side ``install_host_mcp`` / ``sync_host_mcp``. Runs on the
daemon (not the MCP subprocess) so cli-docker agents can reach the
operator's host config and concurrent installs don't race on it."""

from __future__ import annotations

import json
import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..crypto.encoding import base64url_decode
from ..crypto.http_client import PuffoCoreHttpClient
from ..crypto.keystore import KeyStore, decode_secret
from ..agent import send_mode
from ..agent.send_coordinator import SemanticSendRequest, failed_result
from ..crypto.message import (
    EncryptInput,
    RecipientDevice,
    build_plaintext_message,
    encrypt_message,
)
from ..crypto.primitives import Ed25519KeyPair
from ..mcp.config import _emit_codex_mcp_block

logger = logging.getLogger(__name__)


_VALID_TRANSPORTS = {"stdio", "sse", "http"}


@dataclass
class HostMcpContext:
    """Per-agent dispatch context. ``harness`` routes to the correct
    operator config: ``claude-code`` → ``~/.claude.json``, ``codex``
    → ``~/.codex/config.toml``. Other harnesses raise."""

    agent_id: str
    slug: str
    operator_slug: str
    host_home: Path
    agent_home: Path
    harness: str
    keystore: KeyStore
    http_client: PuffoCoreHttpClient
    # The worker's live PuffoCoreMessageClient, for tools that drive
    # daemon-side state (leave requests). None until warm().
    message_client: Any = None
    # The worker's single persistent semantic send coordinator. Package 4
    # supplies it; optional preserves existing context constructors.
    send_coordinator: Any = None


# ── filesystem helpers (host & agent .claude.json) ─────────────────


def _read_claude_json(path: Path) -> dict[str, Any]:
    """Best-effort read. Returns ``{}`` on missing / unreadable / non-object; never raises."""
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


# ── codex config.toml helpers ──────────────────────────────────────


def _codex_host_config_path(host_home: Path) -> Path:
    """Honours ``$CODEX_HOME`` so we land in the same file the operator's codex CLI reads."""
    codex_home_env = os.environ.get("CODEX_HOME")
    codex_home = Path(codex_home_env) if codex_home_env else host_home / ".codex"
    return codex_home / "config.toml"


def _agent_codex_config_path(agent_home: Path) -> Path:
    return agent_home / ".codex" / "config.toml"


def _read_codex_mcp_servers(path: Path) -> dict[str, dict]:
    """Return the ``[mcp_servers.*]`` table; ``{}`` on missing / unreadable / non-object."""
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return {}
    raw = data.get("mcp_servers")
    return raw if isinstance(raw, dict) else {}


def _append_codex_mcp_block(path: Path, name: str, spec: dict[str, Any]) -> None:
    """Append a single block to ``path``. Never regenerates the whole
    file — operator's other config (auth, models, comments) must
    round-trip intact. Caller guarantees the entry isn't already present."""
    path.parent.mkdir(parents=True, exist_ok=True)
    block_lines = _emit_codex_mcp_block(name, spec)
    block_text = "\n".join(block_lines).rstrip("\n") + "\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + block_text, encoding="utf-8")
    else:
        path.write_text(block_text, encoding="utf-8")


# ── catalog + adhoc spec normalisation ─────────────────────────────


def _spec_from_template(template: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce a catalog row into the ``mcpServers[<id>]`` shape."""
    transport = str(template.get("type") or "").lower()
    args = template.get("args") or []
    env = template.get("env") or {}
    args_list = [str(a) for a in args] if isinstance(args, list) else []
    env_map = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
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
    """Coerce an agent-supplied spec into the on-disk shape. Raises with a clear message."""
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
                "install_host_mcp: spec.command is required for stdio transport"
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


def _build_operator_dm_body(
    *,
    name: str,
    display_name: str,
    host_path: str = "~/.claude.json",
) -> str:
    """One-line install confirmation. The agent sends docs/env hints separately."""
    return (
        f"I just installed **{display_name}** into your host {host_path} as {name!r}."
    )


async def _fetch_device_keys(
    http_client: PuffoCoreHttpClient,
    slugs: list[str],
) -> list[RecipientDevice]:
    """Paginate ``/certs/sync?slugs=...`` and collect ``(device_id, kem_pk)`` per device_cert."""
    if not slugs:
        return []
    slugs_param = ",".join(slugs)
    devices: list[RecipientDevice] = []
    seen_ids: set[str] = set()
    since = 0
    while True:
        data = await http_client.get(f"/certs/sync?slugs={slugs_param}&since={since}")
        for entry in data.get("entries", []):
            if entry.get("kind") == "device_cert":
                cert = entry.get("cert", {})
                dev_id = cert.get("device_id", "")
                keys_block = cert.get("keys") or {}
                enc_block = keys_block.get("encryption") or {}
                kem_b64 = enc_block.get("public_key") or cert.get("kem_public_key", "")
                if dev_id and kem_b64 and dev_id not in seen_ids:
                    try:
                        devices.append(
                            RecipientDevice(
                                device_id=dev_id,
                                kem_public_key=base64url_decode(kem_b64),
                            )
                        )
                        seen_ids.add(dev_id)
                    except Exception:
                        pass
            since = entry.get("seq", since)
        if not data.get("has_more"):
            break
    return devices


async def _send_dm_to_operator(
    ctx: HostMcpContext,
    text: str,
) -> str:
    """DM ``ctx.operator_slug`` from ``ctx.slug``. Returns the
    envelope_id on success. Raises on any failure."""
    sess = ctx.keystore.load_session(ctx.slug)
    signing_key = Ed25519KeyPair.from_secret_bytes(
        decode_secret(sess.subkey_secret_key)
    )
    # Unthreaded DM — only the turn-bundle rule applies (no store here).
    encrypt = await send_mode.encryption_required(ctx.slug, None, None)
    devices: list[RecipientDevice] = []
    if encrypt:
        devices = await _fetch_device_keys(
            ctx.http_client,
            [ctx.slug, ctx.operator_slug],
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
    if encrypt:
        envelope = encrypt_message(inp, signing_key)
        await ctx.http_client.post("/messages", envelope)
    else:
        envelope = build_plaintext_message(inp, signing_key)
        await ctx.http_client.post("/v2/messages/plaintext", envelope)
    return str(envelope.get("envelope_id") or "?")


# ── handler entry points ───────────────────────────────────────────


_SUPPORTED_HARNESSES = {"claude-code", "codex"}


def _require_supported_harness(ctx: HostMcpContext, tool: str) -> None:
    """Only claude-code + codex have a well-known host config to write into."""
    if ctx.harness not in _SUPPORTED_HARNESSES:
        raise RuntimeError(
            f"{tool}: harness {ctx.harness!r} is not supported "
            f"(supported: {sorted(_SUPPORTED_HARNESSES)})"
        )


async def install(
    ctx: HostMcpContext,
    *,
    name: str,
    template_id: str = "",
    spec: dict[str, Any] | None = None,
) -> str:
    """Install into the operator's host config and DM them."""
    _require_supported_harness(ctx, "install_host_mcp")
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

    spec_to_write, display_name = await _resolve_install_spec(
        ctx, name, template_id, spec
    )
    host_path_label, existing = _write_host_mcp(ctx, name, spec_to_write)
    if existing:
        return existing

    dm_body = _build_operator_dm_body(
        name=name,
        display_name=display_name,
        host_path=host_path_label,
    )
    try:
        envelope_id = await _send_dm_to_operator(ctx, dm_body)
    except Exception as exc:
        return (
            f"Installed {name!r} into host's {host_path_label}, "
            f"BUT sending the setup-instructions DM to "
            f"@{ctx.operator_slug} failed ({exc}). Retry by sending "
            f"this yourself:\n\n"
            f"send_message(channel='@{ctx.operator_slug}', "
            f"is_visible_to_human=True, text=<<<\n{dm_body}\n>>>)"
        )

    return (
        f"Installed {name!r} into host's {host_path_label} AND "
        f"DM'd @{ctx.operator_slug} the setup steps (envelope_id "
        f"{envelope_id}). They'll ping you when host setup is done; "
        f"call sync_host_mcp({name!r}) then refresh()."
    )


async def _resolve_install_spec(
    ctx: HostMcpContext, name: str, template_id: str, spec: dict[str, Any] | None
) -> tuple[dict[str, Any], str]:
    if not template_id:
        return _validate_adhoc_spec(spec or {}), name
    try:
        template = await ctx.http_client.get(f"/v2/mcp-templates/{template_id}")
    except Exception as exc:
        raise RuntimeError(
            f"install_host_mcp: catalog fetch failed for {template_id!r}: {exc}"
        ) from exc
    if not isinstance(template, dict):
        raise RuntimeError(
            f"install_host_mcp: catalog returned a non-object body for {template_id!r}"
        )
    normalized = _spec_from_template(template)
    if normalized is None:
        raise RuntimeError(
            f"install_host_mcp: catalog entry for {template_id!r} has an unsupported transport or is missing a required field"
        )
    return normalized, str(template.get("name") or name)


def _write_host_mcp(
    ctx: HostMcpContext, name: str, spec: dict[str, Any]
) -> tuple[str, str | None]:
    if ctx.harness == "codex":
        path, label, existing = (
            _codex_host_config_path(ctx.host_home),
            "~/.codex/config.toml",
            _read_codex_mcp_servers(_codex_host_config_path(ctx.host_home)),
        )
        if name in existing:
            return (
                label,
                f"{name!r} is already registered in the host's {label} — left untouched. Call sync_host_mcp({name!r}) to pick up the operator's populated entry.",
            )
        try:
            _append_codex_mcp_block(path, name, spec)
        except OSError as exc:
            raise RuntimeError(
                f"install_host_mcp: failed to write host's {label}: {exc}"
            ) from exc
        return label, None
    path, label, data = (
        ctx.host_home / ".claude.json",
        "~/.claude.json",
        _read_claude_json(ctx.host_home / ".claude.json"),
    )
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if name in servers:
        return (
            label,
            f"{name!r} is already registered in the host's {label} — left untouched. Call sync_host_mcp({name!r}) to pull the operator's populated entry into your own config.",
        )
    servers[name] = spec
    data["mcpServers"] = servers
    try:
        _atomic_write_claude_json(path, data)
    except OSError as exc:
        raise RuntimeError(
            f"install_host_mcp: failed to write host's {label}: {exc}"
        ) from exc
    return label, None


async def sync(ctx: HostMcpContext, *, template_id: str) -> str:
    """Mirror the operator's host MCP entry into the agent's config.
    Codex skips the agent-side write — the worker re-merges host's
    ``~/.codex/config.toml`` on every restart, so refresh() already
    picks it up."""
    _require_supported_harness(ctx, "sync_host_mcp")
    if not template_id or not isinstance(template_id, str):
        raise RuntimeError("sync_host_mcp: template_id is required")

    if ctx.harness == "codex":
        host_path = _codex_host_config_path(ctx.host_home)
        host_servers = _read_codex_mcp_servers(host_path)
        if template_id not in host_servers:
            return (
                f"sync_host_mcp: no entry for {template_id!r} in the "
                f"host's ~/.codex/config.toml. Call "
                f"install_host_mcp({template_id!r}) first and DM the "
                f"operator the setup steps."
            )
        return (
            f"Verified host's ~/.codex/config.toml has {template_id!r}. "
            f"Call refresh() — your codex worker re-merges the host's "
            f"mcp_servers into your own config on every restart, so "
            f"the new entry will be live immediately."
        )

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


async def request_leave(
    ctx: HostMcpContext,
    *,
    kind: str,
    space_id: str,
    channel_id: str,
    reason: str,
) -> str:
    """Agent asked to leave a space/channel — hand off to the daemon's
    message client, which DMs the operator for y/n and signs the leave
    only on approval. ``kind`` is ``leave_space`` / ``leave_channel``."""
    client = ctx.message_client
    if client is None:
        raise RuntimeError("agent isn't fully warm yet — try again in a moment")
    if kind not in ("leave_space", "leave_channel"):
        raise RuntimeError(f"unknown leave kind {kind!r}")
    if not space_id:
        raise RuntimeError("space_id is required")
    if kind == "leave_channel" and not channel_id:
        raise RuntimeError("channel_id is required for leave_channel")
    return await client.request_leave_approval(
        kind=kind,
        space_id=space_id,
        channel_id=channel_id,
        reason=reason or "",
    )


async def send_message(
    ctx: HostMcpContext,
    *,
    channel: str,
    text: str = "",
    paths: list[str] | None = None,
    caption: str = "",
    root_id: str = "",
    visibility_level: str = "default",
    send_anyway: bool = False,
) -> dict[str, Any]:
    """Dispatch a semantic model send through the worker-owned coordinator."""
    coordinator = ctx.send_coordinator
    if coordinator is None:
        return failed_result(
            "persistent send coordinator is unavailable",
            kind="coordinator_unavailable",
        )
    request = SemanticSendRequest(
        destination=str(channel or ""),
        text=str(text or ""),
        attachment_paths=tuple(paths or ()),
        caption=str(caption or ""),
        root_id=str(root_id or ""),
        visibility_level=str(visibility_level or "default"),
        send_anyway=send_anyway is True,
    )
    result = await coordinator.send(request)
    if not isinstance(result, dict):
        return failed_result(
            "persistent send coordinator returned a malformed result",
            kind="protocol",
        )
    result.setdefault("attempted", True)
    if result.get("state") == "held":
        runtime = getattr(ctx.message_client, "global_runtime", None)
        if runtime is not None:
            result = await runtime.stage_held_send_result(
                result,
                tool_name=(
                    "send_message_with_attachments"
                    if request.attachment_paths
                    else "send_message"
                ),
                tool_arguments=request.to_tool_arguments(),
            )
    return result


async def stage_model_visible_read(
    ctx: HostMcpContext,
    *,
    space_id: str,
    channel_id: str,
    through_seq: int,
    through_envelope_id: str,
    tool_name: str,
    tool_arguments: dict[str, object],
    visible_message_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Correlate a local-history tool result with the live provider turn."""
    runtime = getattr(ctx.message_client, "global_runtime", None)
    if runtime is None:
        raise RuntimeError("global message runtime is unavailable")
    return await runtime.stage_model_visible_read(
        space_id=space_id,
        channel_id=channel_id,
        through_seq=through_seq,
        through_envelope_id=through_envelope_id,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        visible_message_ids=visible_message_ids,
    )


async def read_inbox(
    ctx: HostMcpContext,
    *,
    target: str = "",
    cursor: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    runtime = getattr(ctx.message_client, "global_runtime", None)
    if runtime is None:
        raise RuntimeError("global Inbox runtime is unavailable")
    arguments: dict[str, object] = {}
    if target:
        arguments["target"] = target
    if cursor:
        arguments["cursor"] = cursor
    if limit != 50:
        arguments["limit"] = limit
    return await runtime.read_inbox(
        target=target,
        cursor=cursor,
        limit=limit,
        tool_arguments=arguments,
    )


async def create_reminder(
    ctx: HostMcpContext,
    *,
    content: str,
    target: str,
    intended_at: str,
) -> dict[str, object]:
    """Resolve reminder creation against the warm worker's one Inbox runtime."""
    runtime = getattr(ctx.message_client, "global_runtime", None)
    if runtime is None:
        raise RuntimeError("global Inbox runtime is unavailable")
    return await runtime.create_reminder(
        content=content,
        target=target,
        intended_at=intended_at,
    )


async def list_reminders(
    ctx: HostMcpContext,
    *,
    state: str = "",
    limit: int = 50,
) -> dict[str, object]:
    runtime = getattr(ctx.message_client, "global_runtime", None)
    if runtime is None:
        raise RuntimeError("global Inbox runtime is unavailable")
    return await runtime.list_reminders(state=state, limit=limit)


async def cancel_reminder(
    ctx: HostMcpContext,
    *,
    reminder_id: str,
) -> dict[str, object]:
    runtime = getattr(ctx.message_client, "global_runtime", None)
    if runtime is None:
        raise RuntimeError("global Inbox runtime is unavailable")
    return await runtime.cancel_reminder(reminder_id=reminder_id)


async def request_command_permission(
    ctx: HostMcpContext,
    *,
    tool_name: str,
    summary: str,
    timeout_s: object,
) -> str:
    """Hand a hook's permission ask to the message client; blocks
    until y/n or timeout. Returns allow/deny/timeout."""
    client = ctx.message_client
    if client is None:
        raise RuntimeError("agent isn't fully warm yet — try again in a moment")
    if not tool_name:
        raise RuntimeError("tool_name is required")
    try:
        timeout = int(timeout_s) if timeout_s is not None else 300
    except (TypeError, ValueError):
        timeout = 300
    timeout = max(5, min(timeout, 3600))
    return await client.request_command_permission(
        tool_name=str(tool_name),
        summary=str(summary or ""),
        timeout_s=timeout,
    )
