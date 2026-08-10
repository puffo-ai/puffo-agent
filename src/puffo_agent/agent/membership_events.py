"""Membership event side effects and invite polling for the daemon."""

from __future__ import annotations

import logging
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..crypto.encoding import base64url_decode
from .event_kinds import EventKind

Log = logging.Logger | logging.LoggerAdapter
AsyncCall = Callable[..., Awaitable[Any]]


async def invite_poll_loop(
    *,
    poll_invites: AsyncCall,
    next_interval: Callable[..., int],
    sleep: AsyncCall,
) -> None:
    """Run the fast-then-steady invite poll cadence until cancelled."""
    try:
        await sleep(2)
        while True:
            await poll_invites()
            interval = next_interval(
                fast=10,
                steady=30,
                fast_phase_seconds=300,
            )
            await sleep(interval)
    except asyncio.CancelledError:
        return


async def maybe_cache_channel_space(
    *,
    kind: str | None,
    event: dict[str, Any],
    payload: dict[str, Any],
    slug: str,
    store: Any,
    channel_spaces: dict[str, str],
) -> None:
    if not kind:
        return
    channel_id = payload.get("channel_id") or ""
    space_id = payload.get("space_id") or ""
    if not channel_id or not space_id:
        return
    if kind == EventKind.INVITE_TO_CHANNEL:
        if payload.get("invitee_slug") != slug:
            return
    elif kind == EventKind.ACCEPT_CHANNEL_INVITE:
        if event.get("signer_slug") != slug:
            return
    elif kind != EventKind.CREATE_CHANNEL:
        return
    await store.mark_channel_space(channel_id, space_id)
    channel_spaces[channel_id] = space_id


async def evict_space_caches(
    *,
    space_id: str,
    channel_spaces: dict[str, str],
    channel_names: dict[str, str],
    space_names: dict[str, str],
    space_members: dict[str, dict[str, str]],
    store: Any,
    log: Log,
) -> None:
    if not space_id:
        return
    for channel_id in [
        key for key, value in channel_spaces.items() if value == space_id
    ]:
        channel_spaces.pop(channel_id, None)
        channel_names.pop(channel_id, None)
    space_names.pop(space_id, None)
    space_members.pop(space_id, None)
    try:
        await store.unmark_channel_space_for_space(space_id)
    except Exception:
        log.exception(
            "unmark_channel_space_for_space failed for sp=%s "
            "(non-fatal — in-memory eviction already ran)",
            space_id,
        )


async def evict_channel_caches(
    *,
    channel_id: str,
    channel_spaces: dict[str, str],
    channel_names: dict[str, str],
    store: Any,
    log: Log,
) -> None:
    if not channel_id:
        return
    channel_spaces.pop(channel_id, None)
    channel_names.pop(channel_id, None)
    try:
        await store.unmark_channel_space(channel_id)
    except Exception:
        log.exception(
            "unmark_channel_space failed for ch=%s "
            "(non-fatal — in-memory eviction already ran)",
            channel_id,
        )


async def dm_operator_membership_change(
    *,
    text: str,
    operator_slug: str,
    send_dm: AsyncCall,
    log: Log,
) -> None:
    if not operator_slug:
        log.info("membership change but no operator_slug; not DMing: %s", text)
        return
    try:
        await send_dm(operator_slug, text, root_id="")
    except Exception:
        log.exception("failed to DM operator about membership change: %s", text)


async def on_left_space(
    *,
    space_id: str,
    synthetic: bool,
    gate_left_spaces: set[str],
    still_member: AsyncCall,
    resolve_space_name: AsyncCall,
    evict_space: AsyncCall,
    dm_membership_change: AsyncCall,
    log: Log,
) -> None:
    if not space_id:
        return
    if synthetic and await still_member(space_id) is True:
        log.warning(
            "synthetic LeaveSpace for sp=%s but /spaces still lists us — "
            "ignoring (likely server bug or WS redelivery)",
            space_id,
        )
        return
    space_label = await resolve_space_name(space_id)
    await evict_space(space_id)
    if not synthetic and space_id in gate_left_spaces:
        gate_left_spaces.discard(space_id)
        return
    reason = "your space exit cascaded to me" if synthetic else "I signed a LeaveSpace"
    await dm_membership_change(
        f"Removed from space **{space_label}**({space_id}) — {reason}."
    )


