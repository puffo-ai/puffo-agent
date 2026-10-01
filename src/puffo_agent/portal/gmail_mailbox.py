"""Read and organise the mailbox shared with this agent (``gmail.modify``).

Split from ``gmail_send`` by the outcome model, not the subject: everything
here is idempotent, so a lost answer is a failure the agent may retry, not
an "unknown". The scope stops short of ``mail.google.com``, so nothing here
deletes mail permanently. Only ids and codes reach the log.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import urllib.parse
from email import message_from_bytes
from email.policy import default as default_policy

import aiohttp

from .credentials import AgentCredentials
from .gmail_send import GmailSendError, Transport, _access_token, _pick_account, gmail_transport

logger = logging.getLogger(__name__)

API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Past this the body would bury the agent's own context; the cut is flagged.
MAX_BODY_CHARS = 20_000
MAX_RESULTS = 50

# The user's words, mapped to the label edit Gmail wants. No delete: the
# scope does not grant one.
LABEL_ACTIONS = {
    "archive": ([], ["INBOX"]),
    "move_to_inbox": (["INBOX"], []),
    "mark_read": ([], ["UNREAD"]),
    "mark_unread": (["UNREAD"], []),
    "star": (["STARRED"], []),
    "unstar": ([], ["STARRED"]),
}


class GmailLostAnswer(Exception):
    """The answer was lost. Retryable, since everything here is idempotent."""


async def search(
    credentials: AgentCredentials | None,
    *,
    query: str = "",
    limit: int = 10,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Messages matching a Gmail search, newest first, with a summary line."""
    account = await _account(credentials, from_account)
    limit = max(1, min(int(limit or 10), MAX_RESULTS))
    params = {"maxResults": str(limit)}
    if query:
        params["q"] = query
    listed = await _get(account, f"{api}/messages", params, transport)
    ids = [m["id"] for m in (listed.get("messages") or []) if isinstance(m.get("id"), str)]
    messages = []
    for message_id in ids:
        full = await _get(
            account, f"{api}/messages/{urllib.parse.quote(message_id, safe='')}",
            {"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
            transport,
        )
        messages.append(_summary(full))
    logger.info("gmail search: %d result(s)", len(messages))
    return {"messages": messages, "count": len(messages)}


async def read(
    credentials: AgentCredentials | None,
    *,
    message_id: str,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """One message: its headers, its plain-text body, and attachment names."""
    account = await _account(credentials, from_account)
    if not message_id.strip():
        raise GmailSendError("bad_request", "message_id is required")
    full = await _get(
        account, f"{api}/messages/{urllib.parse.quote(message_id.strip(), safe='')}",
        {"format": "raw"}, transport,
    )
    raw = full.get("raw")
    if not isinstance(raw, str):
        raise GmailSendError("bad_reply", "Gmail returned a message without its content")
    try:
        parsed = message_from_bytes(_b64(raw), policy=default_policy)
    except Exception as exc:  # noqa: BLE001 - a message we cannot parse is not readable
        raise GmailSendError("bad_reply", f"could not read the message: {type(exc).__name__}") from exc
    body, truncated = _plain_body(parsed)
    logger.info("gmail read: %s", full.get("id", ""))
    return {
        "message_id": full.get("id", ""),
        "thread_id": full.get("threadId", ""),
        "from": str(parsed.get("From", "")),
        "to": str(parsed.get("To", "")),
        "cc": str(parsed.get("Cc", "")),
        "subject": str(parsed.get("Subject", "")),
        "date": str(parsed.get("Date", "")),
        "labels": list(full.get("labelIds") or []),
        "body": body,
        "body_truncated": truncated,
        "attachments": _attachment_names(parsed),
    }


async def _modify(
    credentials, message_id: str, action: str, from_account: str, transport, api: str,
) -> dict:
    account = await _account(credentials, from_account)
    add, remove = LABEL_ACTIONS[action]
    data = await _post(
        account,
        f"{api}/messages/{urllib.parse.quote(message_id.strip(), safe='')}/modify",
        {"addLabelIds": add, "removeLabelIds": remove}, transport,
    )
    logger.info("gmail modify: %s %s", action, data.get("id", ""))
    return {"status": "done", "action": action, "message_id": data.get("id", ""),
            "labels": list(data.get("labelIds") or [])}


async def _trash(
    credentials, message_id: str, restore: bool, from_account: str, transport, api: str,
) -> dict:
    account = await _account(credentials, from_account)
    verb = "untrash" if restore else "trash"
    data = await _post(
        account,
        f"{api}/messages/{urllib.parse.quote(message_id.strip(), safe='')}/{verb}",
        {}, transport,
    )
    logger.info("gmail %s: %s", verb, data.get("id", ""))
    return {"status": "done", "action": verb, "message_id": data.get("id", ""),
            "labels": list(data.get("labelIds") or [])}


TRASH_ACTIONS = {"trash": False, "untrash": True}

ACTIONS = sorted(set(LABEL_ACTIONS) | set(TRASH_ACTIONS))


async def organize(
    credentials: AgentCredentials | None,
    *,
    message_id: str,
    action: str,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """One vocabulary for every way an agent may file a message."""
    if not message_id.strip():
        raise GmailSendError("bad_request", "message_id is required")
    if action in TRASH_ACTIONS:
        return await _trash(
            credentials, message_id, TRASH_ACTIONS[action], from_account, transport, api,
        )
    if action not in LABEL_ACTIONS:
        raise GmailSendError(
            "bad_request", f"{action!r} is not an action; one of: " + ", ".join(ACTIONS)
        )
    return await _modify(credentials, message_id, action, from_account, transport, api)


# ── the shared request path ────────────────────────────────────────


class _Account:
    def __init__(self, credentials: AgentCredentials, index: int):
        self.credentials, self.index = credentials, index


async def _account(credentials: AgentCredentials | None, from_account: str) -> _Account:
    if credentials is None:
        raise GmailSendError(
            "no_credentials", "this agent cannot hold credentials (no operator recorded)"
        )
    return _Account(credentials, await _pick_account(credentials, from_account))


async def _get(account, url: str, params: dict, transport) -> dict:
    query = urllib.parse.urlencode(params, doseq=True)
    return await _call(account, "GET", f"{url}?{query}" if query else url, None, transport)


async def _post(account, url: str, payload: dict, transport) -> dict:
    return await _call(account, "POST", url, json.dumps(payload).encode(), transport)


async def _call(account, method: str, url: str, payload, transport) -> dict:
    """One authed call, refreshing the token once on a 401. A lost answer
    raises ``GmailLostAnswer``, not a definite failure."""
    transport = transport or gmail_transport
    token = await _access_token(account.credentials, account.index, fresh=False)
    for attempt in (1, 2):
        headers = {"authorization": f"Bearer {token}"}
        if payload is not None:
            headers["content-type"] = "application/json"
        try:
            status, reply = await transport(url, payload, headers, method=method)
        except aiohttp.ClientConnectorError as exc:
            _fail("unreachable")
            raise GmailSendError("unreachable", "could not reach Gmail") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _fail("no_answer")
            raise GmailLostAnswer("the connection dropped; nothing was changed twice") from exc
        if status == 401 and attempt == 1:
            token = await _access_token(account.credentials, account.index, fresh=True)
            continue
        return _read_reply(status, reply)
    raise AssertionError("unreachable")  # pragma: no cover


def _read_reply(status: int, reply: bytes) -> dict:
    try:
        data = json.loads(reply) if reply else {}
    except ValueError:
        data = None
    if 200 <= status < 300:
        if isinstance(data, dict):
            return data
        raise GmailSendError("bad_reply", f"Gmail answered {status} with no readable body")
    if status >= 500:
        _fail("no_answer")
        raise GmailLostAnswer(f"Gmail answered {status}")
    if status == 403:
        # Usually a token granted before the scope widened; a refresh keeps
        # the old scope, so only re-consent fixes it.
        _fail("gmail_403")
        raise GmailSendError(
            "insufficient_scope",
            "Gmail refused (403). The account was most likely connected before "
            "mailbox access was granted; reconnect it to grant the wider scope.",
        )
    reason = ""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        reason = str(data["error"].get("message", ""))[:200]
    _fail(f"gmail_{status}")
    raise GmailSendError(f"gmail_{status}", f"Gmail refused ({status}) {reason}".strip())


def _fail(code: str) -> None:
    logger.warning("gmail mailbox: failed (%s)", code)


# ── reading a message ──────────────────────────────────────────────


def _b64(raw: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as exc:
        raise GmailSendError("bad_reply", "Gmail returned an unreadable message") from exc


def _summary(full: dict) -> dict:
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in (full.get("payload") or {}).get("headers") or []
    }
    labels = list(full.get("labelIds") or [])
    return {
        "message_id": full.get("id", ""),
        "thread_id": full.get("threadId", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": full.get("snippet", ""),
        "unread": "UNREAD" in labels,
        "labels": labels,
    }


def _plain_body(parsed) -> tuple[str, bool]:
    """text/plain wins, but an empty one falls through to the HTML: bulk
    senders ship a blank plain part beside the real content."""
    for preference in (("plain",), ("html",)):
        part = parsed.get_body(preferencelist=preference)
        if part is None:
            continue
        text = _part_text(part)
        if text.strip():
            if len(text) > MAX_BODY_CHARS:
                return text[:MAX_BODY_CHARS], True
            return text, False
    return "", False


def _part_text(part) -> str:
    try:
        text = part.get_content()
    except Exception:  # noqa: BLE001 - an undecodable part is not readable text
        return ""
    if not isinstance(text, str):
        return ""
    return _strip_tags(text) if part.get_content_subtype() == "html" else text


def _strip_tags(html: str) -> str:
    from html.parser import HTMLParser

    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts, self.skip = [], 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1
            elif tag in ("p", "br", "div", "tr", "li"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style") and self.skip:
                self.skip -= 1

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    reader = Text()
    reader.feed(html)
    lines = [line.strip() for line in "".join(reader.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def _attachment_names(parsed) -> list[str]:
    return [
        part.get_filename()
        for part in parsed.iter_attachments()
        if part.get_filename()
    ]
