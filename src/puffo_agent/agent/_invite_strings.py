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
    agent. Numbered step ladder + WHERE-to-run clause reframe the DM
    as instruction rather than debug output. Falls back to a bare
    ``id`` when ``agent_display_name`` is empty."""
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
        "4. Once you're signed in, come back here and send me a message — "
        "I'll pick up where I left off.\n"
        "\n"
        f"⚠️ {label} — 我的 Claude Code 登录已过期，需要刷新后我才能"
        "继续回复。\n"
        "\n"
        "**在运行 puffo-agent 的电脑上：**\n"
        "1. 打开终端。\n"
        "2. 运行：`claude auth login`\n"
        "3. 按浏览器提示用你的 Claude 账户登录。\n"
        "4. 登录完成后回到这里发一条消息即可恢复。"
    )


def format_anthropic_api_key_rejected(
    agent_id: str, agent_display_name: str = "",
) -> str:
    """Bilingual recovery copy for daemon-owned Anthropic API keys."""
    label = (
        f"**{agent_display_name}** (`{agent_id}`)"
        if agent_display_name else f"`{agent_id}`"
    )
    return (
        f"⚠️ {label} — my Anthropic API key was rejected, so I can't "
        "answer until it is corrected.\n\n"
        "**On the computer where puffo-agent is running:**\n"
        "1. Update `anthropic.api_key` in `daemon.yml` (or run "
        "`puffo-agent config --anthropic-api-key KEY`).\n"
        "2. Keep `anthropic.cli_use_api_key: true`.\n"
        "3. Restart puffo-agent, then send me another message.\n\n"
        f"⚠️ {label} — 我的 Anthropic API key 被拒绝，需要修正后才能"
        "继续回复。\n\n"
        "**在运行 puffo-agent 的电脑上：**\n"
        "1. 修改 `daemon.yml` 中的 `anthropic.api_key`（或运行 "
        "`puffo-agent config --anthropic-api-key KEY`）。\n"
        "2. 保持 `anthropic.cli_use_api_key: true`。\n"
        "3. 重启 puffo-agent，然后再发一条消息。"
    )


def _resets_clause(resets_at: int | None) -> tuple[str, str]:
    """``(en, zh)`` reset-time clause, or empty strings when the provider
    didn't give us one. Local time — the operator reads this on the host
    the agent runs on."""
    if not resets_at:
        return "", ""
    from datetime import datetime

    # 24h and zero-padded: the `%-d` / `%-I` no-pad flags are glibc-only
    # and raise on Windows, which this daemon also runs on (PUF-420).
    when = datetime.fromtimestamp(resets_at).strftime("%b %d, %H:%M")
    return f" It resets around **{when}**.", f"额度大约在 **{when}** 重置。"


def format_drained(
    agent_id: str,
    agent_display_name: str = "",
    *,
    resets_at: int | None = None,
    provider: str = "Claude Code",
) -> str:
    """Bilingual operator DM for a quota-exhausted agent. Deliberately
    carries NO re-auth step: the JYP case this ticket fixes sent the
    operator to `claude auth login` for a spent quota, which can't help.
    The four options here are the only things that actually move a
    drained agent."""
    label = (
        f"**{agent_display_name}** (`{agent_id}`)"
        if agent_display_name else f"`{agent_id}`"
    )
    en_reset, zh_reset = _resets_clause(resets_at)
    return (
        f"🪫 {label} — my {provider} usage limit is spent, so I can't "
        f"answer you until it refills.{en_reset} I'm holding your "
        "messages rather than retrying; nothing is lost.\n"
        "\n"
        "**Your options:**\n"
        "1. Wait for the window to reset — no action needed, I resume on my own.\n"
        "2. Switch me to a smaller model (`/config`, or the model field on my agent card).\n"
        "3. Add credits / raise the spend cap on the account this host is signed in with.\n"
        "4. Upgrade the plan if you hit this often.\n"
        "\n"
        "**This is not a sign-in problem — re-running a login command won't help.**\n"
        "\n"
        f"🪫 {label} — 我的 {provider} 用量额度已经用完，要等额度恢复才能"
        f"继续回复。{zh_reset}我会先把消息存住、不重试，不会丢。\n"
        "\n"
        "**你可以：**\n"
        "1. 等窗口重置 —— 不用管，我会自己恢复。\n"
        "2. 把我换成更小的模型（`/config`，或 agent 卡片上的 model 字段）。\n"
        "3. 给这台机器登录的账号加额度 / 提高消费上限。\n"
        "4. 如果经常撞到，考虑升级套餐。\n"
        "\n"
        "**这不是登录问题 —— 重新跑登录命令没有用。**"
    )


def format_codex_drained(
    agent_id: str,
    agent_display_name: str = "",
    *,
    resets_at: int | None = None,
) -> str:
    """Codex-provider sibling of :func:`format_drained`. Only the provider
    name differs — the recovery options are the same four."""
    return format_drained(
        agent_id, agent_display_name, resets_at=resets_at, provider="Codex",
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
        "4. Once you're signed in, come back here and send me a message — "
        "I'll pick up where I left off.\n"
        "\n"
        f"⚠️ {label} — 我的 Codex 登录已过期，需要刷新后我才能继续回复。\n"
        "\n"
        "**在运行 puffo-agent 的电脑上：**\n"
        "1. 打开终端。\n"
        "2. 运行：`codex login`\n"
        "3. 按浏览器提示用你的 Codex 账户登录。\n"
        "4. 登录完成后回到这里发一条消息即可恢复。"
    )
