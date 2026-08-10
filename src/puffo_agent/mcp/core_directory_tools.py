"""MCP registration for directory tools."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..agent.message_projection import (
    CONTEXT_VERSION,
    channel_projection,
    identity_projection,
    space_projection,
)
from .puffo_core_tools import (
    _read_channel_members,
    _read_profile_map,
    _read_profiles,
    _read_space_channels,
    _read_spaces,
    _resolve_channel_space,
)

logger = logging.getLogger(__name__)


def register_space_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def list_spaces() -> dict[str, Any]:
        """List authoritative space memberships as structured context.

        ``GET /spaces`` is server-filtered to memberships the
        agent actually has, so the result reflects authoritative
        permissions — channels can be enumerated for any space
        listed here via ``list_channels_in_space``."""
        data = await _read_spaces(cfg)
        spaces_entries = (
            data.get("spaces", []) if isinstance(data, dict) else []
        ) or []
        spaces = [
            space_projection(space)
            for space in spaces_entries
            if isinstance(space, dict) and space.get("space_id")
        ]
        return {
            "context_version": CONTEXT_VERSION,
            "count": len(spaces),
            "spaces": spaces,
        }


def register_channel_directory_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def list_channels_in_space(space_id: str) -> dict[str, Any]:
        """List authoritative channel memberships for one space.

        ``GET /spaces/<space_id>/channels`` is server-filtered to the
        agent's actual channel memberships; this tool just formats the
        result. The legacy ``cfg.space_id`` is not consulted — pass
        the explicit ``space_id`` to scope the query. Use
        ``list_spaces`` to enumerate valid ``space_id``s first.

        Tight-race note: just after AcceptSpaceInvite the endpoint can
        briefly return the SPA-route HTML stub (decoded as ``str``)
        while the materialiser commits. Treat that as "no channels yet"
        rather than crashing the tool.
        """
        sid = (space_id or "").strip()
        if not sid:
            raise RuntimeError("space_id is required")
        data = await _read_space_channels(cfg, sid)
        channels = (data.get("channels", []) if isinstance(data, dict) else []) or []
        projected = [
            channel_projection(channel, space_id=sid)
            for channel in channels
            if isinstance(channel, dict) and channel.get("channel_id")
        ]
        return {
            "context_version": CONTEXT_VERSION,
            "space_id": sid,
            "count": len(projected),
            "channels": projected,
        }

    @mcp.tool()
    async def list_channels_in_all_spaces() -> dict[str, Any]:
        """List channels in every authoritative space membership.

        Convenience over ``list_spaces`` + ``list_channels_in_space``
        for the case where the LLM wants the full membership picture
        in one tool call. Walks one ``GET /spaces`` plus one
        ``GET /spaces/<sp>/channels`` per space."""
        spaces_data = await _read_spaces(cfg)
        spaces_entries = (
            spaces_data.get("spaces", []) if isinstance(spaces_data, dict) else []
        ) or []
        projected_spaces: list[dict[str, Any]] = []
        channel_count = 0
        for sp in spaces_entries:
            if not isinstance(sp, dict):
                continue
            space_id = sp.get("space_id", "")
            if not space_id:
                continue
            ch_data = await _read_space_channels(cfg, space_id)
            channels = (
                ch_data.get("channels", []) if isinstance(ch_data, dict) else []
            ) or []
            projected_channels = [
                channel_projection(channel, space_id=space_id)
                for channel in channels
                if isinstance(channel, dict) and channel.get("channel_id")
            ]
            channel_count += len(projected_channels)
            projected_spaces.append(
                {
                    **space_projection(sp),
                    "channels": projected_channels,
                }
            )
        return {
            "context_version": CONTEXT_VERSION,
            "space_count": len(projected_spaces),
            "channel_count": channel_count,
            "spaces": projected_spaces,
        }


def register_member_directory_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def list_channel_members(channel: str) -> dict[str, Any]:
        """List channel members as stable identity objects."""
        channel_ref = channel.strip()
        if channel_ref.startswith("#"):
            raise RuntimeError(
                "'#<name>' channel addressing isn't supported; pass the "
                "channel id directly."
            )
        channel_id = channel_ref
        # Resolve from the local channel→space cache (populated by
        # membership events). Misses raise — the previous version
        # silently used ``cfg.space_id`` (home space), which broke
        # for any channel not in the agent's home space.
        space_id = await _resolve_channel_space(cfg, channel_id)

        # Both transports read the exact channel-scoped roster. See
        # ``_read_channel_members``.
        data = await _read_channel_members(cfg, space_id, channel_id)
        roster = data.get("members", []) if isinstance(data, dict) else []
        roster = [member for member in roster if isinstance(member, dict)]
        profiles = await _read_profile_map(
            cfg,
            [str(member.get("slug") or "") for member in roster],
        )
        members = []
        for member in roster:
            slug = str(member.get("slug") or "").lstrip("@")
            if not slug:
                continue
            profile = profiles.get(slug, {})
            members.append(
                identity_projection(
                    slug,
                    display_name=profile.get("display_name"),
                    identity_type=member.get("identity_type") or "unknown",
                    owner_slug=member.get("owner_slug"),
                    role=member.get("role") or "member",
                    self_identity=slug == cfg.slug.lstrip("@"),
                )
            )
        return {
            "context_version": CONTEXT_VERSION,
            "target": {
                "target_type": "channel",
                "target_ref": f"channel:{space_id}:{channel_id}",
                "space_id": space_id,
                "channel_id": channel_id,
            },
            "count": len(members),
            "members": members,
        }


def register_profile_directory_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def get_user_info(username: str) -> dict[str, Any]:
        """Look up one identity by unique slug and return fresh profile context.

        Always fetches fresh from puffo-server (bypasses the daemon's
        TTL'd profile cache) and writes the result back to that cache
        so the next inbound message renders with the new values.
        Use this when the operator says someone renamed themselves.
        """
        slug = (username or "").lstrip("@").strip()
        if not slug:
            raise RuntimeError("username is required")
        # ``/identities/profiles?slugs=`` accepts a comma-separated
        # list; we read back the first entry. Empty list means the
        # slug isn't registered.
        data = await _read_profiles(cfg, slug)
        profiles = data.get("profiles", []) if isinstance(data, dict) else []
        if not profiles:
            return {
                "context_version": CONTEXT_VERSION,
                "found": False,
                "identity": f"@{slug}",
            }
        p = profiles[0]
        # Server returns ``display_name`` (was previously read as
        # ``username`` here, which silently dropped the field for
        # every lookup — the line was never printed).
        display_name = (p.get("display_name") or "").strip()
        avatar_url = (p.get("avatar_url") or "").strip()
        bio = (p.get("bio") or "").strip()
        # Push the just-fetched values into the daemon's profile
        # cache for this agent's view so the next render of an
        # inbound message uses the fresh display_name + avatar
        # instead of waiting for the TTL to expire.
        try:
            await cfg.data_client.update_profile_cache(
                slug,
                display_name,
                avatar_url,
            )
        except Exception as exc:
            logger.warning(
                "get_user_info: failed to refresh daemon cache for %s: %s",
                slug,
                exc,
            )
        owner_slug = p.get("owner_slug")
        identity_type = (
            "agent" if owner_slug or slug == cfg.slug.lstrip("@") else "unknown"
        )
        return {
            "context_version": CONTEXT_VERSION,
            "found": True,
            "identity": identity_projection(
                p.get("slug", slug),
                display_name=display_name,
                identity_type=identity_type,
                owner_slug=owner_slug,
                role=p.get("role"),
                self_identity=slug == cfg.slug.lstrip("@"),
                role_short=p.get("role_short") or None,
                bio=bio or None,
                avatar_url=avatar_url or None,
                profile_updated_at=p.get("profile_updated_at"),
            ),
        }


def register_directory_tools(mcp: FastMCP, cfg: Any) -> None:
    """Register directory tools in their established public order."""
    register_space_tools(mcp, cfg)
    register_channel_directory_tools(mcp, cfg)
    register_member_directory_tools(mcp, cfg)
    register_profile_directory_tools(mcp, cfg)
