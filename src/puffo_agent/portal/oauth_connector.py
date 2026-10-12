"""The request path shared by every provider an operator connects through
Settings → Apps: pick the shared account, carry its access token, refresh it
once when the provider refuses it. What a provider's reply *means* stays in
the provider module; only ids and codes reach the log.

Two failure kinds, because the caller's retry policy differs: a
``ConnectorError`` is definite (nothing happened, or the provider refused),
a ``ConnectorLostAnswer`` means the request may have been acted on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

from ..crypto.http_session import create_remote_http_session
from .credentials import AgentCredentials

logger = logging.getLogger(__name__)

Transport = Callable[..., Awaitable[tuple[int, bytes]]]
# (status, parsed body or None) → "the token was refused".
Refused = Callable[[int, Any], bool]

# Refused rather than buffered; no provider answer should be anywhere near it.
MAX_REPLY_BYTES = 30 * 1024 * 1024


@dataclass(frozen=True)
class Connector:
    """One provider as the credential subsystem knows it."""

    name: str
    label: str
    rt_type: str
    at_type: str


@dataclass(frozen=True)
class Account:
    credentials: AgentCredentials
    index: int


class ConnectorError(Exception):
    """Definite: the request did not happen, or the provider refused it.
    ``code`` is safe to log."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ConnectorLostAnswer(Exception):
    """The request went out and the answer was lost."""


async def pick_account(
    connector: Connector, credentials: AgentCredentials | None, from_account: str,
) -> Account:
    if credentials is None:
        raise ConnectorError(
            "no_credentials", "this agent cannot hold credentials (no operator recorded)"
        )
    held = await _before_request(connector, "list_failed", credentials.held(connector.rt_type))
    if not held:
        raise ConnectorError("no_account", f"no {connector.label} is shared with this agent")
    if from_account:
        for index, alias in held:
            if alias == from_account:
                return Account(credentials, index)
        raise ConnectorError(
            "unknown_account",
            f"{from_account!r} is not shared with this agent; available: " + _names(held),
        )
    if len(held) > 1:
        raise ConnectorError(
            "ambiguous_account",
            f"several {connector.label}s are shared with this agent; pass from_account, "
            "one of: " + _names(held),
        )
    return Account(credentials, held[0][0])


def _names(held: list[tuple[int, str]]) -> str:
    return ", ".join(alias or f"#{index}" for index, alias in held)


async def access_token(connector: Connector, account: Account, *, fresh: bool) -> str:
    credentials, index = account.credentials, account.index
    held = None
    if not fresh:
        held = await _before_request(
            connector, "token_failed", credentials.get(connector.at_type, index)
        )
    if held is None:
        held = await _before_request(
            connector, "refresh_failed", credentials.refresh(connector.rt_type, index)
        )
    if held is None:
        _fail(connector, "not_held")
        raise ConnectorError(
            "not_held",
            f"the {connector.label} is no longer shared with this agent, or is switched off",
        )
    return held.value.decode()


async def _before_request(connector: Connector, code: str, call):
    """Anything failing before the request goes out is definite."""
    try:
        return await call
    except Exception as exc:  # noqa: BLE001 - reported as a definite, logged-by-code failure
        _fail(connector, code)
        raise ConnectorError(code, f"nothing was sent: {type(exc).__name__}: {exc}") from exc


async def call(
    connector: Connector,
    account: Account,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    payload: dict | None = None,
    headers: dict | None = None,
    transport: Transport | None = None,
    refused: Refused,
) -> tuple[int, Any]:
    """One authed request, retried once with a fresh token when ``refused``
    says the provider turned the token away. Returns the status and the
    parsed JSON body (``None`` when the body is not JSON)."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    body = json.dumps(payload).encode() if payload is not None else None
    transport = transport or http_transport
    token = await access_token(connector, account, fresh=False)
    for attempt in (1, 2):
        sent = {**(headers or {}), "authorization": f"Bearer {token}"}
        if body is not None:
            sent["content-type"] = "application/json; charset=utf-8"
        try:
            status, reply = await transport(url, body, sent, method=method)
        except aiohttp.ClientConnectorError as exc:
            _fail(connector, "unreachable")
            raise ConnectorError(
                "unreachable", f"could not reach {connector.label.split()[0].capitalize()}"
            ) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            _fail(connector, "no_answer")
            raise ConnectorLostAnswer("the connection dropped after the request was sent") from exc
        data = _json(reply)
        if attempt == 1 and refused(status, data):
            token = await access_token(connector, account, fresh=True)
            continue
        return status, data
    raise AssertionError("unreachable")  # pragma: no cover


def _json(reply: bytes) -> Any:
    try:
        return json.loads(reply) if reply else {}
    except ValueError:
        return None


def _fail(connector: Connector, code: str) -> None:
    logger.warning("%s: failed (%s)", connector.name, code)


async def http_transport(
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
    """Read to the end: ``read(n)`` returns what is buffered, not n bytes."""
    chunks, total = [], 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > MAX_REPLY_BYTES:
            raise ConnectorError("too_large", "the provider's answer was too large to read")
        chunks.append(chunk)
    return b"".join(chunks)
