"""MCP tools for puffo-core: signed API + E2E encrypted messages.

Wire calls follow puffo-cli's conventions: ``/certs/sync`` for
device certs, ``/spaces/<sp>/channels/<ch>/members`` for channel
members, event-stream replay for channel discovery. Host-side /
local tools live in ``host_tools.py``.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from ..crypto.encoding import base64url_decode
from ..crypto.http_client import PuffoCoreHttpClient
from ..crypto.keystore import KeyStore
from ..crypto.message import (
    RecipientDevice,
    build_supplementation_envelope,
)
from ..agent.context_controller import MODEL_VISIBLE_READ_RECEIPT_PREFIX
from ..agent._logging import log_runtime_event
from ..agent.send_coordinator import (
    SemanticSendRequest,
    SendCoordinator,
    failed_result,
)
from .data_client import DataClient, DataNotFound
from ._host_mcp import PuffoRpcClient

logger = logging.getLogger(__name__)


def _history_text(content: Any) -> str:
    """Render the human text/caption portion of stored structured content."""
    if isinstance(content, dict):
        value = content.get("text")
        if isinstance(value, str) and value:
            return value
        caption = content.get("caption")
        return caption if isinstance(caption, str) else ""
    return content if isinstance(content, str) else ""


async def _send_encryption_required(cfg, resolved_root):
    """Daemon-level send-mode decision. Data-client shims without the
    method (older harnesses) fail safe to E2EE."""
    getter = getattr(cfg.data_client, "get_send_encryption", None)
    if getter is None:
        return True
    return await getter(cfg.slug, resolved_root or None)


async def _resolve_channel_space(cfg: Any, channel_id: str) -> str:
    """Resolve ``channel_id`` → ``space_id`` from the local cache.

    The cache is filled by ``puffo_core_client._handle_event`` for
    every membership event the agent receives that carries both ids
    (``invite_to_channel`` where we're the invitee,
    ``accept_channel_invite`` where we're the signer, and
    ``create_channel`` — server only fans those to space members).
    ``mark_channel_space`` is also written synchronously inside
    ``_accept_invite`` to close the WS-echo race.

    Raises ``RuntimeError`` (which propagates to the LLM as an MCP
    tool error) on miss — that's the signal "agent has no way to
    reach this channel; you may not be a member, or the id is
    wrong." Earlier code walked ``GET /spaces`` as a fallback
    resolver, but with events feeding the cache that fallback is
    redundant and silently misleading (a hit there only proved
    access to the space, not membership in the channel).
    """
    space_id = await cfg.data_client.lookup_channel_space(channel_id)
    if not space_id:
        # Non-``ch_`` miss is almost always a bare user slug — hint at
        # the DM path instead of the membership-flavoured error below.
        if not channel_id.startswith("ch_"):
            raise RuntimeError(
                f"'{channel_id}' is not a channel id (channel ids "
                f"start with 'ch_'). If it's a user slug, prepend "
                f"'@' to DM them: send_message(channel='@{channel_id}', "
                f"...); to read a DM conversation use "
                f"get_dm_history(peer='{channel_id}'). To find a "
                f"channel id, call list_channels_in_all_spaces."
            )
        raise RuntimeError(
            f"agent has no record of channel {channel_id} — either it "
            f"isn't a channel the agent belongs to, the id is wrong, or "
            f"you were just added and the membership hasn't propagated to "
            f"this agent yet (retrying shortly resolves that last case). "
            f"Call list_channels_in_all_spaces to see the channels the "
            f"agent can currently reach."
        )
    return space_id


def _ts_to_iso(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _enc_tag(m: Any) -> str:
    """`[encrypted]`/`[plaintext]` tag; legacy rows default to encrypted."""
    return "[encrypted]" if getattr(m, "is_encrypted", True) else "[plaintext]"


async def _stage_model_visible_messages(
    cfg: PuffoCoreToolsConfig,
    messages: list[Any],
    *,
    tool_name: str,
    tool_arguments: dict[str, object],
) -> str:
    """Stage the highest channel watermark returned to the provider."""
    # Same backend order ``read_inbox`` uses: in-process tools (ws-local) hold
    # a live runtime and never an rpc_client, and ``GlobalInboxRuntime`` and
    # ``PuffoRpcClient`` expose ``stage_model_visible_read`` with the identical
    # keyword signature, so the call site below is shared.
    staging = getattr(cfg, "inbox_runtime", None)
    if staging is None:
        staging = getattr(getattr(cfg, "message_client", None), "global_runtime", None)
    if staging is None:
        staging = getattr(cfg, "rpc_client", None)
    if staging is None:
        log_runtime_event(
            logger,
            "history.read_staged",
            level=logging.DEBUG,
            agent_id=cfg.agent_id,
            agent_slug=cfg.slug,
            state="unsupported_adapter",
        )
        return ""
    candidates = [
        message
        for message in messages
        if getattr(message, "envelope_kind", "") != "dm"
        and getattr(message, "space_id", None)
        and getattr(message, "channel_id", None)
        and isinstance(getattr(message, "server_seq", None), int)
        and not isinstance(getattr(message, "server_seq", None), bool)
    ]
    if not candidates:
        state = (
            "dm_unsupported"
            if any(getattr(message, "envelope_kind", "") == "dm" for message in messages)
            else "unsequenced"
            if any(getattr(message, "server_seq", None) is None for message in messages)
            else "unsupported_history"
        )
        log_runtime_event(
            logger,
            "history.read_staged",
            level=logging.DEBUG,
            agent_id=cfg.agent_id,
            agent_slug=cfg.slug,
            state=state,
        )
        return ""
    watermark = max(candidates, key=lambda message: message.server_seq)
    if any(
        message.space_id != watermark.space_id
        or message.channel_id != watermark.channel_id
        for message in candidates
    ):
        # One continuation has one channel watermark; do not claim partial
        # visibility for a mixed-channel presentation.
        return ""
    # Only stage the exact local rows that are represented in this response.
    visible = [message.envelope_id for message in candidates]
    staged = await staging.stage_model_visible_read(
        space_id=watermark.space_id,
        channel_id=watermark.channel_id,
        through_seq=watermark.server_seq,
        through_envelope_id=watermark.envelope_id,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        visible_message_ids=visible,
    )
    receipt = staged.get("correlation_receipt")
    if not isinstance(receipt, str) or not receipt:
        raise RuntimeError("model-visible read staging returned no receipt")
    return f"[{MODEL_VISIBLE_READ_RECEIPT_PREFIX}{receipt}]"


# ── transport seam ─────────────────────────────────────────────────
#
# One helper per wire read/write. Each branches on ``cfg.keyless``: the
# keyless (T23 bridge) transport hits the unsigned, token-authed
# ``/v2/cloud-agents/*`` routes (the E2B egress proxy injects
# ``x-sandbox-token``); the native transport keeps the signed keystore
# path byte-for-byte. Kept as module-level ``_read_*``/``_send_*``
# functions to match the existing ``_resolve_channel_space`` /
# ``_fetch_device_keys`` idiom.


async def _read_spaces(cfg: Any) -> Any:
    if cfg.keyless:
        return await cfg.http_client.get_unsigned("/v2/cloud-agents/spaces")
    return await cfg.http_client.get("/spaces")


async def _read_space_channels(cfg: Any, space_id: str) -> Any:
    quoted_space_id = urllib.parse.quote(space_id, safe="")
    if cfg.keyless:
        return await cfg.http_client.get_unsigned(
            f"/v2/cloud-agents/spaces/{quoted_space_id}/channels"
        )
    return await cfg.http_client.get(f"/spaces/{quoted_space_id}/channels")


async def _read_channel_members(
    cfg: Any, space_id: str, channel_id: str,
) -> Any:
    """Read the exact channel roster on both transports."""
    quoted_space_id = urllib.parse.quote(space_id, safe="")
    quoted_channel_id = urllib.parse.quote(channel_id, safe="")
    if cfg.keyless:
        return await cfg.http_client.get_unsigned(
            "/v2/cloud-agents/spaces/"
            f"{quoted_space_id}/channels/{quoted_channel_id}/members"
        )
    return await cfg.http_client.get(
        f"/spaces/{quoted_space_id}/channels/{quoted_channel_id}/members"
    )


async def _read_profiles(cfg: Any, slugs_csv: str) -> Any:
    quoted = urllib.parse.quote(slugs_csv, safe=",")
    if cfg.keyless:
        return await cfg.http_client.get_unsigned(
            f"/v2/cloud-agents/identities/profiles?slugs={quoted}"
        )
    return await cfg.http_client.get(
        f"/identities/profiles?slugs={quoted}"
    )


async def _read_profile_map(
    cfg: Any, slugs: list[str],
) -> dict[str, dict[str, Any]]:
    """Best-effort profile enrichment without making roster reads fragile."""
    normalized = list(dict.fromkeys(slug.lstrip("@") for slug in slugs if slug))
    profiles: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(normalized), 100):
        batch = normalized[offset : offset + 100]
        try:
            data = await _read_profiles(cfg, ",".join(batch))
        except Exception as exc:  # noqa: BLE001
            logger.warning("profile enrichment failed for channel roster: %s", exc)
            continue
        rows = data.get("profiles", []) if isinstance(data, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            slug = str(row.get("slug") or "").lstrip("@")
            if slug:
                profiles[slug] = row
    return profiles


async def _send_keyless(cfg: Any, body: dict) -> dict:
    return await cfg.http_client.post_unsigned(
        "/v2/cloud-agents/messages", body,
    ) or {}


async def _upload_blob_keyless(cfg: Any, data: bytes) -> dict:
    return await cfg.http_client.post_bytes_unsigned(
        "/v2/cloud-agents/blobs/upload", data,
    ) or {}


@dataclass
class PuffoCoreToolsConfig:
    slug: str
    device_id: str
    keystore: KeyStore
    http_client: PuffoCoreHttpClient
    data_client: DataClient
    agent_id: str = ""
    space_id: Optional[str] = None
    # Workspace root used by ``send_message_with_attachments`` to
    # safety-resolve LLM-supplied relative paths (no ``..`` escape,
    # no absolutes).
    workspace: Optional[str] = None
    # None when PUFFO_RPC_URL isn't set; install/sync tools surface
    # a clear error rather than touching operator files in-process.
    rpc_client: Optional[PuffoRpcClient] = None
    # Set only on the in-process (ws-local) path, where tools run inside
    # the daemon and drive the message client directly instead of via RPC.
    message_client: Any = None
    # Package 4 wires the worker's one persistent instance. Optional keeps
    # older constructors source-compatible; sends fail closed while absent.
    send_coordinator: Any = None
    # T23 keyless bridge transport (``CloudBridgeClient``). Populated only
    # at the in-process ws-local site (from ``client._bridge``); the
    # subprocess/RPC MCP path leaves it None.
    bridge_client: Any = None
    # Live Inbox runtime for in-process tools. Subprocess tools use rpc_client.
    inbox_runtime: Any = None

    @property
    def keyless(self) -> bool:
        """Whether reads and sends use the keyless cloud-agent routes."""
        return getattr(self.http_client, "keyless", False)


async def _dispatch_semantic_send(
    cfg: PuffoCoreToolsConfig, request: SemanticSendRequest,
    *, tool_name: str = "send_message",
) -> dict[str, Any]:
    coordinator = getattr(cfg, "send_coordinator", None)
    if coordinator is None:
        coordinator = getattr(
            getattr(cfg, "message_client", None), "send_delegate", None
        )
    if coordinator is not None:
        try:
            if hasattr(coordinator, "workspace"):
                coordinator.workspace = cfg.workspace
            result = await coordinator.send(request)
        except Exception as exc:
            return failed_result(
                f"persistent send coordinator failed: {exc}",
                kind="coordinator",
            )
        if isinstance(result, dict):
            result.setdefault("attempted", True)
            runtime = getattr(cfg, "inbox_runtime", None)
            if result.get("state") == "held" and runtime is not None:
                result = await runtime.stage_held_send_result(
                    result,
                    tool_name=tool_name,
                    tool_arguments=request.to_tool_arguments(),
                )
            return result
        return failed_result(
            "persistent send coordinator returned a malformed result",
            kind="protocol",
        )


    rpc = getattr(cfg, "rpc_client", None)
    if rpc is not None:
        try:
            result = await rpc.send_message(**request.to_rpc_dict())
        except Exception as exc:
            return failed_result(f"send RPC unavailable: {exc}", kind="rpc_unavailable")
        if isinstance(result, dict):
            result.setdefault("attempted", True)
            return result
        return failed_result("send RPC returned a malformed result", kind="protocol")
    if cfg.keyless:
        coordinator = SendCoordinator(
            slug=cfg.slug,
            keystore=cfg.keystore,
            http_client=cfg.http_client,
            data_client=cfg.data_client,
            workspace=cfg.workspace,
        )
        return await coordinator.send(request)
    return failed_result(
        "persistent send coordinator is unavailable",
        kind="coordinator_unavailable",
    )

def _note_contact(
    cfg: PuffoCoreToolsConfig, slug: str, *,
    allowed: bool = False, blocked: Optional[bool] = None,
) -> None:
    """Reflect an allowlist/blocklist write into the in-process contact
    cache when the tool runs inside the daemon (ws-local). Out-of-process
    runtimes (cli-local) pick it up via the cache's TTL / miss refresh."""
    mc = getattr(cfg, "message_client", None)
    contacts = getattr(mc, "_contacts", None) if mc is not None else None
    if contacts is None:
        return
    if allowed:
        contacts.note_allowed(slug)
    if blocked is not None:
        contacts.note_blocked(slug, blocked)


