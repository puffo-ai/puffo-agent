"""Slack, as the user the operator connected: post, search, read and list.

Posting is the one non-idempotent call, so it keeps the three outcomes
(*sent*, *failed*, *unknown*); everything else is repeatable and a lost
answer is simply retryable. Slack fails with HTTP 200 and ``ok: false``, so
``ok`` is the status. Only ids and codes reach the log.
"""

from __future__ import annotations

import logging
from typing import Any

from .credentials import AgentCredentials
from .oauth_connector import (
    Account,
    Connector,
    ConnectorError,
    ConnectorLostAnswer,
    Transport,
    call,
    pick_account,
)

logger = logging.getLogger(__name__)

# puffo-server registers the provider as "slackuser" (the user half of the
# grant), so the types say SLACKUSER, not SLACK.
CONNECTOR = Connector(
    name="slack", label="Slack workspace",
    rt_type="PUFFO_SLACKUSER_OAUTH_v1", at_type="PUFFO_SLACKUSER_OAUTH_AT_v1",
)
API = "https://slack.com/api"

# Slack's own names for "this token is no good"; one refresh, then reconnect.
AUTH_ERRORS = frozenset({
    "invalid_auth", "token_revoked", "token_expired", "not_authed", "account_inactive",
})
CHANNEL_KINDS = ("public_channel", "private_channel", "mpim", "im")
DEFAULT_KINDS = ",".join(CHANNEL_KINDS)
MAX_RESULTS = 50
# Past this a message would bury the agent's own context; the cut is flagged.
MAX_TEXT_CHARS = 4000
# Name lookups are a courtesy; a long channel does not get one per author.
MAX_NAME_LOOKUPS = 25


