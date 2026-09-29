"""Normalize copied Inbox targets at the shared semantic send boundary."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .message_store_models import parse_inbox_target
from .send_models import SemanticSendRequest


async def normalize_send_target(
    request: SemanticSendRequest, data_client: Any,
) -> SemanticSendRequest:
    """Preserve space/thread intent before uploads, freshness checks, or sends."""
    target = request.destination.strip()
    if not target.startswith(("dm:", "channel:")):
        return request
    kind, space_id, channel_id, peer_or_root = parse_inbox_target(target)
    if kind == "dm":
        if any(char.isspace() or char in "@/\\" for char in peer_or_root):
            raise ValueError("dm:<peer> requires a bare user slug without @ or whitespace")
        return replace(request, destination=f"@{peer_or_root}")
    if peer_or_root and request.root_id and peer_or_root != request.root_id:
        raise ValueError("root_id conflicts with the thread in channel target_ref")
    known_space = await data_client.lookup_channel_space(channel_id)
    if known_space != space_id:
        raise ValueError(
            "channel target_ref space does not match the agent's channel record; "
            "use list_channels_in_all_spaces to verify the destination"
        )
    return replace(
        request, destination=channel_id, root_id=peer_or_root or request.root_id,
        _require_thread=bool(peer_or_root),
    )


async def resolve_send_root(
    request: SemanticSendRequest, data_client: Any, *, self_slug: str,
    channel_id: str | None, space_id: str | None, dm_peer: str | None,
) -> tuple[str | None, str]:
    """Enforce canonical thread intent at every actual send preparation."""
    from ..mcp.puffo_core_tools import _resolve_outgoing_root

    root, note = await _resolve_outgoing_root(
        request.root_id, data_client, self_slug=self_slug,
        channel_id=channel_id, space_id=space_id, dm_peer=dm_peer,
    )
    if request._require_thread and not root:
        raise ValueError(
            "cannot verify the thread in channel target_ref; read the thread "
            "before retrying, or explicitly choose a channel-level destination"
        )
    return root, note
