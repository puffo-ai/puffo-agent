"""Formatting and persistence payloads for membership announcements."""

from __future__ import annotations

from typing import Any


def identity_label(slug: str, display_name: str) -> str:
    if display_name:
        return f"**{display_name}**(@{slug})"
    return f"@{slug}"


def operator_label(slug: str, display_name: str) -> str:
    if not slug:
        return "an operator"
    return identity_label(slug, display_name)


def membership_body(
    *,
    action: str,
    actor_label: str,
    channel_name: str,
    space_label: str,
    inviter_label: str = "",
    kicker_label: str = "",
) -> str | None:
    invited_by = f" (invited by {inviter_label})" if inviter_label else ""
    if action == "joined":
        return f"{actor_label} joined channel #{channel_name}{invited_by}."
    if action == "left":
        return f"{actor_label} left channel #{channel_name}."
    if action == "removed":
        return (
            f"{actor_label} was removed from channel #{channel_name} by {kicker_label}."
        )
    if action == "joined_space":
        return f"{actor_label} joined space {space_label}{invited_by}."
    if action == "left_space":
        return f"{actor_label} left space {space_label}."
    if action == "removed_from_space":
        return f"{actor_label} was removed from space {space_label} by {kicker_label}."
    return None


def membership_store_payload(
    *,
    action: str,
    actor_slug: str,
    actor_display_name: str,
    body: str,
    inviter_slug: str,
    initiator_slug: str,
    channel_id: str,
    channel_name: str,
    space_id: str,
    space_name: str,
    event_id: str,
    now_ms: int,
) -> tuple[str, dict[str, Any]]:
    """Return the deterministic envelope id and local-store payload."""
    suffix = event_id or str(now_ms)
    envelope_id = f"membership-{action}-{channel_id}-{actor_slug}-{suffix}"
    event_types = {
        "joined": "channel_member_joined",
        "left": "channel_member_left",
        "removed": "channel_member_removed",
        "joined_space": "space_member_joined",
        "left_space": "space_member_left",
        "removed_from_space": "space_member_removed",
    }
    subject_ref = (
        f"space:{space_id}"
        if action.endswith("_space")
        else f"channel:{space_id}:{channel_id}"
    )
    return envelope_id, {
        "envelope_id": envelope_id,
        "envelope_kind": "channel",
        "sender_slug": "system",
        "channel_id": channel_id,
        "space_id": space_id,
        "content_type": "application/vnd.puffo.runtime-event+json",
        "content": {
            "event_type": event_types[action],
            "text": body,
            "actor_slug": actor_slug,
            "actor_display_name": actor_display_name,
            "inviter_slug": inviter_slug,
            "initiator_slug": initiator_slug,
            "subject_ref": subject_ref,
            "space_id": space_id,
            "space_name": space_name,
            "channel_id": channel_id,
            "channel_name": channel_name,
        },
        "sent_at": now_ms,
        "thread_root_id": envelope_id,
        "reply_to_id": None,
    }
