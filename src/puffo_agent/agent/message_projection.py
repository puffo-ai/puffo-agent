"""Presentation-only projection for locally decrypted conversation rows."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


CONTEXT_VERSION = 1


def _value(message: Any, name: str, default: Any = "") -> Any:
    return (
        message.get(name, default)
        if isinstance(message, Mapping)
        else getattr(message, name, default)
    )


def _content(message: Any) -> Mapping[str, Any]:
    content = _value(message, "content", {})
    return content if isinstance(content, Mapping) else {}


def _normalized_slug(value: Any) -> str:
    """Normalize the runtime's slug-shaped identities for comparison."""
    return str(value or "").strip().lstrip("@").lower()


def _quoted(value: Any) -> str:
    """Quote readable annotations without letting them alter the row grammar."""
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return f'"{escaped}"'


def _json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _annotation(label: str, value: Any) -> str:
    return f" {label}={_quoted(value)}" if value not in (None, "") else ""


def message_text(message: Any) -> str:
    content = _value(message, "content", "")
    if isinstance(content, Mapping):
        return str(content.get("text") or content.get("caption") or "")
    return str(content or "")


def _content_field(value: Any) -> str:
    """Keep untrusted prose inside one JSON value, outside row grammar."""
    text = "" if value is None else str(value)
    return f"content={_json(text)}"


def _dm_peer(message: Any, current_agent_aliases: Sequence[str] = ()) -> str:
    sender = str(_value(message, "sender_slug") or "")
    recipient = str(_value(message, "recipient_slug") or "")
    aliases = {_normalized_slug(alias) for alias in current_agent_aliases if alias}
    if _normalized_slug(sender) in aliases and recipient:
        return recipient.lstrip("@")
    return (sender or recipient or "unknown").lstrip("@")


def canonical_target_parts(message: Any) -> tuple[str, ...]:
    """Return the one durable routing projection used by every Inbox layer."""
    if str(_value(message, "envelope_kind", "")) == "dm":
        return (
            "dm",
            str(_value(message, "sender_slug", "") or "")
            or str(_value(message, "recipient_slug", "") or ""),
        )
    space_id = str(_value(message, "space_id", "") or "")
    channel_id = str(_value(message, "channel_id", "") or "")
    root_id = _effective_thread_root(message)
    if root_id:
        return ("thread", space_id, channel_id, root_id)
    return ("channel", space_id, channel_id)


def target_label(
    message: Any,
    *,
    current_agent_aliases: Sequence[str] = (),
    thread_root_id: str = "",
) -> str:
    """Return the complete canonical target header, including durable IDs."""
    content = _content(message)
    channel_id = str(_value(message, "channel_id") or "")
    if not channel_id or str(_value(message, "envelope_kind", "")) == "dm":
        peer = _dm_peer(message, current_agent_aliases)
        return (
            f"context context_version={CONTEXT_VERSION} "
            f"target_type=\"dm\" target_ref={_quoted(f'dm:{peer}')} "
            f"peer_identity={_quoted('@' + peer)}"
        )
    space_id = str(_value(message, "space_id") or "")
    effective_thread_root_id = _effective_thread_root(
        message, explicit_thread_root_id=thread_root_id,
    )
    target_type = "thread" if effective_thread_root_id else "channel"
    ref = f"channel:{space_id}:{channel_id}"
    if effective_thread_root_id:
        ref += f":thread:{effective_thread_root_id}"
    header = (
        f"context context_version={CONTEXT_VERSION} "
        f"target_type={_quoted(target_type)} target_ref={_quoted(ref)} "
        f"space_id={_quoted(space_id)}"
    )
    header += _annotation("space_name", content.get("space_name"))
    header += f" channel_id={_quoted(channel_id)}"
    channel_name = content.get("channel_name")
    if channel_name:
        header += _annotation("channel_name", str(channel_name).lstrip("#"))
    if target_type == "thread":
        header += f" thread_root_id={_quoted(effective_thread_root_id)}"
    return header


def sender_type(message: Any, *, current_agent_aliases: Sequence[str] = ()) -> str:
    content = _content(message)
    explicit = content.get("sender_type") or _value(message, "sender_type", "")
    if explicit in {"human", "agent", "system"}:
        return str(explicit)
    if (
        content.get("sender_is_agent")
        or content.get("sender_is_bot")
        or content.get("sender_owner_slug")
        or _value(message, "sender_is_agent", False)
        or _value(message, "sender_is_bot", False)
    ):
        return "agent"
    if str(_value(message, "envelope_kind", "")) in {"system", "runtime"}:
        return "system"
    if (
        content.get("is_from_operator")
        or content.get("sender_is_human")
        or _value(message, "sender_is_human", False)
    ):
        return "human"
    aliases = {_normalized_slug(alias) for alias in current_agent_aliases if alias}
    if _normalized_slug(_value(message, "sender_slug")) in aliases:
        return "agent"
    return "unknown"


