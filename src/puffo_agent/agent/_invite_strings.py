"""Human-facing copy for invite failures and the OAuth-expired
operator DM (``format_oauth_expired``)."""

from __future__ import annotations

import json

from ..crypto.http_client import HttpError


def format_invite_error(exc: Exception, verb: str) -> str:
    """Translate an invite-accept/reject failure into a user-facing
    message safe to surface in the operator-DM confirm. Raw ``exc`` is
    preserved in the caller's ``log.exception`` for diagnostic; this
    helper produces ONLY the human-readable text.
    """
    prefix = f"Couldn't {verb} invite"
    if isinstance(exc, HttpError):
        error_code = ""
        message_text = ""
        try:
            parsed = json.loads(exc.body)
            if isinstance(parsed, dict):
                error_code = str(parsed.get("error") or "")
                message_text = str(parsed.get("message") or "")
        except (ValueError, TypeError):
            pass

        # Specific mappings BEFORE the status-class fallbacks: a 403
        # with message ``channel not found`` lands on the channel
        # branch by design. Flipping the order changes which branch
        # a 403+message-shaped response hits.
        #
        # Copy is deliberately ambiguous ("isn't reachable right now")
        # until PUF-247 bug-1 confirms the root cause is a true stale
        # invite (alpha) and not envelope corruption (beta/gamma);
        # promote to definitive language once bug-1 lands.
        lower_msg = message_text.lower()
        if "channel not found" in lower_msg:
            return (
                f"{prefix}: the server says that channel isn't reachable "
                "right now. Try again later."
            )
        if "space not found" in lower_msg:
            return (
                f"{prefix}: the server says that space isn't reachable "
                "right now. Try again later."
            )
        if exc.status == 403 or error_code == "FORBIDDEN":
            return f"{prefix}: you don't have permission for this one."
        if exc.status == 409 or error_code == "CONFLICT":
            return f"{prefix}: looks like it's already been handled."

        if 400 <= exc.status < 500:
            return f"{prefix}: please try again."
        if exc.status >= 500:
            return (
                f"{prefix}: Puffo server hit an issue. "
                "Please try again in a moment."
            )

    return f"{prefix}: unexpected error. Please try again."


def format_leave_error(exc: Exception) -> str:
    """Translate a ``leave_space``/``leave_channel`` failure into copy
    safe to surface in the operator-DM confirm. The two server-enforced
    rejections worth naming: a space owner can't leave directly, and a
    public channel can't be left without leaving the whole space."""
    prefix = "Couldn't leave"
    if isinstance(exc, HttpError):
        error_code = ""
        message_text = ""
        try:
            parsed = json.loads(exc.body)
            if isinstance(parsed, dict):
                error_code = str(parsed.get("error") or "")
                message_text = str(parsed.get("message") or "")
        except (ValueError, TypeError):
            pass
        lower_msg = message_text.lower()
        if "owner" in lower_msg:
            return (
                f"{prefix}: I'm the space owner, so I can't leave directly — "
                "ownership has to be transferred first."
            )
        if "public" in lower_msg:
            return (
                f"{prefix}: that's a public channel — I can only leave the "
                "whole space, not just the channel."
            )
        if exc.status == 403 or error_code == "FORBIDDEN":
            return f"{prefix}: the server won't let me leave this one."
        if exc.status == 409 or error_code == "CONFLICT":
            return f"{prefix}: looks like I'm already out."
        if 400 <= exc.status < 500:
            return f"{prefix}: please try again."
        if exc.status >= 500:
            return (
                f"{prefix}: Puffo server hit an issue. "
                "Please try again in a moment."
            )
    return f"{prefix}: unexpected error. Please try again."


def format_oauth_expired(agent_id: str, agent_display_name: str = "") -> str:
    """Bilingual (zh+en) operator DM for an OAuth-expired agent: run
    ``claude auth login`` and just send a message (a new message
    auto-resumes the agent). Falls back to a bare ``id`` when
    ``agent_display_name`` is empty."""
    label = (
        f"**{agent_display_name}** (`{agent_id}`)"
        if agent_display_name else f"`{agent_id}`"
    )
    return (
        f"⚠️ {label} — Claude OAuth expired. Run `claude auth login`, "
        "then send me a message and I'll pick up where I left off.\n"
        "⚠️ Claude OAuth 已过期。请运行 `claude auth login`，"
        "然后给我发条消息即可恢复。"
    )
