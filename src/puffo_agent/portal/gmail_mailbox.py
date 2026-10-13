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
import os
import re
import tempfile
import urllib.parse
from email import message_from_bytes
from pathlib import Path
from email.policy import default as default_policy

import aiohttp

from .credentials import AgentCredentials
from .gmail_send import GmailSendError, Transport, _access_token, _pick_account, gmail_transport

logger = logging.getLogger(__name__)

API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Past this the body would bury the agent's own context; the cut is flagged.
MAX_BODY_CHARS = 20_000
MAX_RESULTS = 50
# Gmail's own per-message attachment ceiling.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
# messages.batchModify takes at most this many ids per call.
MAX_BATCH_IDS = 1000
ATTACHMENT_DIR = "gmail-attachments"

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
    page_token: str = "",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Messages matching a Gmail search, newest first, with a summary line.
    ``next_page_token`` is set while more remain."""
    account = await _account(credentials, from_account)
    limit = max(1, min(int(limit or 10), MAX_RESULTS))
    params = {"maxResults": str(limit)}
    if query:
        params["q"] = query
    if page_token:
        params["pageToken"] = page_token
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
    return {
        "messages": messages,
        "count": len(messages),
        "next_page_token": listed.get("nextPageToken") or "",
    }


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


# ── labels ─────────────────────────────────────────────────────────


async def list_labels(
    credentials: AgentCredentials | None,
    *,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Every label, system and user, with the id the other tools accept."""
    account = await _account(credentials, from_account)
    labels = await _labels(account, transport, api)
    return {"labels": labels, "count": len(labels)}