def _author(message: Any) -> tuple[str, str, str]:
    content = _content(message)
    slug = str(_value(message, "sender_slug") or "unknown").lstrip("@")
    name = str(content.get("sender_display_name") or "")
    owner_slug = str(content.get("sender_owner_slug") or "").lstrip("@")
    return slug, name, owner_slug


def iso_time(message: Any) -> str:
    raw = _value(message, "sent_at", None)
    try:
        timestamp = datetime.fromtimestamp(float(raw) / 1000, tz=timezone.utc)
        return timestamp.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z",
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown-time"


def _attachment_paths(message: Any) -> list[str]:
    attachments = _content(message).get(
        "attachment_paths", _content(message).get("attachments"),
    )
    if isinstance(attachments, Sequence) and not isinstance(attachments, (str, bytes)):
        return [str(path) for path in attachments if path not in (None, "")]
    return []


def _mentions(message: Any) -> list[dict[str, Any]]:
    raw = _content(message).get("mentions")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    mentions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        slug = str(item.get("username") or item.get("slug") or "").lstrip("@")
        if not slug:
            continue
        agent_marker = item.get("is_agent", item.get("is_bot"))
        identity_type = (
            "agent" if agent_marker is True
            else "human" if agent_marker is False
            else "unknown"
        )
        mentions.append(
            {
                "identity": f"@{slug}",
                "identity_type": identity_type,
                "self": bool(item.get("is_self")),
            }
        )
    return mentions


def _reminder_event(message: Any) -> Mapping[str, Any] | None:
    """Return a locally durable reminder event without reclassifying messages."""
    content = _content(message)
    return content if content.get("event_type") == "reminder" else None


def format_reminder_event(message: Any) -> str:
    """Project a reminder fact without sender prose or behavioral advice."""
    event = _reminder_event(message)
    if event is None:
        raise ValueError("message is not a reminder event")
    fields = [
        f"context_version={CONTEXT_VERSION}",
        "event_type=\"reminder\"",
        f"event_id={_quoted(_value(message, 'envelope_id', 'unknown'))}",
        f"reminder_id={_quoted(event.get('reminder_id', ''))}",
        f"occurrence_id={_quoted(event.get('occurrence_id', ''))}",
        f"target_ref={_quoted(event.get('target', ''))}",
        f"intended_at={_quoted(event.get('intended_at', ''))}",
        f"actual_fire_at={_quoted(event.get('actual_fire_at', ''))}",
    ]
    return f"[event {' '.join(fields)}]\n{_content_field(event.get('content', ''))}"


def format_membership_event(message: Any) -> str:
    event = _content(message)
    fields = [
        f"context_version={CONTEXT_VERSION}",
        f"event_type={_quoted(event.get('event_type', 'membership'))}",
        f"event_id={_quoted(_value(message, 'envelope_id', 'unknown'))}",
        f"occurred_at={_quoted(iso_time(message))}",
        f"subject_ref={_quoted(event.get('subject_ref', ''))}",
        f"actor_identity={_quoted('@' + str(event.get('actor_slug', '')).lstrip('@'))}",
    ]
    for label, key, prefix in (
        ("actor_display_name", "actor_display_name", ""),
        ("inviter_identity", "inviter_slug", "@"),
        ("initiator_identity", "initiator_slug", "@"),
    ):
        value = str(event.get(key) or "")
        if value:
            fields.append(f"{label}={_quoted(prefix + value.lstrip('@'))}")
    return f"[event {' '.join(fields)}]\n{_content_field(message_text(message))}"


def _event_type(message: Any) -> str:
    return str(_content(message).get("event_type") or "")


def _effective_thread_root(
    message: Any, *, explicit_thread_root_id: str = "",
) -> str:
    if explicit_thread_root_id:
        return explicit_thread_root_id
    root = str(_value(message, "thread_root_id", "") or "")
    envelope_id = str(_value(message, "envelope_id", "") or "")
    if root != envelope_id:
        return root
    event_type = _event_type(message)
    is_non_replyable_event = event_type.startswith(
        ("channel_member_", "space_member_"),
    )
    is_intro_prompt = (
        str(_value(message, "sender_slug", "")) == "system"
        and envelope_id.startswith("intro-prompt-")
    )
    return "" if is_non_replyable_event or is_intro_prompt else root


