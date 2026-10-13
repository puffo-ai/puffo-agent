"""Send mail as a Google account shared with this agent.

An agent that reads "failed" sends again, so three outcomes stay distinct:
*sent*, *failed*, and *unknown* — went out, answer lost, never retried.
Only ids and codes reach the log.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import urllib.parse
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import SMTP
from email.policy import default as default_policy
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from ..crypto.http_session import create_remote_http_session
from .credentials import AgentCredentials

logger = logging.getLogger(__name__)

API = "https://gmail.googleapis.com/gmail/v1/users/me"
SEND_URL = f"{API}/messages/send"
MAX_ATTACHMENTS = 10
# Gmail's ceiling for one message.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
RT_TYPE = "PUFFO_GOOGLE_OAUTH_v1"
AT_TYPE = "PUFFO_GOOGLE_OAUTH_AT_v1"

# ClientConnectorError means the request never went out; any other
# ClientError / TimeoutError means the answer was lost.
Transport = Callable[..., Awaitable[tuple[int, bytes]]]


class GmailSendError(Exception):
    """Definitely not sent. ``code`` is safe to log."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def build_message(
    to: str,
    subject: str,
    body: str,
    *,
    cc: str = "",
    bcc: str = "",
    html_body: str = "",
    in_reply_to: str = "",
    references: str = "",
    attachments: tuple = (),
) -> bytes:
    """RFC 5322 bytes; Gmail sets From. The email package refuses CR/LF in a
    header, which is what blocks header injection. ``attachments`` holds
    ``(filename, data)`` pairs or whole messages to forward."""
    if not to.strip():
        raise GmailSendError("bad_request", "to must name at least one recipient")
    msg = EmailMessage(policy=SMTP)
    try:
        msg["To"] = to
        if cc.strip():
            msg["Cc"] = cc
        if bcc.strip():
            msg["Bcc"] = bcc
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
    except ValueError as exc:
        raise GmailSendError("bad_request", f"invalid header: {exc}") from exc
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for item in attachments:
        if isinstance(item, EmailMessage):
            msg.add_attachment(item)
            continue
        name, data = item
        maintype, _, subtype = (
            mimetypes.guess_type(name)[0] or "application/octet-stream"
        ).partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


async def send(
    credentials: AgentCredentials | None,
    *,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    html_body: str = "",
    reply_to_message_id: str = "",
    forward_message_id: str = "",
    attachments: list | tuple = (),
    workspace: Path | None = None,
    from_account: str = "",
    transport: Transport | None = None,
    send_url: str = SEND_URL,
    api: str = API,
) -> dict:
    """Returns ``{"status": "sent" | "unknown", ...}``; raises when it
    definitely was not sent."""
    index = await _require_account(credentials, from_account)
    message = await _compose(
        credentials, index, to=to, subject=subject, body=body, cc=cc, bcc=bcc,
        html_body=html_body, reply_to_message_id=reply_to_message_id,
        forward_message_id=forward_message_id, attachments=attachments,
        workspace=workspace, transport=transport, api=api,
    )
    return await _deliver(credentials, index, send_url, message, transport, _outcome, _unknown)


