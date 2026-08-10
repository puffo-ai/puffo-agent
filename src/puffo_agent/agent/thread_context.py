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
        log_label="_validate_incoming_parent_id",
    )
    return parent_id if parent is not None else None


async def validated_parent(
    *,
    store: Any,
    log: Log,
    parent_id: str,
    expected_channel_id: str | None,
    expected_space_id: str | None,
    log_label: str,
) -> Any:
    """Load a parent that belongs to the expected channel and space."""
    try:
        parent = await store.get_message_by_envelope(parent_id)
    except Exception as exc:
        log.warning("%s: lookup failed for %s: %s", log_label, parent_id, exc)
        return None
    if parent is None:
        log.info("%s: wiped %s — parent not in local cache", log_label, parent_id)
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
) -> str | None:
    """Resolve a reply reference to its trusted canonical thread root."""
    if not parent_id:
        return parent_id
    current = parent_id
    seen: set[str] = set()
    for _ in range(_INCOMING_ROOT_MAX_DEPTH):
        if current in seen:
            log.info(
                "_resolve_incoming_thread_root: wiped %s — cycle in thread chain",
                parent_id,
            )
            return None
        seen.add(current)
        parent = await validated_parent(
            store=store,
            log=log,
            parent_id=current,
            expected_channel_id=expected_channel_id,
            expected_space_id=expected_space_id,
            log_label="_resolve_incoming_thread_root",
        )
        if parent is None:
            return None
        if not parent.thread_root_id or parent.thread_root_id == current:
            if current != parent_id:
                log.info(
                    "_resolve_incoming_thread_root: corrected %s → %s "
                    "(pointed at a reply, not the root)",
                    parent_id,
                    current,
                )
            return current
        current = parent.thread_root_id
    log.info(
        "_resolve_incoming_thread_root: wiped %s — chain deeper than %d",
        parent_id,
        _INCOMING_ROOT_MAX_DEPTH,
    )
    return None