async def create_label(
    credentials: AgentCredentials | None,
    *,
    name: str,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Idempotent: a label that already exists is returned, not duplicated."""
    name = name.strip()
    if not name:
        raise GmailSendError("bad_request", "name is required")
    account = await _account(credentials, from_account)
    try:
        data = await _post(
            account, f"{api}/labels",
            {"name": name, "labelListVisibility": "labelShow",
             "messageListVisibility": "show"},
            transport,
        )
    except GmailSendError as exc:
        if exc.code != "gmail_409":
            raise
        existing = _find_label(await _labels(account, transport, api), name)
        if existing is None:
            raise
        logger.info("gmail create_label: exists %s", existing["id"])
        return {"status": "exists", **existing}
    logger.info("gmail create_label: %s", data.get("id", ""))
    return {"status": "created", "id": data.get("id", ""), "name": data.get("name", name),
            "type": data.get("type", "user")}


async def label(
    credentials: AgentCredentials | None,
    *,
    message_ids: list,
    add: list | None = None,
    remove: list | None = None,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Add and remove labels on up to ``MAX_BATCH_IDS`` messages in one call.
    Labels are named or given by id; a name that does not exist is refused,
    so a typo never creates a label."""
    ids = _strings(message_ids, "message_ids")
    add_names, remove_names = _strings(add or [], "add"), _strings(remove or [], "remove")
    if not ids:
        raise GmailSendError("bad_request", "message_ids must name at least one message")
    if len(ids) > MAX_BATCH_IDS:
        raise GmailSendError("bad_request", f"at most {MAX_BATCH_IDS} messages per call")
    if not add_names and not remove_names:
        raise GmailSendError("bad_request", "add or remove must name at least one label")
    account = await _account(credentials, from_account)
    known = await _labels(account, transport, api)
    add_ids, remove_ids = _label_ids(known, add_names), _label_ids(known, remove_names)
    await _post(
        account, f"{api}/messages/batchModify",
        {"ids": ids, "addLabelIds": add_ids, "removeLabelIds": remove_ids}, transport,
    )
    logger.info("gmail label: %d message(s), +%d -%d", len(ids), len(add_ids), len(remove_ids))
    return {"status": "done", "count": len(ids), "added": add_ids, "removed": remove_ids}


async def _labels(account, transport, api: str) -> list[dict]:
    data = await _get(account, f"{api}/labels", {}, transport)
    return [
        {"id": item["id"], "name": item.get("name", item["id"]),
         "type": item.get("type", "user")}
        for item in data.get("labels") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def _find_label(labels: list[dict], wanted: str) -> dict | None:
    """By id first, then by name ignoring case, as Gmail itself does."""
    for item in labels:
        if item["id"] == wanted:
            return item
    folded = wanted.casefold()
    for item in labels:
        if item["name"].casefold() == folded:
            return item
    return None


def _label_ids(labels: list[dict], wanted: list[str]) -> list[str]:
    ids, missing = [], []
    for name in wanted:
        found = _find_label(labels, name)
        if found is None:
            missing.append(name)
        else:
            ids.append(found["id"])
    if missing:
        raise GmailSendError(
            "unknown_label",
            "no such label: " + ", ".join(repr(m) for m in missing)
            + ". Create it first with gmail_create_label.",
        )
    return ids


def _strings(value, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise GmailSendError("bad_request", f"{field} must be a list of strings")
    return [v.strip() for v in value if v.strip()]


# ── drafts ─────────────────────────────────────────────────────────


async def list_drafts(
    credentials: AgentCredentials | None,
    *,
    limit: int = 10,
    page_token: str = "",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Drafts, newest first, each with the ``draft_id`` that sends it."""
    account = await _account(credentials, from_account)
    params = {"maxResults": str(max(1, min(int(limit or 10), MAX_RESULTS)))}
    if page_token:
        params["pageToken"] = page_token
    listed = await _get(account, f"{api}/drafts", params, transport)
    drafts = []
    for item in listed.get("drafts") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        full = await _get(
            account, f"{api}/drafts/{urllib.parse.quote(item['id'], safe='')}",
            {"format": "metadata"}, transport,
        )
        drafts.append({"draft_id": item["id"], **_summary(full.get("message") or {})})
    logger.info("gmail list_drafts: %d", len(drafts))
    return {
        "drafts": drafts,
        "count": len(drafts),
        "next_page_token": listed.get("nextPageToken") or "",
    }


# ── attachments ────────────────────────────────────────────────────


async def get_attachment(
    credentials: AgentCredentials | None,
    *,
    message_id: str,
    filename: str,
    workspace: Path | None,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Save one attachment into the workspace and return its relative path.
    Saving again overwrites, so a retry is safe."""
    message_id, filename = message_id.strip(), filename.strip()
    if not message_id or not filename:
        raise GmailSendError("bad_request", "message_id and filename are required")
    if workspace is None:
        raise GmailSendError("no_workspace", "this agent has no workspace to save into")
    account = await _account(credentials, from_account)
    quoted = urllib.parse.quote(message_id, safe="")
    full = await _get(account, f"{api}/messages/{quoted}", {"format": "full"}, transport)
    parts = [p for p in _parts(full.get("payload") or {}) if p.get("filename")]
    matches = [p for p in parts if p["filename"] == filename]
    if not matches:
        raise GmailSendError(
            "unknown_attachment",
            f"{filename!r} is not attached; attached: "
            + (", ".join(repr(p["filename"]) for p in parts) or "nothing"),
        )
    part = matches[0]
    body = part.get("body") or {}
    if int(body.get("size") or 0) > MAX_ATTACHMENT_BYTES:
        raise GmailSendError("too_large", f"{filename!r} is larger than 25 MiB")
    if isinstance(body.get("data"), str):
        data = _b64(body["data"])
    elif isinstance(body.get("attachmentId"), str):
        fetched = await _get(
            account,
            f"{api}/messages/{quoted}/attachments/"
            f"{urllib.parse.quote(body['attachmentId'], safe='')}",
            {}, transport,
        )
        if not isinstance(fetched.get("data"), str):
            raise GmailSendError("bad_reply", "Gmail returned the attachment without its data")
        data = _b64(fetched["data"])
    else:
        raise GmailSendError("bad_reply", "Gmail returned the attachment without its data")
    saved = _save(Path(workspace), message_id, filename, data)
    logger.info("gmail get_attachment: %s (%d bytes)", message_id, len(data))
    return {"status": "saved", "path": saved, "size": len(data),
            "mime_type": part.get("mimeType", "")}


def _parts(payload: dict):
    yield payload
    for child in payload.get("parts") or []:
        if isinstance(child, dict):
            yield from _parts(child)


def _safe_name(name: str, fallback: str) -> str:
    """A bare file name: no directories, no control characters, no leading dots."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().lstrip(".")
    return name[:200] or fallback


def _save(workspace: Path, message_id: str, filename: str, data: bytes) -> str:
    """Write under ``<workspace>/gmail-attachments/<message>/``, refusing any
    path that resolves outside the workspace, a symlinked directory included."""
    root = workspace.resolve()
    folder = root / ATTACHMENT_DIR / _safe_name(message_id, "message")
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(root):
        raise GmailSendError("bad_request", "the attachment folder escapes the workspace")
    target = folder / _safe_name(filename, "attachment")
    # A temp file then a rename: the rename replaces a symlink at the target
    # rather than writing through it.
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return str(target.relative_to(root))


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