async def still_member_of_space(
    *,
    space_id: str,
    http: Any,
    log: Log,
) -> bool | None:
    try:
        data = await http.get("/spaces")
    except Exception:
        log.exception(
            "membership re-check for sp=%s failed — falling through",
            space_id,
        )
        return None
    return any(entry.get("space_id") == space_id for entry in data.get("spaces") or [])


async def on_kicked_from_space(
    *,
    space_id: str,
    kicker_slug: str,
    resolve_space_name: AsyncCall,
    evict_space: AsyncCall,
    fetch_display_name: AsyncCall,
    dm_membership_change: AsyncCall,
) -> None:
    if not space_id:
        return
    space_label = await resolve_space_name(space_id)
    await evict_space(space_id)
    kicker_display = await fetch_display_name(kicker_slug) if kicker_slug else ""
    kicker_label = _actor_label(kicker_slug, kicker_display, "the space owner")
    await dm_membership_change(
        f"Removed from space **{space_label}**({space_id}) by {kicker_label}."
    )


async def on_kicked_from_channel(
    *,
    channel_id: str,
    space_id: str,
    kicker_slug: str,
    resolve_channel_name: AsyncCall,
    resolve_space_name: AsyncCall,
    evict_channel: AsyncCall,
    fetch_display_name: AsyncCall,
    dm_membership_change: AsyncCall,
) -> None:
    if not channel_id:
        return
    channel_label = await resolve_channel_name(
        space_id=space_id,
        channel_id=channel_id,
    )
    space_label = await resolve_space_name(space_id) if space_id else ""
    await evict_channel(channel_id)
    kicker_display = await fetch_display_name(kicker_slug) if kicker_slug else ""
    kicker_label = _actor_label(kicker_slug, kicker_display, "the space owner")
    location = f"channel **{channel_label}**({channel_id})"
    if space_id:
        location += f" in space **{space_label}**({space_id})"
    await dm_membership_change(f"Removed from {location} by {kicker_label}.")


def _actor_label(slug: str, display_name: str, fallback: str) -> str:
    if display_name:
        return f"**{display_name}**(@{slug})"
    if slug:
        return f"@{slug}"
    return fallback


async def on_invite_canceled(
    *,
    invitation_event_id: str,
    scope: str,
    pending_invites: dict[str, dict[str, Any]],
    processed_invite_ids: set[str],
    operator_slug: str,
    fetch_display_name: AsyncCall,
    send_dm: AsyncCall,
    log: Log,
) -> None:
    if not invitation_event_id:
        return
    target_envelope_id = next(
        (
            envelope_id
            for envelope_id, meta in pending_invites.items()
            if meta.get("invitation_event_id") == invitation_event_id
        ),
        None,
    )
    if target_envelope_id is None:
        return
    meta = pending_invites.pop(target_envelope_id)
    processed_invite_ids.add(invitation_event_id)
    text = await _canceled_invite_text(
        scope=scope,
        meta=meta,
        fetch_display_name=fetch_display_name,
    )
    try:
        await send_dm(operator_slug, text, root_id=target_envelope_id)
    except Exception:
        log.exception(
            "failed to DM operator about canceled invite (event_id=%s)",
            invitation_event_id,
        )