async def create_draft(
    credentials: AgentCredentials | None,
    *,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    html_body: str = "",
    reply_to_message_id: str = "",
    forward_message_id: str = "",
    attachments: list | tuple = (),
    workspace: Path | None = None,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Same composition as ``send``, saved for review instead of sent.
    ``"unknown"`` means a draft may exist; check the drafts before retrying."""
    index = await _require_account(credentials, from_account)
    message = await _compose(
        credentials, index, to=to, subject=subject, body=body, cc=cc, bcc=bcc,
        html_body=html_body, reply_to_message_id=reply_to_message_id,
        forward_message_id=forward_message_id, attachments=attachments,
        workspace=workspace, transport=transport, api=api,
    )
    return await _deliver(
        credentials, index, f"{api}/drafts", {"message": message}, transport,
        _draft_outcome, _draft_unknown,
    )


async def send_draft(
    credentials: AgentCredentials | None,
    *,
    draft_id: str,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Send a saved draft. Same three outcomes as ``send``."""
    if not draft_id.strip():
        raise GmailSendError("bad_request", "draft_id is required")
    index = await _require_account(credentials, from_account)
    return await _deliver(
        credentials, index, f"{api}/drafts/send", {"id": draft_id.strip()}, transport,
        _outcome, _unknown,
    )


async def _require_account(credentials: AgentCredentials | None, from_account: str) -> int:
    if credentials is None:
        raise GmailSendError(
            "no_credentials", "this agent cannot hold credentials (no operator recorded)"
        )
    return await _pick_account(credentials, from_account)


async def _deliver(credentials, index: int, url: str, message: dict, transport, on_ok, on_lost):
    """POST once, refreshing the token once on a 401. A lost answer is
    ``on_lost``: the request may have taken effect."""
    payload = json.dumps(message).encode()
    transport = transport or gmail_transport
    token = await _access_token(credentials, index, fresh=False)
    for attempt in (1, 2):
        try:
            status, reply = await transport(
                url, payload,
                {"authorization": f"Bearer {token}", "content-type": "application/json"},
            )
        except aiohttp.ClientConnectorError as exc:
            _log_failure("unreachable")
            raise GmailSendError("unreachable", "could not reach Gmail; nothing was sent") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return on_lost("the connection dropped after the request was sent")
        if status == 401 and attempt == 1:
            # A refused token means nothing was sent. One refresh.
            token = await _access_token(credentials, index, fresh=True)
            continue
        return on_ok(status, reply)
    raise AssertionError("unreachable")  # pragma: no cover


async def _compose(
    credentials, index: int, *, to, subject, body, cc, bcc, html_body,
    reply_to_message_id, forward_message_id, attachments, workspace, transport, api,
) -> dict:
    """The ``{"raw", "threadId"?}`` Gmail takes. Every failure here comes
    before anything is sent, so it is definite."""
    reply_to, forward = reply_to_message_id.strip(), forward_message_id.strip()
    if reply_to and forward:
        raise GmailSendError("bad_request", "reply_to_message_id and forward_message_id exclude each other")
    files = _workspace_files(workspace, attachments)
    in_reply_to = references = thread_id = ""
    extra: list = []
    if reply_to:
        original = await _lookup(
            credentials, index, reply_to,
            {"format": "metadata",
             "metadataHeaders": ["Message-ID", "References", "Subject", "From", "Reply-To"]},
            transport, api,
        )
        headers = _headers(original)
        thread_id = original.get("threadId", "")
        in_reply_to = headers.get("message-id", "")
        references = f"{headers.get('references', '')} {in_reply_to}".strip()
        to = to or headers.get("reply-to") or headers.get("from", "")
        subject = subject or _prefixed("Re:", headers.get("subject", ""))
    elif forward:
        original = await _lookup(credentials, index, forward, {"format": "raw"}, transport, api)
        raw = original.get("raw")
        if not isinstance(raw, str):
            raise GmailSendError("bad_reply", "Gmail returned the message without its content; nothing was sent")
        try:
            forwarded = message_from_bytes(
                base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=default_policy,
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable original cannot be forwarded
            raise GmailSendError("bad_reply", "could not read the message to forward; nothing was sent") from exc
        thread_id = original.get("threadId", "")
        subject = subject or _prefixed("Fwd:", str(forwarded.get("Subject", "")))
        extra.append(forwarded)
    raw_bytes = build_message(
        to, subject, body, cc=cc, bcc=bcc, html_body=html_body,
        in_reply_to=in_reply_to, references=references, attachments=(*files, *extra),
    )
    if len(raw_bytes) > MAX_ATTACHMENT_BYTES * 4 // 3 + 1024 * 1024:
        raise GmailSendError("too_large", "the message is larger than Gmail accepts; nothing was sent")
    message = {"raw": base64.urlsafe_b64encode(raw_bytes).decode()}
    if thread_id:
        message["threadId"] = thread_id
    return message


async def _lookup(credentials, index: int, message_id: str, params: dict, transport, api: str) -> dict:
    from .gmail_mailbox import GmailLostAnswer, _Account, _get

    try:
        return await _get(
            _Account(credentials, index),
            f"{api}/messages/{urllib.parse.quote(message_id, safe='')}", params, transport,
        )
    except GmailLostAnswer as exc:
        _log_failure("lookup_lost")
        raise GmailSendError("lookup_failed", f"could not read the original message; nothing was sent: {exc}") from exc


def _headers(message: dict) -> dict:
    return {
        h.get("name", "").lower(): h.get("value", "")
        for h in (message.get("payload") or {}).get("headers") or []
    }


def _prefixed(prefix: str, subject: str) -> str:
    return subject if subject.lower().startswith(prefix.lower()) else f"{prefix} {subject}".strip()


def _workspace_files(workspace: Path | None, paths) -> list[tuple[str, bytes]]:
    """``(name, data)`` for each workspace-relative path. Same rule as chat
    attachments: relative, resolving inside the workspace, a regular file."""
    if isinstance(paths, str) or not all(isinstance(p, str) for p in paths):
        raise GmailSendError("bad_request", "attachments must be a list of workspace paths")
    paths = [p.strip() for p in paths if p.strip()]
    if not paths:
        return []
    if workspace is None:
        raise GmailSendError("no_workspace", "this agent has no workspace to attach from")
    if len(paths) > MAX_ATTACHMENTS:
        raise GmailSendError("bad_request", f"at most {MAX_ATTACHMENTS} attachments")
    root = Path(workspace).resolve()
    files, total = [], 0
    for rel in paths:
        if Path(rel).is_absolute():
            raise GmailSendError("bad_request", f"{rel!r}: give a path relative to your workspace")
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise GmailSendError("bad_request", f"{rel!r} is outside your workspace")
        if not target.is_file():
            raise GmailSendError("bad_request", f"{rel!r} is not a file")
        total += target.stat().st_size
        if total > MAX_ATTACHMENT_BYTES:
            raise GmailSendError("too_large", "attachments exceed Gmail's 25 MiB limit")
        files.append((target.name, target.read_bytes()))
    return files


async def _pick_account(credentials: AgentCredentials, from_account: str) -> int:
    held = await _before_sending("list_failed", credentials.held(RT_TYPE))
    if not held:
        raise GmailSendError("no_account", "no Google account is shared with this agent")
    if from_account:
        for index, alias in held:
            if alias == from_account:
                return index
        raise GmailSendError(
            "unknown_account",
            f"{from_account!r} is not shared with this agent; available: "
            + ", ".join(alias or f"#{index}" for index, alias in held),
        )
    if len(held) > 1:
        raise GmailSendError(
            "ambiguous_account",
            "several Google accounts are shared with this agent; pass from_account, one of: "
            + ", ".join(alias or f"#{index}" for index, alias in held),
        )
    return held[0][0]


async def _access_token(credentials: AgentCredentials, index: int, *, fresh: bool) -> str:
    held = None if fresh else await _before_sending("token_failed", credentials.get(AT_TYPE, index))
    if held is None:
        held = await _before_sending("refresh_failed", credentials.refresh(RT_TYPE, index))
    if held is None:
        _log_failure("not_held")
        raise GmailSendError(
            "not_held", "the Google account is no longer shared with this agent, or is switched off"
        )
    return held.value.decode()


async def _before_sending(code: str, call):
    """Anything failing before the request goes out: nothing was sent."""
    try:
        return await call
    except Exception as exc:  # noqa: BLE001 - reported as a definite, logged-by-code failure
        _log_failure(code)
        raise GmailSendError(code, f"nothing was sent: {type(exc).__name__}: {exc}") from exc


def _outcome(status: int, reply: bytes) -> dict:
    try:
        data = json.loads(reply) if reply else {}
    except ValueError:
        data = None
    if 200 <= status < 300:
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            logger.info("gmail send: sent %s", data["id"])
            return {"status": "sent", "message_id": data["id"],
                    "thread_id": data.get("threadId", "")}
        # Accepted with no readable id: it may well have gone out.
        return _unknown(f"Gmail answered {status} without a message id")
    if status >= 500:
        # A 5xx after the request went out says nothing definite.
        return _unknown(f"Gmail answered {status}")
    reason = ""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        reason = str(data["error"].get("message", ""))[:200]
    code = f"gmail_{status}"
    _log_failure(code)
    raise GmailSendError(code, f"Gmail refused the message ({status}) {reason}".strip())


def _draft_outcome(status: int, reply: bytes) -> dict:
    try:
        data = json.loads(reply) if reply else {}
    except ValueError:
        data = None
    if 200 <= status < 300 and isinstance(data, dict) and isinstance(data.get("id"), str):
        message = data.get("message") or {}
        logger.info("gmail draft: created %s", data["id"])
        return {"status": "created", "draft_id": data["id"],
                "message_id": message.get("id", ""), "thread_id": message.get("threadId", "")}
    if 200 <= status < 300 or status >= 500:
        return _draft_unknown(f"Gmail answered {status} without a draft id")
    reason = ""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        reason = str(data["error"].get("message", ""))[:200]
    code = f"gmail_{status}"
    _log_failure(code)
    raise GmailSendError(code, f"Gmail refused the draft ({status}) {reason}".strip())


def _draft_unknown(detail: str) -> dict:
    logger.warning("gmail draft: outcome unknown")
    return {
        "status": "unknown",
        "detail": f"{detail}. A draft may have been created; check gmail_list_drafts "
                  "before creating it again.",
    }


def _unknown(detail: str) -> dict:
    logger.warning("gmail send: outcome unknown")
    return {
        "status": "unknown",
        "detail": f"{detail}. The message may have been sent; do not send it again "
                  "without checking the Sent folder.",
    }


def _log_failure(code: str) -> None:
    logger.warning("gmail send: failed (%s)", code)


# Refused rather than buffered; Gmail will not accept a larger one anyway.
MAX_REPLY_BYTES = 30 * 1024 * 1024


async def gmail_transport(
    url: str, body: bytes | None, headers: dict, *, method: str = "POST",
) -> tuple[int, bytes]:
    async with create_remote_http_session(
        url, timeout=aiohttp.ClientTimeout(total=60)
    ) as session:
        async with session.request(
            method, url, data=body, headers=headers, allow_redirects=False,
        ) as resp:
            return resp.status, await _read_body(resp)


async def _read_body(resp) -> bytes:
    """Read to the end.

    ``StreamReader.read(n)`` returns what is buffered, not n bytes, so a
    chunked reply came back truncated and read as malformed JSON.
    """
    chunks, total = [], 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > MAX_REPLY_BYTES:
            raise GmailSendError("too_large", "Gmail's answer was too large to read")
        chunks.append(chunk)
    return b"".join(chunks)
