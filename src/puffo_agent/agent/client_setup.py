"""Construction-only state assembly for :mod:`puffo_core_client`."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..limits import (
    DEFAULT_CATCHUP_STALE_HOURS,
    MAX_INLINE_MESSAGE_CHARS,
    MESSAGE_SEGMENT_CHARS,
)
from ..portal.state import agent_dir
from .client_support import AgentLogger, DeviceKeyCache
from .contact_cache import ContactCache
from .dm_approvals import load_pending_dm_approvals
from .inbound_attachments import _DEFAULT_IMAGE_EDGE_PX


def _configuration_state(
    *,
    agent_id: str,
    slug: str,
    device_id: str,
    space_id: str,
    operator_slug: str,
    auto_accept_space_invitations: bool,
    auto_accept_dm: bool,
    workspace: str,
    max_inline_chars: int,
    segment_chars: int,
    agent_created_at: int,
    image_edge_px: int,
    catchup_stale_hours: float,
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "slug": slug,
        "device_id": device_id,
        "space_id": space_id,
        "_agent_created_at": int(agent_created_at),
        "operator_slug": operator_slug,
        "auto_accept_space_invitations": bool(auto_accept_space_invitations),
        "auto_accept_dm": bool(auto_accept_dm),
        "workspace": workspace,
        "_max_inline_chars": max(1, int(max_inline_chars)),
        "_segment_chars": max(1, int(segment_chars)),
        "_catchup_stale_ms": (
            int(catchup_stale_hours * 3600 * 1000) if catchup_stale_hours > 0 else 0
        ),
        "_image_edge_px": int(image_edge_px) or _DEFAULT_IMAGE_EDGE_PX,
    }


def _transport_state(*, http_client: Any, bridge_client: Any) -> dict[str, Any]:
    return {
        "http": http_client,
        "_key_cache": DeviceKeyCache(http_client),
        "_ws": None,
        "_bridge": bridge_client,
        "_processing_reports": None,
        "_connected_callbacks": [],
        "_ack_tasks": set(),
        "_bridge_pending_nonprogress": False,
        "global_runtime": None,
        "_legacy_dm_peer": "",
        "_last_dm_sender": "",
        # The connection-owned keyless invitation poller (see
        # ``bridge_transport.listen_bridge``); one fresh task per connect.
        "_keyless_invite_poll_task": None,
    }


def _membership_state(slug: str) -> dict[str, Any]:
    return {
        "_operator_root_pubkey": None,
        "_inviter_root_cache": {},
        "_profile_cache": {},
        "_owner_slug_cache": {},
        "_processed_invite_ids": set(),
        "_processed_membership_event_ids": set(),
        "_inviter_by_invitation_event_id": {},
        "_pending_invite_dms": {},
        "_pending_leave_dms": {},
        "_gate_left_spaces": set(),
        "_pending_dm_approvals": load_pending_dm_approvals(slug),
        "_keyless_dm_approval_lock": asyncio.Lock(),
        "_pending_command_permissions": {},
        "_timed_out_command_permissions": {},
    }


def _cache_state() -> dict[str, Any]:
    return {
        "_channel_space": {},
        "_rewarm_lock": asyncio.Lock(),
        "_last_rewarm": 0.0,
        "_warm_task": None,
        "_space_name_cache": {},
        "_channel_name_cache": {},
        "_channel_encrypted": {},
        "_space_members": {},
    }


def _keyless_contact_state_path(slug: str) -> Any:
    """Per-Agent local contact state for a keyless transport.

    A keyless agent can never hydrate the signed ``/allowlists`` and
    ``/blocklists`` routes, so its ``ContactCache`` answers (and persists)
    a small per-Agent JSON allow/block set instead. Signed clients never
    pass a path and keep today's server-hydration / in-memory behavior.
    """
    return agent_dir(slug) / ".puffo-agent" / "keyless_contacts.json"


def _contacts(
    http_client: Any,
    log: Any,
    *,
    slug: str,
) -> ContactCache:
    keyless = bool(getattr(http_client, "keyless", False))
    return ContactCache(
        http_client,
        log,
        local_state_path=_keyless_contact_state_path(slug) if keyless else None,
    )


def _bridge_invite_prompt_send(bridge: Any, operator_slug: str):
    """The durable flow's DM-prompt lane over a real bridge transport.

    A keyless bridge prompt is a plain ``send_send`` DM to the configured
    operator — never a signed HTTP route. The returned correlated ack is the
    flow's prompt identity, which the operator's threaded reply echoes back.
    """

    async def send(prompt: str, *, client_ref: str) -> dict:
        return await bridge.send_send(
            plaintext=prompt,
            recipient_slug=operator_slug,
            client_ref=client_ref,
        )

    return send


def _keyless_invitation_flow(
    *,
    slug: str,
    bridge_client: Any,
    operator_slug: str,
    auto_accept_space_invitations: bool,
):
    """Build exactly one invitation flow for a real keyless bridge client.

    Native clients keep no flow and never touch the signed HTTP invitation
    path. The flow loads its durable per-Agent invitation state here, so a
    reconnect resumes the same records instead of minting a second flow.
    """
    if bridge_client is None:
        return None
    from .keyless_invitation_flow import KeylessInvitationFlow

    return KeylessInvitationFlow(
        slug=slug,
        bridge=bridge_client,
        operator_slug=operator_slug,
        send_dm=_bridge_invite_prompt_send(bridge_client, operator_slug),
        auto_accept_space_invitations=auto_accept_space_invitations,
    )


def initial_client_state(
    *,
    agent_id: str = "",
    slug: str,
    device_id: str,
    space_id: str,
    keystore: Any,
    http_client: Any,
    message_store: Any,
    operator_slug: str = "",
    auto_accept_space_invitations: bool = False,
    auto_accept_dm: bool = False,
    workspace: str = "",
    max_inline_chars: int = MAX_INLINE_MESSAGE_CHARS,
    segment_chars: int = MESSAGE_SEGMENT_CHARS,
    agent_created_at: int = 0,
    image_edge_px: int = _DEFAULT_IMAGE_EDGE_PX,
    catchup_stale_hours: float = DEFAULT_CATCHUP_STALE_HOURS,
    bridge_client: Any = None,
) -> dict[str, Any]:
    """Build every instance field without changing the client facade."""
    state = _configuration_state(
        agent_id=agent_id,
        slug=slug,
        device_id=device_id,
        space_id=space_id,
        operator_slug=operator_slug,
        auto_accept_space_invitations=auto_accept_space_invitations,
        auto_accept_dm=auto_accept_dm,
        workspace=workspace,
        max_inline_chars=max_inline_chars,
        segment_chars=segment_chars,
        agent_created_at=agent_created_at,
        image_edge_px=image_edge_px,
        catchup_stale_hours=catchup_stale_hours,
    )
    state.update(_transport_state(http_client=http_client, bridge_client=bridge_client))
    state.update(_membership_state(slug))
    state.update(_cache_state())
    state["keystore"] = keystore
    state["store"] = message_store
    state["_log"] = AgentLogger(
        logging.getLogger("puffo_agent.agent.puffo_core_client"), {"agent": slug}
    )
    state["_contacts"] = _contacts(http_client, state["_log"], slug=slug)
    state["_keyless_invitation_flow"] = _keyless_invitation_flow(
        slug=slug,
        bridge_client=bridge_client,
        operator_slug=operator_slug,
        auto_accept_space_invitations=auto_accept_space_invitations,
    )
    return state
