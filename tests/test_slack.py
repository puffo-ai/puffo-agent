"""Slack against a fake Slack.

The cells that matter: posting keeps "unknown" distinct from failure, Slack's
``ok: false`` is read as the status it is, a bad token is refreshed once and
then named, repeatable calls stay retryable, and tokens and message text stay
out of the log.
"""

import logging
from types import SimpleNamespace as NS

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from puffo_agent.mcp._host_mcp import PuffoRpcClient
from puffo_agent.portal import slack
from puffo_agent.portal.oauth_connector import ConnectorError, ConnectorLostAnswer
from tests.test_oauth_connector import FRESH, TOKEN, Routed, Scripted, Wallet

API = "https://slack.test/api"


def wallet(**kw):
    return Wallet(slack.CONNECTOR, accounts=kw.pop("accounts", ((0, "acme-hq"),)), **kw)


def ok(**fields):
    return 200, {"ok": True, **fields}


def refused(error):
    return 200, {"ok": False, "error": error}


async def _send(w, api, **kw):
    kw = {"channel": "C123", "text": "hello there", **kw}
    return await slack.send(w, transport=api, api=API, **kw)


# ── posting ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_posts_with_the_held_token_in_a_thread_when_asked():
    api = Scripted(ok(channel="C123", ts="1.2"))
    assert await _send(wallet(), api, thread_ts="1.0") == {"status": "sent", "channel": "C123", "ts": "1.2"}
    (method, url, headers, body), = api.requests
    assert (method, url) == ("POST", f"{API}/chat.postMessage")
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert body == {"channel": "C123", "text": "hello there", "thread_ts": "1.0"}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["channel", "text"])
async def test_a_post_with_no_channel_or_text_never_reaches_slack(field):
    api = Scripted()
    with pytest.raises(ConnectorError) as exc:
        await _send(wallet(), api, **{field: "  "})
    assert exc.value.code == "bad_request" and api.requests == []


@pytest.mark.asyncio
async def test_slacks_ok_false_is_a_definite_refusal_carrying_its_error():
    with pytest.raises(ConnectorError) as exc:
        await _send(wallet(), Scripted(refused("channel_not_found")))
    assert exc.value.code == "slack_channel_not_found" and "channel_not_found" in str(exc.value)


@pytest.mark.asyncio
async def test_a_bad_token_is_refreshed_once_then_named_as_the_reason():
    api, w = Scripted(refused("invalid_auth"), ok(channel="C1", ts="9")), wallet()
    assert (await _send(w, api))["status"] == "sent"
    assert [h["authorization"] for _, _, h, _ in api.requests] == [f"Bearer {TOKEN}", f"Bearer {FRESH}"]
    with pytest.raises(ConnectorError) as exc:
        await _send(wallet(), Scripted(refused("token_revoked"), refused("token_revoked")))
    assert exc.value.code == "not_authorized" and "reconnect" in str(exc.value)


@pytest.mark.asyncio
async def test_a_missing_scope_points_at_reconnecting_not_at_a_bug():
    with pytest.raises(ConnectorError) as exc:
        await _send(wallet(), Scripted(refused("missing_scope")))
    assert exc.value.code == "insufficient_scope" and "reconnect" in str(exc.value)


@pytest.mark.asyncio
async def test_a_rate_limited_post_is_definite_because_slack_did_not_act():
    with pytest.raises(ConnectorError) as exc:
        await _send(wallet(), Scripted((429, {"ok": False, "error": "ratelimited"})))
    assert exc.value.code == "rate_limited"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [aiohttp.ServerDisconnectedError(), (503, {}), (200, b"<html>")],
                         ids=["disconnect", "5xx", "2xx-not-json"])
async def test_a_lost_answer_to_a_post_is_unknown_and_never_reposted(answer):
    api = Scripted(answer)
    if isinstance(answer, tuple) and answer[0] == 200:
        with pytest.raises(ConnectorError) as exc:
            await _send(wallet(), api)
        assert exc.value.code == "bad_reply"
        return
    result = await _send(wallet(), api)
    assert result["status"] == "unknown" and "may have been posted" in result["detail"]
    assert len(api.requests) == 1


