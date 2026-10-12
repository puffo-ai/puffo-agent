"""Discord against a fake Discord: identity reads, refresh, 403/429 mapping."""

from types import SimpleNamespace as NS

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from puffo_agent.portal import discord
from puffo_agent.portal.oauth_connector import ConnectorError, ConnectorLostAnswer
from tests.test_oauth_connector import FRESH, TOKEN, Routed, Scripted, Wallet

API = "https://discord.test/api/v10"


def wallet(**kw):
    return Wallet(discord.CONNECTOR, accounts=kw.pop("accounts", ((0, "ali#1"),)), **kw)


def test_the_types_are_the_ones_puffo_server_provisions():
    """Literal server names (``DiscordProvider::NAME`` = "discord")."""
    assert (discord.CONNECTOR.rt_type, discord.CONNECTOR.at_type) == (
        "PUFFO_DISCORD_OAUTH_v1", "PUFFO_DISCORD_OAUTH_AT_v1")


@pytest.mark.asyncio
async def test_whoami_is_a_get_with_the_token_and_a_user_agent():
    api = Scripted((200, {"id": "1", "username": "ali", "global_name": "Ali", "email": "a@x", "verified": True}))
    out = await discord.whoami(wallet(), transport=api, api=API)
    assert out == {"id": "1", "username": "ali", "global_name": "Ali", "email": "a@x", "verified": True}
    (method, url, headers, body), = api.requests
    assert (method, url, body) == ("GET", f"{API}/users/@me", None)
    assert headers["authorization"] == f"Bearer {TOKEN}" and headers["user-agent"] == "puffo-agent"


@pytest.mark.asyncio
async def test_guilds_and_connections_shape_each_row_and_read_a_non_list_as_empty():
    api = Routed(API, {
        "/users/@me/guilds": (200, [{"id": "g1", "name": "Puffo", "owner": True, "permissions": "8"}, "junk"]),
        "/users/@me/connections": (200, {"not": "a list"}),
    })
    assert await discord.guilds(wallet(), transport=api, api=API) == {
        "guilds": [{"id": "g1", "name": "Puffo", "owner": True, "permissions": "8"}], "count": 1}
    assert await discord.connections(wallet(), transport=api, api=API) == {"connections": [], "count": 0}


@pytest.mark.asyncio
async def test_a_membership_is_read_for_the_quoted_guild_and_needs_an_id():
    api = Routed(API, {"/users/@me/guilds/g%2F1/member": (200, {"nick": "al", "roles": [1, "r2"], "joined_at": "2026", "pending": False})})
    out = await discord.guild_member(wallet(), guild_id=" g/1 ", transport=api, api=API)
    assert out == {"guild_id": "g/1", "nick": "al", "roles": ["1", "r2"], "joined_at": "2026", "pending": False}
    with pytest.raises(ConnectorError) as exc:
        await discord.guild_member(wallet(), guild_id=" ", transport=api, api=API)
    assert exc.value.code == "bad_request" and len(api.requests) == 1


@pytest.mark.asyncio
async def test_a_refused_token_is_refreshed_once_then_named_as_the_reason():
    api, w = Scripted((401, {"message": "401: Unauthorized"}), (200, {"id": "1"})), wallet()
    assert (await discord.whoami(w, transport=api, api=API))["id"] == "1"
    assert [h["authorization"] for _, _, h, _ in api.requests] == [f"Bearer {TOKEN}", f"Bearer {FRESH}"]
    with pytest.raises(ConnectorError) as exc:
        await discord.whoami(wallet(), transport=Scripted((401, {}), (401, {})), api=API)
    assert exc.value.code == "not_authorized" and "reconnect" in str(exc.value)


@pytest.mark.asyncio
async def test_a_403_points_at_the_grant_and_other_refusals_carry_discords_words():
    with pytest.raises(ConnectorError) as exc:
        await discord.guilds(wallet(), transport=Scripted((403, {"message": "Missing Access"})), api=API)
    assert exc.value.code == "insufficient_scope"
    with pytest.raises(ConnectorError) as exc:
        await discord.guild_member(wallet(), guild_id="g", transport=Scripted((404, {"message": "Unknown Guild"})), api=API)
    assert exc.value.code == "discord_404" and "Unknown Guild" in str(exc.value)