async def _canceled_invite_text(
    *,
    scope: str,
    meta: dict[str, Any],
    fetch_display_name: AsyncCall,
) -> str:
    inviter_slug = meta.get("inviter_slug") or ""
    space_id = meta.get("space_id") or ""
    channel_id = meta.get("channel_id") or ""
    space_name = meta.get("space_name") or None
    channel_name = meta.get("channel_name") or None
    space_label = f"**{space_name}**" if space_name else space_id
    inviter_display = await fetch_display_name(inviter_slug) if inviter_slug else ""
    inviter_label = _actor_label(inviter_slug, inviter_display, "the inviter")
    if inviter_display:
        inviter_label = f"**{inviter_display}** (@{inviter_slug})"
    if scope == "channel":
        channel_label = f"**{channel_name}**" if channel_name else channel_id
        target = f"channel {channel_label} in space {space_label}"
    else:
        target = f"space {space_label}"
    return (
        f"{inviter_label} withdrew the invite to {target} — ignore my earlier prompt."
    )


async def poll_pending_invites(
    *,
    http: Any,
    processed_invite_ids: set[str],
    process_invite: AsyncCall,
    log: Log,
) -> None:
    try:
        data = await http.get("/invites?direction=received")
    except Exception:
        log.exception("invite poll failed")
        return
    invites = data.get("invites") or []
    if not invites:
        return
    log.info("invite poll: %d pending invite(s)", len(invites))
    for entry in invites:
        invitation_event_id = entry.get("invitation_event_id", "")
        if not invitation_event_id or invitation_event_id in processed_invite_ids:
            continue
        kind = _invite_kind(entry.get("scope", ""))
        if not kind:
            log.warning(
                "unknown invite scope %r (event_id=%s) — skipping",
                entry.get("scope", ""),
                invitation_event_id,
            )
            continue
        await process_invite(
            kind=kind,
            invitation_event_id=invitation_event_id,
            inviter_slug=entry.get("inviter_slug", ""),
            space_id=entry.get("space_id", ""),
            channel_id=entry.get("channel_id") or "",
            space_name=entry.get("space_name") or None,
            channel_name=entry.get("channel_name") or None,
        )


def _invite_kind(scope: str) -> str:
    if scope == "channel":
        return EventKind.INVITE_TO_CHANNEL
    if scope == "space":
        return EventKind.INVITE_TO_SPACE
    return ""


async def process_invite(
    *,
    kind: str,
    invitation_event_id: str,
    inviter_slug: str,
    space_id: str,
    channel_id: str,
    space_name: str | None,
    channel_name: str | None,
    auto_accept_space_invitations: bool,
    processed_invite_ids: set[str],
    inviter_is_operator: AsyncCall,
    accept_invite: AsyncCall,
    report_auto_accepted_space_invite: AsyncCall,
    notify_operator_of_invite: AsyncCall,
    log: Log,
) -> None:
    if not invitation_event_id or not inviter_slug or not space_id:
        log.warning(
            "invite missing required fields: kind=%s event_id=%s signer=%s space=%s",
            kind,
            invitation_event_id,
            inviter_slug,
            space_id,
        )
        return
    if kind == EventKind.INVITE_TO_CHANNEL and not channel_id:
        log.warning(
            "channel invite missing channel_id: event_id=%s",
            invitation_event_id,
        )
        return
    if invitation_event_id in processed_invite_ids:
        return

    is_from_operator = await inviter_is_operator(inviter_slug)
    flag_accept = kind == EventKind.INVITE_TO_SPACE and auto_accept_space_invitations
    if is_from_operator or flag_accept:
        try:
            await accept_invite(kind, invitation_event_id, space_id, channel_id)
            log.info(
                "auto-accepted %s from %s (event_id=%s)",
                kind,
                inviter_slug,
                invitation_event_id,
            )
            processed_invite_ids.add(invitation_event_id)
        except Exception:
            log.exception(
                "failed to auto-accept %s from %s (event_id=%s)",
                kind,
                inviter_slug,
                invitation_event_id,
            )
            return
        if not is_from_operator:
            await report_auto_accepted_space_invite(
                inviter_slug=inviter_slug,
                space_id=space_id,
                space_name=space_name,
            )
        return
    try:
        await notify_operator_of_invite(
            kind=kind,
            inviter_slug=inviter_slug,
            space_id=space_id,
            channel_id=channel_id,
            invitation_event_id=invitation_event_id,
            space_name=space_name,
            channel_name=channel_name,
        )
    finally:
        processed_invite_ids.add(invitation_event_id)


