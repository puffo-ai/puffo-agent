"""Daemon-level decision for whether an outbound message must be E2EE.

Encrypt when the turn's triggering bundle contained an encrypted
message, or when the send targets a thread whose root is encrypted.
Everything else goes out as a plaintext envelope.
"""

from __future__ import annotations

from typing import Any

# Workers serialize turns per agent. The flag is set when a batch is
# admitted and cleared when the turn ends.
_turn_bundle_encrypted: dict[str, bool] = {}


def note_turn_bundle(keys: list[str], has_encrypted: bool) -> None:
    for key in keys:
        if key:
            _turn_bundle_encrypted[key] = has_encrypted


def raise_turn_bundle(keys: list[str]) -> None:
    """Mark the turn bundle encrypted; never lower an existing ``True``.

    Mid-turn admission adds rows to a turn that was planned from a
    different set. An admitted encrypted row must raise the flag, and a
    plaintext one must not clear an encryption obligation already
    established by the planned bundle.
    """
    for key in keys:
        if key:
            _turn_bundle_encrypted[key] = True


def clear_turn_bundle(keys: list[str]) -> None:
    for key in keys:
        _turn_bundle_encrypted.pop(key, None)


def turn_bundle_encrypted(key: str) -> bool:
    return _turn_bundle_encrypted.get(key, False)


async def encryption_required(
    key: str,
    store: Any,
    thread_root_id: str | None,
) -> bool:
    """Return whether the active turn or target thread requires E2EE."""
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
