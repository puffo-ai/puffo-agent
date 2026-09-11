"""Validate and canonicalize inbound reply and thread references."""

from __future__ import annotations

import logging
from typing import Any

Log = logging.Logger | logging.LoggerAdapter

_INCOMING_ROOT_MAX_DEPTH = 8


async def validate_incoming_parent_id(
    *,
    store: Any,
    log: Log,
    parent_id: str | None,
    expected_channel_id: str | None,
    expected_space_id: str | None,
    expected_envelope_kind: str = "",
    expected_dm_peer: str = "",
    expected_self_slug: str = "",
) -> str | None:
    """Keep a direct parent only when it belongs to the inbound target."""
    if not parent_id:
        return parent_id
    parent = await validated_parent(
        store=store,
        log=log,
        parent_id=parent_id,
        expected_channel_id=expected_channel_id,
        expected_space_id=expected_space_id,
        expected_envelope_kind=expected_envelope_kind,
        expected_dm_peer=expected_dm_peer,
        expected_self_slug=expected_self_slug,
        log_label="_validate_incoming_parent_id",
    )
    if parent is PARENT_MISSING or parent is None:
        return None
    return parent_id


class _ParentMissing:
    """Sentinel: the parent is not locally verifiable (absent or lookup
    failed) — distinct from an affirmative ownership mismatch."""


PARENT_MISSING = _ParentMissing()


async def validated_parent(
    *,
    store: Any,
    log: Log,
    parent_id: str,
    expected_channel_id: str | None,
    expected_space_id: str | None,
    log_label: str,
    expected_envelope_kind: str = "",
    expected_dm_peer: str = "",
    expected_self_slug: str = "",
) -> Any:
    """Load a parent that belongs to the expected channel and space.

    Returns the parent row, ``None`` on an affirmative mismatch, or
    ``PARENT_MISSING`` when the parent simply is not in the local store.
    """
    try:
        parent = await store.get_message_by_envelope(parent_id)
    except Exception as exc:
        log.warning("%s: lookup failed for %s: %s", log_label, parent_id, exc)
        return PARENT_MISSING
    if parent is None:
        log.info("%s: parent %s not in local cache", log_label, parent_id)
        return PARENT_MISSING
    if expected_envelope_kind and parent.envelope_kind != expected_envelope_kind:
        log.info(
            "%s: wiped %s — parent kind %r != incoming kind %r",
            log_label,
            parent_id,
            parent.envelope_kind,
            expected_envelope_kind,
        )
        return None
    if expected_envelope_kind == "dm":
        participants = {parent.sender_slug, parent.recipient_slug}
        if (
            not expected_dm_peer
            or expected_dm_peer not in participants
            or (expected_self_slug and expected_self_slug not in participants)
        ):
            log.info(
                "%s: wiped %s — parent belongs to another DM",
                log_label,
                parent_id,
            )
            return None
    if expected_channel_id and parent.channel_id != expected_channel_id:
        log.info(
            "%s: wiped %s — parent channel %r != incoming channel %r",
            log_label,
            parent_id,
            parent.channel_id,
            expected_channel_id,
        )
        return None
    if expected_space_id and parent.space_id and parent.space_id != expected_space_id:
        log.info(
            "%s: wiped %s — parent space %r != incoming space %r",
            log_label,
            parent_id,
            parent.space_id,
            expected_space_id,
        )
        return None
    return parent


async def resolve_incoming_thread_root(
    *,
    store: Any,
    log: Log,
    parent_id: str | None,
    expected_channel_id: str | None,
    expected_space_id: str | None,
    expected_envelope_kind: str = "",
    expected_dm_peer: str = "",
    expected_self_slug: str = "",
) -> tuple[str | None, bool]:
    """Resolve a reply reference to its trusted canonical thread root.

    Returns ``(root_id, unverified)``. An affirmative ownership mismatch,
    a cycle, or an over-deep chain still wipes the reference (``(None,
    False)``): those are evidence the claim is wrong. A parent that simply
    is not in the local store — root predates this agent joining, root was
    aged out, root failed to decrypt — keeps the claimed id with
    ``unverified=True`` instead of demoting the reply to the channel
    level; the arrival of the root later settles the flag.
    """
    if not parent_id:
        return parent_id, False
    current = parent_id
    seen: set[str] = set()
    for _ in range(_INCOMING_ROOT_MAX_DEPTH):
        if current in seen:
            log.info(
                "_resolve_incoming_thread_root: wiped %s — cycle in thread chain",
                parent_id,
            )
            return None, False
        seen.add(current)
        parent = await validated_parent(
            store=store,
            log=log,
            parent_id=current,
            expected_channel_id=expected_channel_id,
            expected_space_id=expected_space_id,
            expected_envelope_kind=expected_envelope_kind,
            expected_dm_peer=expected_dm_peer,
            expected_self_slug=expected_self_slug,
            log_label="_resolve_incoming_thread_root",
        )
        if parent is PARENT_MISSING:
            log.info(
                "_resolve_incoming_thread_root: kept %s unverified — "
                "root %s not locally verifiable",
                parent_id,
                current,
            )
            return current, True
        if parent is None:
            return None, False
        if not parent.thread_root_id or parent.thread_root_id == current:
            if current != parent_id:
                log.info(
                    "_resolve_incoming_thread_root: corrected %s → %s "
                    "(pointed at a reply, not the root)",
                    parent_id,
                    current,
                )
            return current, False
        current = parent.thread_root_id
    log.info(
        "_resolve_incoming_thread_root: wiped %s — chain deeper than %d",
        parent_id,
        _INCOMING_ROOT_MAX_DEPTH,
    )
    return None, False