# ── reading ─────────────────────────────────────────────────────────


def users(**names):
    return {f"/users.info": [ok(user={"id": uid, "name": n, "real_name": n.title()}) for uid, n in names.items()]}


@pytest.mark.asyncio
async def test_a_search_asks_newest_first_clamps_the_limit_and_shapes_each_hit():
    hit = {"channel": {"id": "C1", "name": "general"}, "user": "U1", "username": "alice",
           "text": "x" * (slack.MAX_TEXT_CHARS + 5), "ts": "3.4", "permalink": "https://p"}
    api = Routed(API, {"/search.messages": ok(messages={"total": 1, "matches": [hit, "junk"]})})
    out = await slack.search(wallet(), query="deploy", limit=999, transport=api, api=API)
    assert out["count"] == 1 and out["total"] == 1
    assert out["messages"][0]["text_truncated"] is True and len(out["messages"][0]["text"]) == slack.MAX_TEXT_CHARS
    assert {k: out["messages"][0][k] for k in ("channel", "channel_name", "user", "user_name", "ts", "permalink")} == {
        "channel": "C1", "channel_name": "general", "user": "U1", "user_name": "alice", "ts": "3.4", "permalink": "https://p"}
    (method, url, _, body), = api.requests
    assert method == "GET" and body is None
    assert url == f"{API}/search.messages?query=deploy&count=50&sort=timestamp&sort_dir=desc"


@pytest.mark.asyncio
async def test_a_blank_query_or_channel_is_refused_before_any_request():
    api = Scripted()
    with pytest.raises(ConnectorError) as exc:
        await slack.search(wallet(), query=" ", transport=api, api=API)
    assert exc.value.code == "bad_request"
    with pytest.raises(ConnectorError) as exc:
        await slack.read(wallet(), channel="", transport=api, api=API)
    assert exc.value.code == "bad_request" and api.requests == []


@pytest.mark.asyncio
async def test_reading_a_channel_names_the_authors_it_can_and_leaves_the_rest_as_ids():
    """A lookup that fails, and one that answers without a name, both leave the id."""
    history = ok(has_more=True, messages=[
        {"ts": "3", "user": "U1", "text": "hi", "reply_count": 2},
        {"ts": "2", "user": "U2", "text": "yo", "subtype": "bot_message", "username": "deploybot"},
        {"ts": "1", "user": "U3", "text": "?"},
    ])
    api = Routed(API, {"/conversations.history": history,
                       "/users.info": [ok(user={"id": "U1", "profile": {"display_name": "ali"}}),
                                       ok(user={"id": "U2", "profile": {}}),
                                       refused("user_not_found")]})
    out = await slack.read(wallet(), channel="C1", limit=0, transport=api, api=API)
    assert out["has_more"] is True and out["count"] == 3
    assert [(m["user"], m["user_name"], m["reply_count"], m["subtype"]) for m in out["messages"]] == [
        ("U1", "ali", 2, ""), ("U2", "deploybot", 0, "bot_message"), ("U3", "", 0, "")]
    assert api.requests[0][1] == f"{API}/conversations.history?channel=C1&limit=20"
    assert sorted(r[1].split("user=")[1] for r in api.requests[1:]) == ["U1", "U2", "U3"]


@pytest.mark.asyncio
async def test_a_thread_is_read_through_replies_with_its_root_ts():
    api = Routed(API, {"/conversations.replies": ok(messages=[])})
    out = await slack.read(wallet(), channel="C1", thread_ts="1.0", transport=api, api=API)
    assert out == {"channel": "C1", "messages": [], "count": 0, "has_more": False}
    assert api.requests[0][1] == f"{API}/conversations.replies?channel=C1&limit=20&ts=1.0"


