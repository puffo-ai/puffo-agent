"""Invite acceptance, introductions, and membership announcement actions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..crypto.keystore import decode_secret
from ..crypto.primitives import Ed25519KeyPair
from .event_kinds import EventKind
from .events import random_nonce, sign_event
from .membership_messages import (
    identity_label,
    membership_body,
    membership_store_payload,
    operator_label,
)

Log = logging.Logger | logging.LoggerAdapter
AsyncCall = Callable[..., Awaitable[Any]]


async def accept_invite(
    *,
    kind: str,
    invitation_event_id: str,
    space_id: str,
    channel_id: str,
    slug: str,
    device_id: str,
    keystore: Any,
    http: Any,
    store: Any,
    find_general_channel: AsyncCall,
    enqueue_intro: AsyncCall,
    log: Log,
) -> None:
    session = keystore.load_session(slug)
    signing_key = Ed25519KeyPair.from_secret_bytes(
        decode_secret(session.subkey_secret_key)
    )
    payload, accept_kind = _accept_payload(
        kind=kind,
        invitation_event_id=invitation_event_id,
        space_id=space_id,
        channel_id=channel_id,
    )
    signed = sign_event(
        kind=accept_kind,
        payload=payload,
        signer_slug=slug,
        signer_device_id=device_id,
        signer_subkey_id=session.subkey_id,
        signing_key=signing_key,
    )
    await http.post(
        "/spaces/events",
        {"space_id": space_id, "events": [signed]},
    )
    if kind == EventKind.INVITE_TO_CHANNEL and channel_id and space_id:
        try:
            await store.mark_channel_space(channel_id, space_id)
        except Exception:
            log.exception(
                "mark_channel_space after manual accept failed (space=%s channel=%s)",
                space_id,
                channel_id,
            )
    intro_channel_id = await _intro_channel_id(
        kind=kind,
        channel_id=channel_id,
        space_id=space_id,
        find_general_channel=find_general_channel,
        log=log,
    )
    if intro_channel_id:
        try:
            await enqueue_intro(space_id=space_id, channel_id=intro_channel_id)
        except Exception:
            log.exception(
                "failed to enqueue channel intro nudge (space=%s channel=%s)",
                space_id,
                intro_channel_id,
            )


def _accept_payload(
    *,
    kind: str,
    invitation_event_id: str,
    space_id: str,
    channel_id: str,
) -> tuple[dict[str, Any], str]:
    accepted_at = int(time.time() * 1000)
    if kind == EventKind.INVITE_TO_SPACE:
        return {
            "space_id": space_id,
            "invitation_event_id": invitation_event_id,
            "accepted_at": accepted_at,
            "nonce": random_nonce(),
        }, EventKind.ACCEPT_SPACE_INVITE
    return {
        "space_id": space_id,
        "channel_id": channel_id,
        "invitation_event_id": invitation_event_id,
        "accepted_at": accepted_at,
        "nonce": random_nonce(),
    }, EventKind.ACCEPT_CHANNEL_INVITE


async def _intro_channel_id(
    *,
    kind: str,
    channel_id: str,
    space_id: str,
    find_general_channel: AsyncCall,
    log: Log,
) -> str:
    if kind == EventKind.INVITE_TO_CHANNEL and channel_id:
        return channel_id
    if kind != EventKind.INVITE_TO_SPACE:
        return ""
    try:
        return await find_general_channel(space_id)
    except Exception:
        log.exception(
            "failed to look up General channel for intro nudge (space=%s)",
            space_id,
        )
        return ""


async def find_public_general_channel(
    *,
    space_id: str,
    http: Any,
    store: Any,
    channel_names: dict[str, str],
    log: Log,
) -> str:
    if not space_id:
        return ""
    retry_delays = (0.5, 1.0, 3.0, 6.0, 12.0, 24.0, 24.0)
    for attempt, delay in enumerate(retry_delays, start=1):
        await asyncio.sleep(delay)
        data = await http.get(f"/spaces/{space_id}/channels")
        if not isinstance(data, dict):
            log.info(
                "channels endpoint not ready for space=%s (attempt %d/%d) — retrying",
                space_id,
                attempt,
                len(retry_delays),
            )
            continue
        general_channel = ""
        for entry in data.get("channels") or []:
            channel_id = entry.get("channel_id") or ""
            name = (entry.get("name") or "").strip()
            if channel_id and channel_id not in channel_names:
                channel_names[channel_id] = name or channel_id
            if channel_id:
                await store.mark_channel_space(channel_id, space_id)
            if not general_channel and entry.get("is_public") and channel_id:
                general_channel = channel_id
        return general_channel
    log.warning(
        "gave up looking up General channel for space=%s after %d attempts — "
        "intro nudge will be skipped",
        space_id,
        len(retry_delays),
    )
    return ""


async def enqueue_channel_intro_nudge(
    *,
    space_id: str,
    channel_id: str,
    store: Any,
    resolve_space_name: AsyncCall,
    resolve_channel_name: AsyncCall,
    notify_runtime: Callable[[], None] | None,
    log: Log,
) -> None:
    if await store.has_channel_intro_been_prompted(channel_id):
        return
    space_name = await resolve_space_name(space_id) if space_id else ""
    channel_name = await resolve_channel_name(space_id, channel_id)
    now_ms = int(time.time() * 1000)
    envelope_id = f"intro-prompt-{channel_id}"
    prompt = (
        "[puffo-agent system message] You've just been added to "
        f"channel #{channel_name} (channel_id: {channel_id}) in "
        f"space {space_name or space_id}. Post a brief "
        "self-introduction (2-3 sentences, in English by default) "
        f'using mcp__puffo__send_message with channel="{channel_id}" '
        "so existing members know who you are and how you can help. "
        "Don't include a thread root_id — this should be a new "
        "top-level post in the channel."
    )
    payload = {
        "envelope_id": envelope_id,
        "envelope_kind": "channel",
        "sender_slug": "system",
        "channel_id": channel_id,
        "space_id": space_id,
        "content_type": "text/plain",
        "content": prompt,
        "sent_at": now_ms,
        "thread_root_id": envelope_id,
        "reply_to_id": None,
    }
    try:
        await store.store_local_event(
            payload,
            reason="channel introduction",
            intro_channel_id=channel_id,
        )
    except Exception as exc:
        log.warning(
            "intro-nudge: failed to persist envelope=%s to messages.db: %s",
            envelope_id,
            exc,
        )
        return
    if notify_runtime is not None:
        notify_runtime()
    log.info(
        "enqueued channel-intro nudge for channel=%s (space=%s)",
        channel_id,
        space_id,
    )


async def maybe_announce_membership_change(
    *,
    kind: str,
    event: dict[str, Any],
    payload: dict[str, Any],
    slug: str,
    channel_spaces: Mapping[str, str],
    inviter_by_event_id: Mapping[str, str],
    processed_event_ids: set[str],
    enqueue_message: AsyncCall,
    log: Log,
) -> None:
    channel_id = payload.get("channel_id") or ""
    if not channel_id or channel_id not in channel_spaces:
        return
    actor = _channel_membership_actor(
        kind=kind,
        event=event,
        payload=payload,
        inviter_by_event_id=inviter_by_event_id,
    )
    if actor is None or not actor[0] or actor[0] == slug:
        return
    actor_slug, action, kicker_slug, inviter_slug = actor
    await _enqueue_once(
        event=event,
        processed_event_ids=processed_event_ids,
        enqueue_message=enqueue_message,
        log=log,
        error_context=(kind, "channel", channel_id, actor_slug),
        channel_id=channel_id,
        actor_slug=actor_slug,
        action=action,
        kicker_slug=kicker_slug,
        inviter_slug=inviter_slug,
    )


def _channel_membership_actor(
    *,
    kind: str,
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
    inviter_by_event_id: Mapping[str, str],
) -> tuple[str, str, str, str] | None:
    if kind == EventKind.ACCEPT_CHANNEL_INVITE:
        inviter = _inviter_slug(payload, inviter_by_event_id)
        return event.get("signer_slug") or "", "joined", "", inviter
    if kind == EventKind.LEAVE_CHANNEL:
        return event.get("signer_slug") or "", "left", "", ""
    if kind == EventKind.REMOVE_FROM_CHANNEL:
        return (
            payload.get("removed_slug") or "",
            "removed",
            event.get("signer_slug") or "",
            "",
        )
    return None


def _inviter_slug(
    payload: Mapping[str, Any],
    inviter_by_event_id: Mapping[str, str],
) -> str:
    original = payload.get("original_invite")
    if isinstance(original, dict):
        inviter = original.get("signer_slug") or ""
        if inviter:
            return inviter
    invitation_event_id = payload.get("invitation_event_id") or ""
    return inviter_by_event_id.get(invitation_event_id, "")


async def pick_space_channel(
    *,
    space_id: str,
    channel_spaces: dict[str, str],
    channel_names: dict[str, str],
    resolve_channel_name: AsyncCall,
    http: Any,
) -> str:
    known = sorted(
        channel_id
        for channel_id, current_space in channel_spaces.items()
        if current_space == space_id
    )
    for channel_id in known:
        name = await resolve_channel_name(
            space_id=space_id,
            channel_id=channel_id,
        )
        if (name or "").strip().lower() == "general":
            return channel_id
    if known:
        return known[0]
    try:
        data = await http.get(f"/spaces/{space_id}/channels")
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    channels = data.get("channels") or []
    if not isinstance(channels, list) or not channels:
        return ""
    return _cache_and_pick_channel(
        channels=channels,
        space_id=space_id,
        channel_spaces=channel_spaces,
        channel_names=channel_names,
    )


def _cache_and_pick_channel(
    *,
    channels: list[Any],
    space_id: str,
    channel_spaces: dict[str, str],
    channel_names: dict[str, str],
) -> str:
    general = ""
    first = ""
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        channel_id = (channel.get("channel_id") or "").strip()
        if not channel_id:
            continue
        channel_spaces[channel_id] = space_id
        name = (channel.get("name") or "").strip()
        if name:
            channel_names.setdefault(channel_id, name)
        first = first or channel_id
        if not general and name.lower() == "general":
            general = channel_id
    return general or first


async def maybe_announce_space_membership_change(
    *,
    kind: str,
    event: dict[str, Any],
    payload: dict[str, Any],
    slug: str,
    inviter_by_event_id: Mapping[str, str],
    processed_event_ids: set[str],
    pick_channel: AsyncCall,
    enqueue_message: AsyncCall,
    log: Log,
) -> None:
    space_id = payload.get("space_id") or ""
    if not space_id:
        return
    channel_id = await pick_channel(space_id)
    if not channel_id:
        return
    actor = _space_membership_actor(
        kind=kind,
        event=event,
        payload=payload,
        inviter_by_event_id=inviter_by_event_id,
    )
    if actor is None or not actor[0] or actor[0] == slug:
        return
    actor_slug, action, kicker_slug, inviter_slug = actor
    await _enqueue_once(
        event=event,
        processed_event_ids=processed_event_ids,
        enqueue_message=enqueue_message,
        log=log,
        error_context=(kind, "space", space_id, actor_slug),
        channel_id=channel_id,
        actor_slug=actor_slug,
        action=action,
        kicker_slug=kicker_slug,
        inviter_slug=inviter_slug,
    )


def _space_membership_actor(
    *,
    kind: str,
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
    inviter_by_event_id: Mapping[str, str],
) -> tuple[str, str, str, str] | None:
    if kind == EventKind.ACCEPT_SPACE_INVITE:
        inviter = _inviter_slug(payload, inviter_by_event_id)
        return event.get("signer_slug") or "", "joined_space", "", inviter
    if kind == EventKind.REDEEM_INVITE_CAPABILITY:
        return payload.get("redeemer_slug") or "", "joined_space", "", ""
    if kind == EventKind.LEAVE_SPACE:
        return event.get("signer_slug") or "", "left_space", "", ""
    if kind == EventKind.REMOVE_FROM_SPACE:
        return (
            payload.get("removed_slug") or "",
            "removed_from_space",
            event.get("signer_slug") or "",
            "",
        )
    return None


async def _enqueue_once(
    *,
    event: Mapping[str, Any],
    processed_event_ids: set[str],
    enqueue_message: AsyncCall,
    log: Log,
    error_context: tuple[str, str, str, str],
    **message: str,
) -> None:
    event_id = event.get("event_id") or ""
    if event_id and event_id in processed_event_ids:
        return
    try:
        await enqueue_message(event_id=event_id, **message)
    except Exception:
        kind, scope, target_id, actor_slug = error_context
        log.exception(
            "failed to enqueue %smembership system-message (kind=%s %s=%s actor=%s)",
            "space " if scope == "space" else "",
            kind,
            scope,
            target_id,
            actor_slug,
        )
        return
    if event_id:
        processed_event_ids.add(event_id)


async def enqueue_membership_system_message(
    *,
    channel_id: str,
    actor_slug: str,
    action: str,
    kicker_slug: str,
    inviter_slug: str,
    event_id: str,
    channel_spaces: Mapping[str, str],
    resolve_space_name: AsyncCall,
    resolve_channel_name: AsyncCall,
    fetch_display_name: AsyncCall,
    store: Any,
    notify_runtime: Callable[[], None] | None,
    log: Log,
) -> None:
    space_id = channel_spaces.get(channel_id) or ""
    space_name = await resolve_space_name(space_id) if space_id else ""
    channel_name = await resolve_channel_name(
        space_id=space_id,
        channel_id=channel_id,
    )
    actor_display_name = await fetch_display_name(actor_slug)
    actor = identity_label(actor_slug, actor_display_name)
    space_label = f"**{space_name}**" if space_name else (space_id or "the space")
    inviter_display_name = (
        await fetch_display_name(inviter_slug)
        if inviter_slug and action in {"joined", "joined_space"}
        else ""
    )
    inviter = (
        identity_label(inviter_slug, inviter_display_name)
        if inviter_slug and action in {"joined", "joined_space"}
        else ""
    )
    kicker_display_name = (
        await fetch_display_name(kicker_slug)
        if kicker_slug and action in {"removed", "removed_from_space"}
        else ""
    )
    kicker = (
        operator_label(kicker_slug, kicker_display_name)
        if action in {"removed", "removed_from_space"}
        else ""
    )
    body = membership_body(
        action=action,
        actor_label=actor,
        channel_name=channel_name,
        space_label=space_label,
        inviter_label=inviter,
        kicker_label=kicker,
    )
    if body is None:
        return
    await _persist_membership_message(
        action=action,
        actor_slug=actor_slug,
        actor_display_name=actor_display_name,
        body=body,
        inviter_slug=inviter_slug,
        initiator_slug=kicker_slug,
        channel_id=channel_id,
        channel_name=channel_name,
        space_id=space_id,
        space_name=space_name,
        event_id=event_id,
        store=store,
        notify_runtime=notify_runtime,
        log=log,
    )


async def _persist_membership_message(
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
    store: Any,
    notify_runtime: Callable[[], None] | None,
    log: Log,
) -> None:
    envelope_id, payload = membership_store_payload(
        action=action,
        actor_slug=actor_slug,
        actor_display_name=actor_display_name,
        body=body,
        inviter_slug=inviter_slug,
        initiator_slug=initiator_slug,
        channel_id=channel_id,
        channel_name=channel_name,
        space_id=space_id,
        space_name=space_name,
        event_id=event_id,
        now_ms=int(time.time() * 1000),
    )
    try:
        await store.store_local_event(payload, reason="membership system message")
    except Exception as exc:
        log.warning(
            "membership system-message: failed to persist envelope=%s "
            "to messages.db: %s",
            envelope_id,
            exc,
        )
        return
    if notify_runtime is not None:
        notify_runtime()
    log.info(
        "enqueued membership system-message channel=%s actor=%s action=%s",
        channel_id,
        actor_slug,
        action,
    )


def resolve_invite_targets(
    *,
    payload_thread_root_id: str | None,
    text: str,
    pending_invites: Mapping[str, dict[str, Any]],
) -> tuple[list[str], bool]:
    if payload_thread_root_id:
        if payload_thread_root_id in pending_invites:
            return [payload_thread_root_id], False
        return [], False
    if text.strip().lower() not in {"y", "yes", "n", "no"}:
        return [], False
    return list(pending_invites), True


async def apply_invite_replies(
    *,
    roots: list[str],
    text: str,
    pending_invites: Mapping[str, dict[str, Any]],
    handle_reply: AsyncCall,
) -> list[str]:
    labels: list[str] = []
    for root in roots:
        meta = pending_invites.get(root)
        handled = await handle_reply(thread_root_id=root, text=text)
        if handled and meta:
            labels.append(invite_target_label(meta))
    return labels


async def send_invite_bulk_summary(
    *,
    labels: list[str],
    text: str,
    root_id: str,
    operator_slug: str,
    send_dm: AsyncCall,
    log: Log,
) -> None:
    accepted = text.strip().lower() in {"y", "yes"}
    verb = "Accepted" if accepted else "Rejected"
    mark = " ✓" if accepted else ""
    if len(labels) == 1:
        summary = f"{verb} invite to {labels[0]}.{mark}"
    else:
        summary = f"{verb} {len(labels)} invites: {', '.join(labels)}.{mark}"
    try:
        await send_dm(operator_slug, summary, root_id=root_id)
    except Exception:
        log.exception("failed to send bulk invite summary to operator")


def invite_target_label(meta: Mapping[str, Any]) -> str:
    space_id = meta.get("space_id") or ""
    space_label = f"**{meta['space_name']}**" if meta.get("space_name") else space_id
    if meta.get("kind") != EventKind.INVITE_TO_CHANNEL:
        return f"space {space_label}"
    channel_id = meta.get("channel_id") or ""
    channel_label = (
        f"**{meta['channel_name']}**" if meta.get("channel_name") else channel_id
    )
    return f"channel {channel_label} in space {space_label}"
