"""Daemon-level decision for whether an outbound message must be E2EE.

Encrypt when the turn's triggering bundle contained an encrypted
message, or when the send targets a thread whose root is encrypted —
the agent never downgrades a conversation's confidentiality. Everything
else (daemon system DMs included) goes out as a plaintext envelope.
"""

from __future__ import annotations

from typing import Any

# Single-slot per agent — the worker serializes turns. Set at batch
# dispatch, cleared when the turn ends, so between-turn sends fall to
# the plaintext default. Keyed by slug and/or agent_id.
_turn_bundle_encrypted: dict[str, bool] = {}


def note_turn_bundle(keys: list[str], has_encrypted: bool) -> None:
    for key in keys:
        if key:
            _turn_bundle_encrypted[key] = has_encrypted


def clear_turn_bundle(keys: list[str]) -> None:
    for key in keys:
        _turn_bundle_encrypted.pop(key, None)


def turn_bundle_encrypted(key: str) -> bool:
    return _turn_bundle_encrypted.get(key, False)


async def encryption_required(
    key: str,
    store: Any,
    thread_root_id: str | None,
    channel_policy: bool | None = None,
) -> bool:
    """The OR of the two rules. An unknown/unstored root counts as
    encrypted (store rows also default is_encrypted=True for legacy).

    PUF-411: an explicit channel policy short-circuits both rules, in
    either direction. That includes forcing plaintext on a channel the
    owner marked plaintext even when the thread root was encrypted —
    the alternative isn't a safer send, it's a rejected one, because the
    server refuses a sealed write there. ``None`` means no policy is set
    (every channel predating PUF-410), leaving the rules untouched.
    """
    if channel_policy is not None:
        return channel_policy
    if turn_bundle_encrypted(key):
        return True
    if not thread_root_id:
        return False
    try:
        row = await store.get_message_by_envelope(thread_root_id)
    except Exception:
        return True
    if row is None:
        return True
    return bool(getattr(row, "is_encrypted", True))