@pytest.mark.asyncio
async def test_name_lookups_are_bounded_so_a_busy_channel_is_not_one_request_per_author():
    many = ok(messages=[{"ts": str(i), "user": f"U{i:03d}", "text": "."} for i in range(40)])
    api = Routed(API, {"/conversations.history": many,
                       "/users.info": [ok(user={"id": "x", "name": "n"})] * slack.MAX_NAME_LOOKUPS})
    await slack.read(wallet(), channel="C1", transport=api, api=API)
    assert len(api.requests) == 1 + slack.MAX_NAME_LOOKUPS


@pytest.mark.asyncio
async def test_listing_asks_for_every_kind_by_default_and_names_a_dm_after_the_other_person():
    listed = ok(channels=[
        {"id": "C1", "name": "general", "is_member": True, "topic": {"value": "chat"}},
        {"id": "G1", "name": "secret", "is_private": True, "is_member": False},
        {"id": "D1", "is_im": True, "user": "U9"},
        {"id": "M1", "name": "mpdm-a--b-1", "is_mpim": True},
    ])
    api = Routed(API, {"/conversations.list": listed, "/users.info": [ok(user={"id": "U9", "real_name": "Bob"})]})
    out = await slack.list_channels(wallet(), transport=api, api=API)
    assert [(c["id"], c["name"], c["kind"], c["is_member"], c["topic"]) for c in out["channels"]] == [
        ("C1", "general", "public_channel", True, "chat"), ("G1", "secret", "private_channel", False, ""),
        ("D1", "Bob", "im", True, ""), ("M1", "mpdm-a--b-1", "mpim", True, "")]
    assert api.requests[0][1] == (f"{API}/conversations.list?types=public_channel%2Cprivate_channel"
                                  "%2Cmpim%2Cim&limit=100&exclude_archived=true")


@pytest.mark.asyncio
async def test_a_kind_slack_does_not_have_is_refused_before_any_request():
    api = Scripted()
    with pytest.raises(ConnectorError, match="public_channel") as exc:
        await slack.list_channels(wallet(), types="im, dm", transport=api, api=API)
    assert exc.value.code == "bad_request" and api.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [(429, {"ok": False, "error": "ratelimited"}), (502, {}),
                                    aiohttp.ServerDisconnectedError()])
async def test_a_lost_answer_to_a_read_is_retryable_not_a_third_outcome(answer):
    with pytest.raises(ConnectorLostAnswer):
        await slack.search(wallet(), query="x", transport=Scripted(answer), api=API)


@pytest.mark.asyncio
async def test_the_log_carries_codes_and_ids_not_tokens_or_message_text(caplog):
    caplog.set_level(logging.DEBUG)
    await _send(wallet(), Scripted(ok(channel="C1", ts="7.7")))
    with pytest.raises(ConnectorError):
        await _send(wallet(), Scripted(refused("channel_not_found")))
    await _send(wallet(), Scripted(aiohttp.ServerDisconnectedError()))
    text = caplog.text
    assert "7.7" in text and "slack_channel_not_found" in text and "outcome unknown" in text
    for secret in (TOKEN, FRESH, "hello there"):
        assert secret not in text


# ── the daemon route and the RPC hop ────────────────────────────────


def _route_app():
    from puffo_agent.portal import rpc_service

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/slack-send", rpc_service.slack_send_route)
    app.router.add_post("/v1/rpc/{agent_id}/slack", rpc_service.connector_route("slack"))
    return app, rpc_service