async def send(
    credentials: AgentCredentials | None,
    *,
    channel: str,
    text: str,
    thread_ts: str = "",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """``{"status": "sent" | "unknown", ...}``; raises when it definitely
    was not posted."""
    if not channel.strip():
        raise ConnectorError("bad_request", "channel is required")
    if not text.strip():
        raise ConnectorError("bad_request", "text is required")
    account = await pick_account(CONNECTOR, credentials, from_account)
    payload: dict[str, Any] = {"channel": channel.strip(), "text": text}
    if thread_ts.strip():
        payload["thread_ts"] = thread_ts.strip()
    try:
        status, data = await call(
            CONNECTOR, account, "POST", f"{api}/chat.postMessage",
            payload=payload, transport=transport, refused=_refused,
        )
    except ConnectorLostAnswer as exc:
        return _unknown(str(exc))
    if status >= 500:
        return _unknown(f"Slack answered {status}")
    reply = _ok(status, data, retryable=False)
    logger.info("slack send: posted %s", reply.get("ts", ""))
    return {"status": "sent", "channel": reply.get("channel", ""), "ts": reply.get("ts", "")}


async def search(
    credentials: AgentCredentials | None,
    *,
    query: str,
    limit: int = 10,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Messages matching a Slack search, newest first."""
    if not query.strip():
        raise ConnectorError("bad_request", "query is required")
    account = await pick_account(CONNECTOR, credentials, from_account)
    data = await _get(
        account, f"{api}/search.messages",
        {"query": query, "count": _limit(limit, 10), "sort": "timestamp", "sort_dir": "desc"},
        transport,
    )
    found = data.get("messages") if isinstance(data.get("messages"), dict) else {}
    messages = [_match(m) for m in found.get("matches") or [] if isinstance(m, dict)]
    logger.info("slack search: %d result(s)", len(messages))
    return {"messages": messages, "count": len(messages), "total": int(found.get("total") or 0)}


async def read(
    credentials: AgentCredentials | None,
    *,
    channel: str,
    limit: int = 20,
    thread_ts: str = "",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """A conversation's latest messages, or one thread's replies."""
    if not channel.strip():
        raise ConnectorError("bad_request", "channel is required")
    account = await pick_account(CONNECTOR, credentials, from_account)
    params: dict[str, Any] = {"channel": channel.strip(), "limit": _limit(limit, 20)}
    if thread_ts.strip():
        params["ts"] = thread_ts.strip()
        data = await _get(account, f"{api}/conversations.replies", params, transport)
    else:
        data = await _get(account, f"{api}/conversations.history", params, transport)
    raw = [m for m in data.get("messages") or [] if isinstance(m, dict)]
    names = await _user_names(account, {m.get("user") for m in raw}, transport, api)
    messages = [_message(m, names) for m in raw]
    logger.info("slack read: %d message(s)", len(messages))
    return {
        "channel": channel.strip(),
        "messages": messages,
        "count": len(messages),
        "has_more": bool(data.get("has_more")),
    }


async def list_channels(
    credentials: AgentCredentials | None,
    *,
    types: str = "",
    limit: int = 100,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Conversations the connected user can see: channels, groups, DMs."""
    kinds = [k.strip() for k in (types or DEFAULT_KINDS).split(",") if k.strip()]
    unknown = [k for k in kinds if k not in CHANNEL_KINDS]
    if unknown:
        raise ConnectorError(
            "bad_request", f"{unknown[0]!r} is not a kind; one of: " + ", ".join(CHANNEL_KINDS)
        )
    account = await pick_account(CONNECTOR, credentials, from_account)
    data = await _get(
        account, f"{api}/conversations.list",
        {"types": ",".join(kinds), "limit": _limit(limit, 100, ceiling=200),
         "exclude_archived": "true"},
        transport,
    )
    raw = [c for c in data.get("channels") or [] if isinstance(c, dict)]
    names = await _user_names(
        account, {c.get("user") for c in raw if c.get("is_im")}, transport, api,
    )
    channels = [_channel(c, names) for c in raw]
    logger.info("slack list: %d conversation(s)", len(channels))
    return {"channels": channels, "count": len(channels)}


# ── the shared request path ────────────────────────────────────────


def _refused(status: int, data: Any) -> bool:
    return status == 401 or (
        isinstance(data, dict) and data.get("ok") is False and data.get("error") in AUTH_ERRORS
    )


async def _get(account: Account, url: str, params: dict, transport) -> dict:
    status, data = await call(
        CONNECTOR, account, "GET", url, params=params, transport=transport, refused=_refused,
    )
    return _ok(status, data, retryable=True)


def _ok(status: int, data: Any, *, retryable: bool) -> dict:
    """Slack's answer, or the right kind of failure. ``retryable`` is the
    caller's idempotency: a rate limit or 5xx on a repeatable call is a lost
    answer, on a post it is definite (Slack does not act on either)."""
    if status == 429:
        if retryable:
            _fail("rate_limited")
            raise ConnectorLostAnswer("Slack rate-limited the request; try again shortly")
        raise _refusal("rate_limited", "Slack rate-limited the request; nothing was posted")
    if status >= 500:
        _fail("no_answer")
        raise ConnectorLostAnswer(f"Slack answered {status}")
    if not isinstance(data, dict):
        raise _refusal("bad_reply", f"Slack answered {status} with no readable body")
    if data.get("ok") is True:
        return data
    error = str(data.get("error") or f"http_{status}")
    if error == "missing_scope":
        raise _refusal(
            "insufficient_scope",
            "Slack refused (missing_scope): the workspace was connected before this "
            "permission was granted; reconnect it.",
        )
    if error in AUTH_ERRORS:
        raise _refusal("not_authorized", f"Slack refused the token ({error}); reconnect the workspace")
    detail = str(data.get("needed") or data.get("warning") or "")[:200]
    raise _refusal(f"slack_{error}", f"Slack refused ({error}) {detail}".strip())


def _refusal(code: str, message: str) -> ConnectorError:
    _fail(code)
    return ConnectorError(code, message)


def _unknown(detail: str) -> dict:
    logger.warning("slack send: outcome unknown")
    return {
        "status": "unknown",
        "detail": f"{detail}. The message may have been posted; do not post it again "
                  "without checking the channel.",
    }


def _fail(code: str) -> None:
    logger.warning("slack: failed (%s)", code)


def _limit(asked: int, default: int, *, ceiling: int = MAX_RESULTS) -> int:
    return max(1, min(int(asked or default), ceiling))


# ── shaping what the agent sees ────────────────────────────────────


def _text(message: dict) -> tuple[str, bool]:
    text = str(message.get("text") or "")
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS], True
    return text, False


def _match(m: dict) -> dict:
    channel = m.get("channel") if isinstance(m.get("channel"), dict) else {}
    text, truncated = _text(m)
    return {
        "channel": str(channel.get("id") or ""),
        "channel_name": str(channel.get("name") or ""),
        "user": str(m.get("user") or ""),
        "user_name": str(m.get("username") or ""),
        "text": text,
        "text_truncated": truncated,
        "ts": str(m.get("ts") or ""),
        "permalink": str(m.get("permalink") or ""),
    }


def _message(m: dict, names: dict[str, str]) -> dict:
    user = str(m.get("user") or "")
    text, truncated = _text(m)
    return {
        "ts": str(m.get("ts") or ""),
        "thread_ts": str(m.get("thread_ts") or ""),
        "user": user,
        "user_name": names.get(user, str(m.get("username") or "")),
        "text": text,
        "text_truncated": truncated,
        "reply_count": int(m.get("reply_count") or 0),
        "subtype": str(m.get("subtype") or ""),
    }


def _channel(c: dict, names: dict[str, str]) -> dict:
    if c.get("is_im"):
        kind, name = "im", names.get(str(c.get("user") or ""), str(c.get("user") or ""))
    elif c.get("is_mpim"):
        kind, name = "mpim", str(c.get("name") or "")
    elif c.get("is_private"):
        kind, name = "private_channel", str(c.get("name") or "")
    else:
        kind, name = "public_channel", str(c.get("name") or "")
    topic = c.get("topic") if isinstance(c.get("topic"), dict) else {}
    return {
        "id": str(c.get("id") or ""),
        "name": name,
        "kind": kind,
        "is_member": bool(c.get("is_member", kind in ("im", "mpim"))),
        "topic": str(topic.get("value") or ""),
    }


async def _user_names(account: Account, ids: set, transport, api: str) -> dict[str, str]:
    """Display names for author ids, best effort: an id that cannot be
    resolved is shown as the id. Bounded so a busy channel costs one
    request per author only up to the cap."""
    names: dict[str, str] = {}
    for user in sorted(u for u in ids if isinstance(u, str) and u)[:MAX_NAME_LOOKUPS]:
        try:
            data = await _get(account, f"{api}/users.info", {"user": user}, transport)
        except (ConnectorError, ConnectorLostAnswer):
            continue
        info = data.get("user") if isinstance(data.get("user"), dict) else {}
        profile = info.get("profile") if isinstance(info.get("profile"), dict) else {}
        name = profile.get("display_name") or info.get("real_name") or info.get("name")
        if name:
            names[user] = str(name)
    return names
