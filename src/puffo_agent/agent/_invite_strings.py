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
    """Bilingual (zh+en) operator DM for a Claude-Code OAuth-expired
    agent. Numbered step ladder + WHERE-to-run clause + Claude-vs-Codex
    disambiguation address the "debug not instruction" gap Sam surfaced
    in PUF-341. Falls back to a bare ``id`` when ``agent_display_name``
    is empty."""
    label = (
        f"**{agent_display_name}** (`{agent_id}`)"
        if agent_display_name else f"`{agent_id}`"
    )
    return (
        f"⚠️ {label} — my Claude Code sign-in has expired, so I can't "
        "answer you until it's refreshed.\n"
        "\n"
        "**On the computer where puffo-agent is running:**\n"
        "1. Open a terminal.\n"
        "2. Run: `claude auth login`\n"
        "3. Follow the browser prompt to sign in with your Claude account.\n"
        "4. Come back here and send me any message — I'll pick up where "
        "I left off.\n"
        "\n"
        "(This is the Claude Code CLI login, not Codex — even if Codex "
        "is also installed, signing into Codex won't fix this one.)\n"
        "\n"
        f"⚠️ {label} — 我的 Claude Code 登录已过期，需要刷新后我才能"
        "继续回复。\n"
        "\n"
        "**在运行 puffo-agent 的电脑上：**\n"
        "1. 打开终端。\n"
        "2. 运行：`claude auth login`\n"
        "3. 按浏览器提示用你的 Claude 账户登录。\n"
        "4. 回到这里给我发一条消息即可恢复。\n"
        "\n"
        "（这是 Claude Code 命令行的登录，不是 Codex——"
        "就算 Codex 也装在你机器上，登录 Codex 无法修复这里。）"
    )


def format_codex_oauth_expired(
    agent_id: str, agent_display_name: str = "",
) -> str:
    """Sibling of :func:`format_oauth_expired` for the Codex provider.
    Worker dispatches between the two on ``agent_cfg.runtime.harness``
    so the operator sees the right recovery command for the agent that
    actually failed."""
    label = (
        f"**{agent_display_name}** (`{agent_id}`)"
        if agent_display_name else f"`{agent_id}`"
    )
    return (
        f"⚠️ {label} — my Codex sign-in has expired, so I can't answer "
        "you until it's refreshed.\n"
        "\n"
        "**On the computer where puffo-agent is running:**\n"
        "1. Open a terminal.\n"
        "2. Run: `codex login`\n"
        "3. Follow the browser prompt to sign in with your Codex account.\n"
        "4. Come back here and send me any message — I'll pick up where "
        "I left off.\n"
        "\n"
        "(This is the Codex CLI login, not Claude Code — even if Claude "
        "Code is also installed, signing into it won't fix this one.)\n"
        "\n"
        f"⚠️ {label} — 我的 Codex 登录已过期，需要刷新后我才能继续回复。\n"
        "\n"
        "**在运行 puffo-agent 的电脑上：**\n"
        "1. 打开终端。\n"
        "2. 运行：`codex login`\n"
        "3. 按浏览器提示用你的 Codex 账户登录。\n"
        "4. 回到这里给我发一条消息即可恢复。\n"
        "\n"
        "（这是 Codex 命令行的登录，不是 Claude Code——"
        "就算 Claude Code 也装在你机器上，登录 Claude Code 无法修复这里。）"
    )