async def report_auto_accepted_space_invite(
    *,
    inviter_slug: str,
    space_id: str,
    space_name: str | None,
    operator_slug: str,
    fetch_display_name: AsyncCall,
    send_dm: AsyncCall,
    log: Log,
) -> None:
    if not operator_slug:
        return
    space_label = f"**{space_name}**({space_id})" if space_name else space_id
    inviter_display = await fetch_display_name(inviter_slug)
    inviter_label = _actor_label(inviter_slug, inviter_display, f"@{inviter_slug}")
    text = (
        f"Auto-accepted an invite to space {space_label} from {inviter_label} "
        "(auto-accept-space-invitations is on)."
    )
    try:
        await send_dm(operator_slug, text, root_id="")
    except Exception:
        log.exception("failed to report auto-accepted space invite to operator")


async def report_auto_accepted_channel_invite(
    *,
    inviter_slug: str,
    space_id: str,
    channel_id: str,
    operator_slug: str,
    resolve_space_name: AsyncCall,
    resolve_channel_name: AsyncCall,
    fetch_display_name: AsyncCall,
    send_dm: AsyncCall,
    log: Log,
) -> None:
    if not operator_slug:
        return
    space_name = await resolve_space_name(space_id)
    channel_name = await resolve_channel_name(
        space_id=space_id,
        channel_id=channel_id,
    )
    inviter_label = f"@{inviter_slug}" if inviter_slug else "the space owner"
    if inviter_slug:
        inviter_display = await fetch_display_name(inviter_slug)
        if inviter_display:
            inviter_label = f"**{inviter_display}**"
    text = (
        f"Auto-accepted {inviter_label}'s invite to channel "
        f"**{channel_name or channel_id}** in space **{space_name or space_id}** "
        "(space-owner invites are auto-accepted)."
    )
    try:
        await send_dm(operator_slug, text, root_id="")
    except Exception:
        log.exception("failed to report auto-accepted channel invite to operator")


async def inviter_is_operator(
    *,
    inviter_slug: str,
    operator_slug: str,
    operator_root_pubkey: bytes | None,
    fetch_inviter_root_pubkey: AsyncCall,
    log: Log,
) -> bool:
    if operator_root_pubkey is None:
        return False
    if inviter_slug and inviter_slug == operator_slug:
        return True
    try:
        inviter_pubkey = await fetch_inviter_root_pubkey(inviter_slug)
    except Exception as exc:
        log.warning(
            "could not look up identity_cert for inviter %s: %s",
            inviter_slug,
            exc,
        )
        return False
    return inviter_pubkey is not None and inviter_pubkey == operator_root_pubkey


async def fetch_inviter_root_pubkey(
    *,
    slug: str,
    cache: dict[str, bytes],
    http: Any,
) -> bytes | None:
    if not slug:
        return None
    if slug in cache:
        return cache[slug]
    since = 0
    encoded_key: str | None = None
    while True:
        data = await http.get(f"/certs/sync?slugs={slug}&since={since}")
        for entry in data.get("entries", []):
            if entry.get("kind") == "identity_cert":
                candidate = (entry.get("cert") or {}).get("root_public_key")
                if isinstance(candidate, str) and candidate:
                    encoded_key = candidate
            since = entry.get("seq", since)
        if not data.get("has_more"):
            break
    if encoded_key is None:
        return None
    try:
        public_key = base64url_decode(encoded_key)
    except Exception:
        return None
    if len(public_key) != 32:
        return None
    cache[slug] = public_key
    return public_key