async def _fetch_device_keys(
    http_client: PuffoCoreHttpClient,
    slugs: list[str],
) -> list[RecipientDevice]:
    """Paginate ``/certs/sync?slugs=...`` and collect
    ``(device_id, kem_pk)`` for every returned device_cert.
    """
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
                # v2 nests under ``keys.encryption.public_key``; fall
                # back to the v1 flat field for legacy entries.
                keys_block = cert.get("keys") or {}
                enc_block = keys_block.get("encryption") or {}
                kem_b64 = enc_block.get("public_key") or cert.get("kem_public_key", "")
                if dev_id and kem_b64 and dev_id not in seen_ids:
                    try:
                        devices.append(RecipientDevice(
                            device_id=dev_id,
                            kem_public_key=base64url_decode(kem_b64),
                        ))
                        seen_ids.add(dev_id)
                    except Exception:
                        # Skip malformed entry; don't abort the fetch.
                        pass
            since = entry.get("seq", since)
        if not data.get("has_more"):
            break
    return devices


async def _supplement_missing_devices(
    http_client: PuffoCoreHttpClient,
    envelope: dict,
    content_key: bytes,
    recipient_slugs: list[str],
    missing_device_ids: list[str],
) -> None:
    """Best-effort: re-fetch certs, build a same-``envelope_id``
    envelope for the missing device_ids, POST. Logs + swallows on
    failure (original send is already durable)."""
    envelope_id = envelope.get("envelope_id", "?")
    try:
        fresh = await _fetch_device_keys(http_client, recipient_slugs)
        wanted = set(missing_device_ids)
        supp_devices = [d for d in fresh if d.device_id in wanted]
        if not supp_devices:
            logger.warning(
                "supplementation: server reported %d missing device(s) "
                "for %s but fresh /certs/sync returned none of them; "
                "those devices won't receive this message",
                len(missing_device_ids), envelope_id,
            )
            return
        supp_env = build_supplementation_envelope(
            envelope, content_key, supp_devices,
        )
        await http_client.post("/messages", supp_env)
        logger.debug(
            "supplementation: re-posted %s to %d device(s)",
            envelope_id, len(supp_devices),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "supplementation: failed for %s: %s", envelope_id, exc,
        )




