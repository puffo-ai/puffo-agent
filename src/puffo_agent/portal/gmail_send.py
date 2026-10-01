"""Send mail as a Google account shared with this agent.

An agent that reads "failed" sends again, so three outcomes stay distinct
(Jeff 227820): *sent*, *failed*, and *unknown* — went out, answer lost,
never retried. Only ids and codes reach the log (Boris 227821).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from email.message import EmailMessage
from email.policy import SMTP
from typing import Awaitable, Callable

import aiohttp

from ..crypto.http_session import create_remote_http_session
from .credentials import AgentCredentials

logger = logging.getLogger(__name__)

SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
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


def build_message(to: str, subject: str, body: str) -> bytes:
    """RFC 5322 bytes; Gmail sets From. The email package refuses CR/LF in a
    header, which is what blocks header injection."""
    if not to.strip():
        raise GmailSendError("bad_request", "to must name at least one recipient")
    msg = EmailMessage(policy=SMTP)
    try:
        msg["To"] = to
        msg["Subject"] = subject
    except ValueError as exc:
        raise GmailSendError("bad_request", f"invalid header: {exc}") from exc
    msg.set_content(body)
    return msg.as_bytes()


async def send(
    credentials: AgentCredentials | None,
    *,
    to: str,
    subject: str,
    body: str,
    from_account: str = "",
    transport: Transport | None = None,
    send_url: str = SEND_URL,
) -> dict:
    """Returns ``{"status": "sent" | "unknown", ...}``; raises when it
    definitely was not sent."""
    if credentials is None:
        raise GmailSendError(
            "no_credentials", "this agent cannot hold credentials (no operator recorded)"
        )
    raw = build_message(to, subject, body)
    index = await _pick_account(credentials, from_account)
    payload = json.dumps({"raw": base64.urlsafe_b64encode(raw).decode()}).encode()
    transport = transport or gmail_transport

    token = await _access_token(credentials, index, fresh=False)
    for attempt in (1, 2):
        try:
            status, reply = await transport(
                send_url, payload,
                {"authorization": f"Bearer {token}", "content-type": "application/json"},
            )
        except aiohttp.ClientConnectorError as exc:
            _log_failure("unreachable")
            raise GmailSendError("unreachable", "could not reach Gmail; nothing was sent") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return _unknown("the connection dropped after the request was sent")
        if status == 401 and attempt == 1:
            # A refused token means nothing was sent. One refresh.
            token = await _access_token(credentials, index, fresh=True)
            continue
        return _outcome(status, reply)
    raise AssertionError("unreachable")  # pragma: no cover


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