@pytest.mark.asyncio
async def test_a_rate_limit_or_5xx_is_retryable_and_a_bodyless_success_is_refused():
    with pytest.raises(ConnectorLostAnswer, match="in 2.5 seconds"):
        await discord.whoami(wallet(), transport=Scripted((429, {"retry_after": 2.5})), api=API)
    with pytest.raises(ConnectorLostAnswer):
        await discord.whoami(wallet(), transport=Scripted((502, {})), api=API)
    for answer in ((200, b"<html>"), (200, ["not", "a", "user"])):
        with pytest.raises(ConnectorError) as exc:
            await discord.whoami(wallet(), transport=Scripted(answer), api=API)
        assert exc.value.code == "bad_reply"
    with pytest.raises(ConnectorError) as exc:
        await discord.guild_member(wallet(), guild_id="g", transport=Scripted((200, [])), api=API)
    assert exc.value.code == "bad_reply"


@pytest.mark.asyncio
async def test_the_route_validates_each_op_and_reaches_the_handler(monkeypatch):
    from puffo_agent.portal import rpc_service

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/discord", rpc_service.connector_route("discord"))
    seen = []

    async def connector(ctx, *, provider, op, **fields):
        seen.append((provider, op, fields))
        return {"id": "1"}

    monkeypatch.setattr(rpc_service.host_mcp_handler, "connector", connector)
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: NS(credentials=wallet())
    try:
        async with TestClient(TestServer(app)) as http:
            assert (await http.post("/v1/rpc/a/discord", json={"op": "guild_member"})).status == 400
            assert (await http.post("/v1/rpc/a/discord", json={"op": "whoami", "guild_id": "g"})).status == 400
            resp = await http.post("/v1/rpc/a/discord", json={"op": "guild_member", "guild_id": "g", "from_account": "ali#1"})
            assert resp.status == 200 and await resp.json() == {"id": "1"}
    finally:
        rpc_service._RPC_RESOLVER = previous
    assert seen == [("discord", "guild_member", {"guild_id": "g", "from_account": "ali#1"})]


@pytest.mark.asyncio
async def test_the_handler_and_the_tools_dispatch_every_discord_operation(monkeypatch):
    from puffo_agent.mcp.core_connector_tools import register_connector_tools
    from puffo_agent.portal import host_mcp_handler

    calls = []

    async def fake(credentials, **kw):
        calls.append(kw)
        return {"ok": 1}

    for name in ("whoami", "guilds", "guild_member", "connections"):
        monkeypatch.setattr(discord, name, fake)
    for op, kw in (("whoami", {}), ("guilds", {}), ("guild_member", {"guild_id": "g"}), ("connections", {})):
        assert await host_mcp_handler.connector(NS(credentials=None), provider="discord", op=op, **kw) == {"ok": 1}
    assert calls == [{}, {}, {"guild_id": "g"}, {}]

    registered, forwarded = {}, []

    class FakeMcp:
        def tool(self):
            def decorate(fn):
                registered[fn.__name__] = fn
                return fn
            return decorate

    async def connector(provider, op, **kw):
        forwarded.append((provider, op, kw))
        return {}

    register_connector_tools(FakeMcp(), NS(rpc_client=NS(slack_send=None, connector=connector)))
    await registered["discord_whoami"](from_account="x")
    await registered["discord_guilds"]()
    await registered["discord_guild_member"](guild_id="g")
    await registered["discord_connections"]()
    assert forwarded == [
        ("discord", "whoami", {"from_account": "x"}),
        ("discord", "guilds", {"from_account": ""}),
        ("discord", "guild_member", {"guild_id": "g", "from_account": ""}),
        ("discord", "connections", {"from_account": ""}),
    ]
