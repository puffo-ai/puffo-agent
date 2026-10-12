"""Discord as the connected user: identity scopes only (no messages), all reads."""

from __future__ import annotations

import logging
import urllib.parse
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

CONNECTOR = Connector(
    name="discord", label="Discord account",
    rt_type="PUFFO_DISCORD_OAUTH_v1", at_type="PUFFO_DISCORD_OAUTH_AT_v1",
)
API = "https://discord.com/api/v10"
# required by Discord
HEADERS = {"user-agent": "puffo-agent"}


async def whoami(
    credentials: AgentCredentials | None, *, from_account: str = "",
    transport: Transport | None = None, api: str = API,
) -> dict:
    account = await pick_account(CONNECTOR, credentials, from_account)
    user = await _get(account, f"{api}/users/@me", transport)
    if not isinstance(user, dict):
        raise _refusal("bad_reply", "Discord answered without a user")
    logger.info("discord whoami: %s", user.get("id", ""))
    return {
        "id": str(user.get("id") or ""),
        "username": str(user.get("username") or ""),
        "global_name": str(user.get("global_name") or ""),
        "email": str(user.get("email") or ""),
        "verified": bool(user.get("verified")),
    }


async def guilds(
    credentials: AgentCredentials | None, *, from_account: str = "",
    transport: Transport | None = None, api: str = API,
) -> dict:
    account = await pick_account(CONNECTOR, credentials, from_account)
    listed = await _get(account, f"{api}/users/@me/guilds", transport)
    rows = [
        {
            "id": str(g.get("id") or ""),
            "name": str(g.get("name") or ""),
            "owner": bool(g.get("owner")),
            "permissions": str(g.get("permissions") or ""),
        }
        for g in (listed if isinstance(listed, list) else [])
        if isinstance(g, dict)
    ]
    logger.info("discord guilds: %d", len(rows))
    return {"guilds": rows, "count": len(rows)}


async def guild_member(
    credentials: AgentCredentials | None, *, guild_id: str, from_account: str = "",
    transport: Transport | None = None, api: str = API,
) -> dict:
    if not guild_id.strip():
        raise ConnectorError("bad_request", "guild_id is required")
    account = await pick_account(CONNECTOR, credentials, from_account)
    member = await _get(
        account,
        f"{api}/users/@me/guilds/{urllib.parse.quote(guild_id.strip(), safe='')}/member",
        transport,
    )
    if not isinstance(member, dict):
        raise _refusal("bad_reply", "Discord answered without a member")
    logger.info("discord member: guild %s", guild_id.strip())
    return {
        "guild_id": guild_id.strip(),
        "nick": str(member.get("nick") or ""),
        "roles": [str(r) for r in member.get("roles") or []],
        "joined_at": str(member.get("joined_at") or ""),
        "pending": bool(member.get("pending")),
    }


async def connections(
    credentials: AgentCredentials | None, *, from_account: str = "",
    transport: Transport | None = None, api: str = API,
) -> dict:
    account = await pick_account(CONNECTOR, credentials, from_account)
    listed = await _get(account, f"{api}/users/@me/connections", transport)
    rows = [
        {
            "type": str(c.get("type") or ""),
            "name": str(c.get("name") or ""),
            "id": str(c.get("id") or ""),
            "verified": bool(c.get("verified")),
        }
        for c in (listed if isinstance(listed, list) else [])
        if isinstance(c, dict)
    ]
    logger.info("discord connections: %d", len(rows))
    return {"connections": rows, "count": len(rows)}


def _refused(status: int, data: Any) -> bool:
    return status == 401


async def _get(account: Account, url: str, transport) -> Any:
    status, data = await call(
        CONNECTOR, account, "GET", url, headers=HEADERS, transport=transport, refused=_refused,
    )
    return _ok(status, data)


def _ok(status: int, data: Any) -> Any:
    if status == 429:
        wait = data.get("retry_after") if isinstance(data, dict) else None
        _fail("rate_limited")
        raise ConnectorLostAnswer(
            f"Discord rate-limited the request; try again in {wait or 'a few'} seconds"
        )
    if status >= 500:
        _fail("no_answer")
        raise ConnectorLostAnswer(f"Discord answered {status}")
    if 200 <= status < 300:
        if data is None:
            raise _refusal("bad_reply", f"Discord answered {status} with no readable body")
        return data
    if status == 401:
        raise _refusal("not_authorized", "Discord refused the token; reconnect the account")
    if status == 403:
        raise _refusal(
            "insufficient_scope",
            "Discord refused (403): the account was connected without this permission, "
            "or the user is not in that server.",
        )
    reason = str(data.get("message") or "")[:200] if isinstance(data, dict) else ""
    raise _refusal(f"discord_{status}", f"Discord refused ({status}) {reason}".strip())


def _refusal(code: str, message: str) -> ConnectorError:
    _fail(code)
    return ConnectorError(code, message)


def _fail(code: str) -> None:
    logger.warning("discord: failed (%s)", code)
