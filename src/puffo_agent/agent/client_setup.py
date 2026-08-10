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
        "_connected_callbacks": [],
        "_ack_tasks": set(),
        "_bridge_pending_nonprogress": False,
        "global_runtime": None,
        "_legacy_dm_peer": "",
        "_last_dm_sender": "",
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
        "_pending_command_permissions": {},
        "_timed_out_command_permissions": {},
    }


def _cache_state() -> dict[str, Any]:
    return {
        "_channel_space": {},
        "_rewarm_lock": asyncio.Lock(),
        "_last_rewarm": 0.0,
        "_warm_task": None,
        "_stale_report_buf": [],
        "_stale_flush_task": None,
        "_space_name_cache": {},
        "_channel_name_cache": {},
        "_space_members": {},
    }


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
    state["_contacts"] = ContactCache(http_client, state["_log"])
    return state
