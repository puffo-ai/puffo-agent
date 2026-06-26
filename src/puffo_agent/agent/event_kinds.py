"""Mirror of puffo-server's ``EventKind`` Rust enum (snake_case wire form).

``StrEnum`` members are ``str`` subclasses, so ``event["kind"] ==
EventKind.LEAVE_CHANNEL`` and ``json.dumps({"kind": kind})`` both
work without explicit conversion.
"""

from __future__ import annotations

from enum import StrEnum


class EventKind(StrEnum):
    INVITE_TO_SPACE = "invite_to_space"
    INVITE_TO_CHANNEL = "invite_to_channel"
    ACCEPT_SPACE_INVITE = "accept_space_invite"
    ACCEPT_CHANNEL_INVITE = "accept_channel_invite"
    REJECT_SPACE_INVITE = "reject_space_invite"
    REJECT_CHANNEL_INVITE = "reject_channel_invite"
    CANCEL_SPACE_INVITE = "cancel_space_invite"
    CANCEL_CHANNEL_INVITE = "cancel_channel_invite"
    LEAVE_SPACE = "leave_space"
    LEAVE_CHANNEL = "leave_channel"
    REMOVE_FROM_SPACE = "remove_from_space"
    REMOVE_FROM_CHANNEL = "remove_from_channel"
    CREATE_CHANNEL = "create_channel"