@pytest.mark.asyncio
@pytest.mark.parametrize("path, payload, status, code", [
    ("slack-send", {"channel": "C1", "text": "x", "extra": 1}, 400, None),
    ("slack-send", {"channel": "C1", "text": 5}, 400, None),
    ("slack-send", {"channel": "C1", "text": "x"}, 409, "no_worker"),
    ("slack", {"op": "shout"}, 400, None),
    ("slack", {"op": "search"}, 400, None),
    ("slack", {"op": "read", "channel": "C1", "limit": "ten"}, 400, None),
    ("slack", {"op": "list_channels", "limit": True}, 400, None),
    ("slack", {"op": "search", "query": "x"}, 409, "no_worker"),
])
async def test_the_routes_refuse_bad_bodies_and_report_no_worker_plainly(path, payload, status, code):
    app, rpc_service = _route_app()
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: None
    try:
        async with TestClient(TestServer(app)) as http:
            resp = await http.post(f"/v1/rpc/agent-1/{path}", json=payload)
            assert resp.status == status
            if code:
                assert (await resp.json())["code"] == code
    finally:
        rpc_service._RPC_RESOLVER = previous


@pytest.mark.asyncio
async def test_the_routes_reach_the_worker_and_keep_each_failure_kind(monkeypatch):
    app, rpc_service = _route_app()
    seen = []

    async def connector(ctx, *, provider, op, **fields):
        seen.append((provider, op, fields))
        if op == "read":
            raise ConnectorLostAnswer("Slack answered 502")
        if op == "list_channels":
            raise ConnectorError("bad_request", "nope")
        return {"count": 0}

    async def slack_send(ctx, **fields):
        seen.append(("send", fields))
        if fields["channel"] == "nope":
            raise ConnectorError("slack_channel_not_found", "no such channel")
        return {"status": "unknown", "detail": "lost"}

    monkeypatch.setattr(rpc_service.host_mcp_handler, "connector", connector)
    monkeypatch.setattr(rpc_service.host_mcp_handler, "slack_send", slack_send)
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: NS(credentials=wallet())
    try:
        async with TestClient(TestServer(app)) as http:
            resp = await http.post("/v1/rpc/a/slack", json={"op": "search", "query": "q", "limit": 3})
            assert resp.status == 200 and await resp.json() == {"count": 0}
            resp = await http.post("/v1/rpc/a/slack", json={"op": "read", "channel": "C1"})
            assert resp.status == 400 and (await resp.json())["code"] == "no_answer"
            assert "safe to try again" in (await resp.json())["error"]
            resp = await http.post("/v1/rpc/a/slack", json={"op": "list_channels"})
            assert resp.status == 400 and (await resp.json())["code"] == "bad_request"
            resp = await http.post("/v1/rpc/a/slack-send", json={"channel": "C1", "text": "t", "thread_ts": "1"})
            assert resp.status == 200 and (await resp.json())["status"] == "unknown"
            resp = await http.post("/v1/rpc/a/slack-send", json={"channel": "nope", "text": "t"})
            assert resp.status == 400 and (await resp.json())["code"] == "slack_channel_not_found"
            for path in ("slack-send", "slack"):
                resp = await http.post(f"/v1/rpc/a/{path}", data=b"not json")
                assert resp.status == 400 and "JSON" in (await resp.json())["error"]
    finally:
        rpc_service._RPC_RESOLVER = previous
    assert seen == [
        ("slack", "search", {"query": "q", "limit": 3}),
        ("slack", "read", {"channel": "C1"}),
        ("slack", "list_channels", {}),
        ("send", {"channel": "C1", "text": "t", "thread_ts": "1", "from_account": ""}),
        ("send", {"channel": "nope", "text": "t", "thread_ts": "", "from_account": ""}),
    ]


@pytest.mark.asyncio
async def test_the_handler_dispatches_each_operation_to_its_function(monkeypatch):
    from puffo_agent.portal import host_mcp_handler

    calls = []

    async def fake(credentials, **kw):
        calls.append(kw)
        return {"ok": 1}

    for name in ("search", "read", "list_channels"):
        monkeypatch.setattr(slack, name, fake)
    monkeypatch.setattr(slack, "send", fake)
    ctx = NS(credentials="creds")
    assert await host_mcp_handler.connector(ctx, provider="slack", op="read", channel="C1") == {"ok": 1}
    assert await host_mcp_handler.slack_send(ctx, channel="C1", text="t") == {"ok": 1}
    assert calls == [{"channel": "C1"}, {"channel": "C1", "text": "t", "thread_ts": "", "from_account": ""}]