_RESOLVE_ROOT_MAX_DEPTH = 8


async def _read_outgoing_root_message(
    data_client: Any,
    root_id: str,
    current: str,
) -> tuple[Any, str]:
    try:
        message = await data_client.get_message_by_envelope(current)
    except DataNotFound:
        message = None
    except Exception as exc:
        logger.warning(
            "resolve_outgoing_root: wiped %s — lookup transport error: %s",
            root_id,
            exc,
        )
        return None, (
            f"\nnote: thread_root_id {root_id} could not be verified "
            "(local cache lookup failed); sent as top-level."
        )
    if message is None:
        logger.info(
            "resolve_outgoing_root: wiped %s — %s not in local cache",
            root_id,
            current,
        )
        return None, (
            f"\nnote: thread_root_id {root_id} not in local cache; "
            "sent as top-level. Agents can only reply in threads "
            "whose root is in their own local message store."
        )
    return message, ""


def _validate_outgoing_root_scope(
    message: Any,
    root_id: str,
    *,
    self_slug: str,
    channel_id: Optional[str],
    space_id: Optional[str],
    dm_peer: Optional[str],
) -> None:
    if dm_peer is not None:
        kind = getattr(message, "envelope_kind", None)
        peer = (
            getattr(message, "recipient_slug", None)
            if getattr(message, "sender_slug", None) == self_slug
            else getattr(message, "sender_slug", None)
        )
        if kind != "dm" or peer != dm_peer:
            logger.info(
                "resolve_outgoing_root: rejected %s — not part of the "
                "DM with %s (kind=%r peer=%r)",
                root_id,
                dm_peer,
                kind,
                peer,
            )
            raise RuntimeError(
                f"thread_root_id {root_id} does not belong to this DM "
                f"with @{dm_peer}; pass a root from this conversation "
                "or omit root_id to start a new thread."
            )
        return
    message_space = getattr(message, "space_id", None)
    if message.channel_id != channel_id or (
        space_id and message_space and message_space != space_id
    ):
        logger.info(
            "resolve_outgoing_root: rejected %s — belongs to channel "
            "%r, outbound is %r",
            root_id,
            message.channel_id,
            channel_id,
        )
        raise RuntimeError(
            f"thread_root_id {root_id} belongs to channel "
            f"{message.channel_id!r}, not this send's channel "
            f"{channel_id!r}; pass a root from the current channel "
            "or omit root_id to start a new thread."
        )