def format_message_row(
    message: Any,
    *,
    current_agent_aliases: Sequence[str] = (),
    reply_count: int | None = None,
) -> str:
    if _reminder_event(message) is not None:
        return format_reminder_event(message)
    if _event_type(message).startswith(("channel_member_", "space_member_")):
        return format_membership_event(message)
    seq = _value(message, "server_seq", None)
    seq_text = str(seq) if isinstance(seq, int) and not isinstance(seq, bool) else "null"
    aliases = {_normalized_slug(alias) for alias in current_agent_aliases if alias}
    author, display_name, owner_slug = _author(message)
    encrypted = bool(_value(message, "is_encrypted", True))
    fields = [
        f"context_version={CONTEXT_VERSION}",
        f"seq={seq_text}",
        f"sent_at={_quoted(iso_time(message))}",
        f"message_id={_quoted(_value(message, 'envelope_id', 'unknown'))}",
        f"sender_identity={_quoted('@' + author)}",
        "sender_type="
        f"{_quoted(sender_type(message, current_agent_aliases=current_agent_aliases))}",
        f"self={'true' if _normalized_slug(author) in aliases else 'false'}",
        f"encrypted={'true' if encrypted else 'false'}",
    ]
    if display_name:
        fields.append(f"sender_display_name={_quoted(display_name)}")
    if owner_slug:
        fields.append(f"sender_owner_identity={_quoted('@' + owner_slug)}")
    human_visible = _content(message).get("is_visible_to_human")
    if isinstance(human_visible, bool):
        fields.append(f"human_visible={'true' if human_visible else 'false'}")
    attachment_paths = _attachment_paths(message)
    if attachment_paths:
        fields.append(f"attachment_paths={_json(attachment_paths)}")
    mentions = _mentions(message)
    if mentions:
        fields.append(f"mentions={_json(mentions)}")
    if reply_count is not None:
        fields.append(f"reply_count={reply_count}")
    return f"[message {' '.join(fields)}]\n{_content_field(message_text(message))}"


def target_ref(
    message: Any,
    *,
    current_agent_aliases: Sequence[str] = (),
    thread_root_id: str = "",
) -> str:
    channel_id = str(_value(message, "channel_id") or "")
    if not channel_id or str(_value(message, "envelope_kind", "")) == "dm":
        return f"dm:{_dm_peer(message, current_agent_aliases)}"
    space_id = str(_value(message, "space_id") or "")
    root = _effective_thread_root(
        message, explicit_thread_root_id=thread_root_id,
    )
    base = f"channel:{space_id}:{channel_id}"
    return f"{base}:thread:{root}" if root else base


def identity_projection(
    slug: Any,
    *,
    display_name: Any = None,
    identity_type: Any = "unknown",
    owner_slug: Any = None,
    role: Any = None,
    self_identity: bool = False,
    **details: Any,
) -> dict[str, Any]:
    """Stable model-facing identity facts shared by identity tools."""
    normalized = str(slug or "unknown").lstrip("@")
    owner = str(owner_slug or "").lstrip("@")
    result: dict[str, Any] = {
        "identity": f"@{normalized}",
        "display_name": str(display_name) if display_name not in (None, "") else None,
        "identity_type": str(identity_type or "unknown"),
        "owner_identity": f"@{owner}" if owner else None,
        "role": str(role) if role not in (None, "") else None,
        "self": bool(self_identity),
    }
    result.update(details)
    return result


def space_projection(space: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "space_id": str(space.get("space_id") or ""),
        "name": str(space.get("name") or ""),
        "description": space.get("description") or None,
        "avatar_url": space.get("avatar_url") or None,
        "role": space.get("role") or None,
        "joined_at": space.get("joined_at"),
    }


def channel_projection(
    channel: Mapping[str, Any], *, space_id: str,
) -> dict[str, Any]:
    owner = str(channel.get("owner_slug") or "").lstrip("@")
    return {
        "space_id": space_id,
        "channel_id": str(channel.get("channel_id") or ""),
        "name": str(channel.get("name") or ""),
        "description": channel.get("description") or None,
        "visibility": (
            "public"
            if channel.get("is_public") is True
            else "private"
            if channel.get("is_public") is False
            else "unknown"
        ),
        "owner_identity": f"@{owner}" if owner else None,
        "created_at": channel.get("created_at"),
    }


def _chronological_key(message: Any) -> tuple[bool, int, float, str]:
    raw_seq = _value(message, "server_seq", None)
    has_seq = isinstance(raw_seq, int) and not isinstance(raw_seq, bool)
    raw_time = _value(message, "sent_at", 0)
    try:
        sent_at = float(raw_time or 0)
    except (TypeError, ValueError):
        sent_at = 0
    return (
        not has_seq,
        raw_seq if has_seq else 0,
        sent_at,
        str(_value(message, "envelope_id", "")),
    )


def format_message_group(
    messages: Iterable[Any],
    *,
    current_agent_aliases: Sequence[str] = (),
    reply_counts: Mapping[str, int] | None = None,
    thread_root_id: str = "",
    chronological: bool = False,
) -> str:
    """Group only adjacent rows with exactly the same canonical target header."""
    output: list[str] = []
    previous: str | None = None
    rows = list(messages)
    if chronological:
        rows.sort(key=_chronological_key)
    for message in rows:
        header = target_label(
            message,
            current_agent_aliases=current_agent_aliases,
            thread_root_id=thread_root_id,
        )
        if header != previous:
            output.append(f"## {header}")
            previous = header
        reply_count = (reply_counts or {}).get(str(_value(message, "envelope_id", "")))
        output.append(
            format_message_row(
                message,
                current_agent_aliases=current_agent_aliases,
                reply_count=reply_count,
            )
        )
    return "\n".join(output)