@pytest.mark.asyncio
@pytest.mark.parametrize("behaviour, expected", [
    ("hang-up", "unknown"), ("500", "unknown"), ("sent", "sent"), ("400", "error"),
])
async def test_the_rpc_hop_keeps_unknown_distinct_from_failure(unused_tcp_port, behaviour, expected):
    seen = {}

    async def route(request):
        seen.update(await request.json())
        if behaviour == "hang-up":
            request.transport.close()
            return web.Response()
        if behaviour == "500":
            return web.json_response({"error": "handler raised"}, status=500)
        if behaviour == "400":
            return web.json_response({"error": "Slack refused", "code": "slack_x"}, status=400)
        return web.json_response({"status": "sent", "channel": "C1", "ts": "1"})

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/slack-send", route)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    client = PuffoRpcClient(f"http://127.0.0.1:{unused_tcp_port}", "agent-1")
    try:
        if expected == "error":
            with pytest.raises(RuntimeError, match="not sent"):
                await client.slack_send(channel="C1", text="t", thread_ts="1.0")
        else:
            out = await client.slack_send(channel="C1", text="t", from_account="acme-hq")
            assert out["status"] == expected
            if expected == "unknown":
                assert "may have been posted" in out["detail"]
    finally:
        await client.close()
        await runner.cleanup()
    # Only the fields that were given travel; nothing is padded in.
    assert seen == ({"channel": "C1", "text": "t", "thread_ts": "1.0"} if expected == "error"
                    else {"channel": "C1", "text": "t", "from_account": "acme-hq"})


@pytest.mark.asyncio
async def test_the_rpc_client_posts_the_op_to_the_provider_route(unused_tcp_port):
    seen = {}

    async def route(request):
        seen[request.path] = await request.json()
        return web.json_response({"count": 1})

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/{provider}", route)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    client = PuffoRpcClient(f"http://127.0.0.1:{unused_tcp_port}", "agent-1")
    try:
        assert await client.connector("slack", "read", channel="C1", limit=5) == {"count": 1}
    finally:
        await client.close()
        await runner.cleanup()
    assert seen == {"/v1/rpc/agent-1/slack": {"op": "read", "channel": "C1", "limit": 5}}


@pytest.mark.asyncio
async def test_the_mcp_tools_register_only_with_a_link_and_forward_verbatim():
    from puffo_agent.mcp.core_connector_tools import register_connector_tools

    registered = {}

    class FakeMcp:
        def tool(self):
            def decorate(fn):
                registered[fn.__name__] = fn
                return fn
            return decorate

    register_connector_tools(FakeMcp(), NS(rpc_client=None))
    assert registered == {}
    calls = []

    async def slack_send(**kw):
        calls.append(("send", kw))
        return {"status": "sent"}

    async def connector(provider, op, **kw):
        calls.append((provider, op, kw))
        return {"count": 0}

    register_connector_tools(FakeMcp(), NS(rpc_client=NS(slack_send=slack_send, connector=connector)))
    assert set(registered) == {"slack_send", "slack_search", "slack_read", "slack_list_channels",
                               "discord_whoami", "discord_guilds", "discord_guild_member",
                               "discord_connections"}
    await registered["slack_send"](channel="C1", text="t", thread_ts="1", from_account="a")
    await registered["slack_search"](query="q")
    await registered["slack_read"](channel="C1", thread_ts="2")
    await registered["slack_list_channels"](types="im")
    assert calls == [
        ("send", {"channel": "C1", "text": "t", "thread_ts": "1", "from_account": "a"}),
        ("slack", "search", {"query": "q", "limit": 10, "from_account": ""}),
        ("slack", "read", {"channel": "C1", "limit": 20, "thread_ts": "2", "from_account": ""}),
        ("slack", "list_channels", {"types": "im", "limit": 100, "from_account": ""}),
    ]