async def _resolve_outgoing_root(
    root_id: str,
    data_client: Any,
    *,
    self_slug: str,
    channel_id: Optional[str],
    space_id: Optional[str],
    dm_peer: Optional[str],
) -> tuple[Optional[str], str]:
    """Resolve the agent-supplied ``root_id`` to a server-valid thread
    root for an outbound send. Returns ``(root_or_None, note)``:

      - daemon-local system envelope (sender ``system`` / self-referencing
        root) -> ``None``: the server has no such row, so the message goes
        out as a new top-level root;
      - reference from another channel/DM -> raises ``RuntimeError`` so
        the agent can correct itself;
      - same-scope reply -> walks up and returns the true root;
      - not in the local store / lookup failure -> ``None`` + note (agents
        may only thread under roots they hold locally);
      - cycle / over-deep chain (corrupt data) -> original id + warning.
    """
    if not root_id.strip():
        return None, ""

    current = root_id
    seen: set[str] = set()
    walked = False
    cycle = False
    for _ in range(_RESOLVE_ROOT_MAX_DEPTH):
        if current in seen:
            cycle = True
            break
        seen.add(current)
        msg, missing_note = await _read_outgoing_root_message(
            data_client,
            root_id,
            current,
        )
        if msg is None:
            return None, missing_note
        # Cross-scope first: rejecting before the system/self-ref wipe
        # keeps misdirected content out of the wrong conversation.
        _validate_outgoing_root_scope(
            msg,
            root_id,
            self_slug=self_slug,
            channel_id=channel_id,
            space_id=space_id,
            dm_peer=dm_peer,
        )
        parent_root = getattr(msg, "thread_root_id", None)
        if getattr(msg, "sender_slug", None) == "system" or parent_root == current:
            # Daemon-minted system envelopes have no server row — threading
            # under them would dangle. Send as a new root instead.
            logger.info(
                "resolve_outgoing_root: wiped %s — daemon-local system thread",
                root_id,
            )
            return None, (
                f"\nnote: thread_root_id {root_id} refers to a local "
                "system message that doesn't exist on the server; sent "
                "as a new top-level message."
            )
        if parent_root is None:
            if walked:
                return current, (
                    f"\nnote: root_id {root_id} was a reply, not a root — "
                    f"auto-corrected to {current}. Pass the metadata "
                    "block's thread_root_id, not post_id."
                )
            return current, ""
        walked = True
        current = parent_root

    reason = (
        "cycle detected in thread chain"
        if cycle
        else f"chain deeper than {_RESOLVE_ROOT_MAX_DEPTH} levels"
    )
    return root_id, (
        f"\nnote: could not resolve root_id {root_id} to a true thread "
        f"root ({reason}); sent as-is. The relay's thread chain looks "
        "corrupt — please flag this to the operator and pass the "
        "metadata block's thread_root_id directly."
    )


def register_core_tools(mcp: FastMCP, cfg: PuffoCoreToolsConfig) -> None:
    """Register the core MCP surface in its established public order."""
    from .core_history_tools import register_history_tools
    from .core_host_tools import register_host_tools
    from .core_identity_tools import register_identity_tools
    from .core_inbox_tools import register_inbox_tools
    from .core_message_tools import register_message_tools

    register_inbox_tools(mcp, cfg)
    register_identity_tools(mcp, cfg)
    register_message_tools(mcp, cfg)
    register_history_tools(mcp, cfg)
    register_host_tools(mcp, cfg)

    if cfg.bridge_client is not None:
        from .lifecycle_tools import register_lifecycle_tools

        register_lifecycle_tools(mcp, cfg)
