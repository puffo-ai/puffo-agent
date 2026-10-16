"""Notion against a fake Notion: projection both ways, refresh, 403 ⊥ 404, no-leak."""

import json
import logging
from types import SimpleNamespace as NS

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from puffo_agent.portal import notion
from puffo_agent.portal.oauth_connector import ConnectorError, ConnectorLostAnswer
from tests.test_oauth_connector import FRESH, TOKEN, Routed, Scripted, Wallet

API = "https://notion.test/v1"
VERSION = "2022-06-28"

PAGE_HIT = {"object": "page", "id": "pg1", "last_edited_time": "2026-02-01T00:00:00Z",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Roadmap"}]}}}
DB_HIT = {"object": "database", "id": "db1", "last_edited_time": "2026-02-02T00:00:00Z",
          "title": [{"plain_text": "Tasks"}]}


def wallet(**kw):
    return Wallet(notion.CONNECTOR, accounts=kw.pop("accounts", ((0, "acme-ws"),)), **kw)


def _clean(payload, *secrets):
    blob = json.dumps(payload, default=str)
    return not any(s in blob for s in secrets)


def test_the_types_are_the_ones_puffo_server_provisions():
    """Literal server names (``NotionProvider::NAME`` = "notion")."""
    assert (notion.CONNECTOR.rt_type, notion.CONNECTOR.at_type) == (
        "PUFFO_NOTION_OAUTH_v1", "PUFFO_NOTION_OAUTH_AT_v1")
    assert notion.HEADERS["notion-version"] == VERSION


@pytest.mark.asyncio
async def test_search_shapes_rows_pins_the_version_and_clamps_the_limit():
    api = Routed(API, {"/search": (200, {"results": [PAGE_HIT, DB_HIT, "junk"],
                                         "next_cursor": "cur2", "has_more": True})})
    out = await notion.search(wallet(), query=" plan ", limit=999, page_token="cur1",
                              transport=api, api=API)
    assert out == {
        "results": [
            {"id": "pg1", "object": "page", "title": "Roadmap", "last_edited": "2026-02-01T00:00:00Z"},
            {"id": "db1", "object": "database", "title": "Tasks", "last_edited": "2026-02-02T00:00:00Z"},
        ],
        "count": 2, "next_page_token": "cur2", "has_more": True,
    }
    (method, url, headers, body), = api.requests
    assert (method, url) == ("POST", f"{API}/search")
    assert headers["authorization"] == f"Bearer {TOKEN}" and headers["notion-version"] == VERSION
    assert body == {"page_size": 100, "query": "plan", "start_cursor": "cur1"}


@pytest.mark.asyncio
async def test_search_reverse_absent_title_is_empty_not_fabricated():
    api = Routed(API, {"/search": (200, {"results": [
        {"object": "page", "id": "p0"},
        {"object": "page", "id": "p1",
         "properties": {"Status": {"type": "select", "select": {"name": "x"}}}},
    ]})})
    out = await notion.search(wallet(), transport=api, api=API)
    assert [r["title"] for r in out["results"]] == ["", ""]
    assert out["next_page_token"] == "" and out["has_more"] is False
    assert api.requests[0][3] == {"page_size": 10}


@pytest.mark.asyncio
async def test_read_page_merges_page_and_blocks_and_summarizes_properties():
    page = {"id": "pg1", "url": "https://notion.so/pg1", "last_edited_time": "2026-03-01",
            "archived": False,
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Spec"}]},
                "Status": {"type": "select", "select": {"name": "Open"}},
                "Size": {"type": "number", "number": 3},
                "Tags": {"type": "multi_select", "multi_select": [{"name": "a"}, {"name": "b"}]},
                "Due": {"type": "date", "date": {"start": "2026-03-10"}},
                "Done": {"type": "checkbox", "checkbox": True},
            }}
    blocks = {"results": [
        {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Title"}]}},
        {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Body text"}]}},
        {"type": "divider", "divider": {}},
    ], "has_more": True}
    api = Routed(API, {"/pages/pg1": (200, page), "/blocks/pg1/children": (200, blocks)})
    out = await notion.read_page(wallet(), page_id=" pg1 ", transport=api, api=API)
    assert out["page_id"] == "pg1" and out["title"] == "Spec"
    assert out["url"] == "https://notion.so/pg1" and out["last_edited"] == "2026-03-01"
    assert out["archived"] is False
    assert out["content"] == "Title\nBody text" and out["content_truncated"] is False
    assert out["has_more_blocks"] is True
    assert out["properties"] == {
        "Name": {"type": "title", "value": "Spec"},
        "Status": {"type": "select", "value": "Open"},
        "Size": {"type": "number", "value": 3},
        "Tags": {"type": "multi_select", "value": ["a", "b"]},
        "Due": {"type": "date", "value": "2026-03-10"},
        "Done": {"type": "checkbox", "value": True},
    }
    assert [r[0] for r in api.requests] == ["GET", "GET"]
    assert api.requests[1][1] == f"{API}/blocks/pg1/children?page_size=100"


@pytest.mark.asyncio
async def test_read_page_truncates_long_content_with_a_flag():
    big = {"results": [{"type": "paragraph",
                        "paragraph": {"rich_text": [{"plain_text": "x" * (notion.MAX_TEXT_CHARS + 50)}]}}]}
    api = Routed(API, {"/pages/pg1": (200, {"id": "pg1"}), "/blocks/pg1/children": (200, big)})
    out = await notion.read_page(wallet(), page_id="pg1", transport=api, api=API)
    assert out["content_truncated"] is True and len(out["content"]) == notion.MAX_TEXT_CHARS


@pytest.mark.asyncio
async def test_read_page_reverse_absent_fields_do_not_fabricate():
    api = Routed(API, {"/pages/pg1": (200, {"id": "pg1"}),
                       "/blocks/pg1/children": (200, {"results": [
                           {"type": "image", "image": {}},
                           {"type": "paragraph", "paragraph": {}},
                       ]})})
    out = await notion.read_page(wallet(), page_id="pg1", transport=api, api=API)
    assert out["title"] == "" and out["url"] == "" and out["last_edited"] == ""
    assert out["content"] == "" and out["content_truncated"] is False
    assert out["properties"] == {} and out["has_more_blocks"] is False


@pytest.mark.asyncio
async def test_read_page_needs_a_page_id():
    api = Scripted()
    with pytest.raises(ConnectorError) as exc:
        await notion.read_page(wallet(), page_id="  ", transport=api, api=API)
    assert exc.value.code == "bad_request" and api.requests == []


@pytest.mark.asyncio
async def test_a_bad_token_is_refreshed_once_then_named_as_the_reason():
    api, w = Scripted((401, {"code": "unauthorized"}), (200, {"results": []})), wallet()
    assert (await notion.search(w, transport=api, api=API))["count"] == 0
    assert [h["authorization"] for _, _, h, _ in api.requests] == [f"Bearer {TOKEN}", f"Bearer {FRESH}"]
    assert w.refreshes == 1 and len(api.requests) == 2
    with pytest.raises(ConnectorError) as exc:
        await notion.search(wallet(), transport=Scripted((401, {}), (401, {})), api=API)
    assert exc.value.code == "not_authorized" and "reconnect" in str(exc.value)


@pytest.mark.asyncio
async def test_a_403_is_insufficient_capability_and_never_refreshes():
    """§5.8: 403 keeps the same scope on refresh, so it must not trigger one."""
    for answer in ((403, {"code": "restricted_resource", "message": "no comments cap"}),
                   (403, {"message": "forbidden"})):
        w, api = wallet(), Scripted(answer)
        with pytest.raises(ConnectorError) as exc:
            await notion.search(w, transport=api, api=API)
        assert exc.value.code == "insufficient_capability"
        assert w.refreshes == 0 and len(api.requests) == 1


@pytest.mark.asyncio
async def test_a_404_is_not_shared_or_missing_and_never_refreshes():
    """Kept a separate case from 403 on purpose (one test, one kill)."""
    for answer in ((404, {"code": "object_not_found", "message": "missing"}),
                   (404, {"message": "nope"})):
        w, api = wallet(), Scripted(answer)
        with pytest.raises(ConnectorError) as exc:
            await notion.search(w, transport=api, api=API)
        assert exc.value.code == "not_shared_or_missing"
        assert w.refreshes == 0 and len(api.requests) == 1


@pytest.mark.asyncio
async def test_other_errors_carry_the_code_and_a_bounded_message():
    with pytest.raises(ConnectorError) as exc:
        await notion.search(wallet(),
                            transport=Scripted((400, {"code": "validation_error", "message": "bad filter"})),
                            api=API)
    assert exc.value.code == "notion_validation_error" and "bad filter" in str(exc.value)
    with pytest.raises(ConnectorError) as exc:
        await notion.read_page(wallet(), page_id="p",
                               transport=Routed(API, {"/pages/p": (200, b"<html>")}), api=API)
    assert exc.value.code == "bad_reply"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [(429, {}), (502, {}), aiohttp.ServerDisconnectedError()],
                         ids=["429", "5xx", "disconnect"])
async def test_a_lost_answer_to_a_read_is_retryable_not_a_third_outcome(answer):
    with pytest.raises(ConnectorLostAnswer):
        await notion.search(wallet(), transport=Scripted(answer), api=API)


@pytest.mark.asyncio
async def test_create_page_builds_a_page_parent_title_and_paragraphs():
    api = Scripted((200, {"id": "new1", "url": "https://notion.so/new1"}))
    out = await notion.create_page(wallet(), parent_id=" par1 ", title=" Plan ",
                                   content="first line\n\nsecond line", transport=api, api=API)
    assert out == {"page_id": "new1", "url": "https://notion.so/new1", "content_truncated": False}
    (method, url, headers, body), = api.requests
    assert (method, url) == ("POST", f"{API}/pages") and headers["notion-version"] == VERSION
    assert body["parent"] == {"page_id": "par1"}
    assert body["properties"] == {"title": {"title": [{"type": "text", "text": {"content": "Plan"}}]}}
    assert [b["paragraph"]["rich_text"][0]["text"]["content"]
            for b in body["children"]] == ["first line", "second line"]


@pytest.mark.asyncio
async def test_create_page_without_content_sends_no_children_and_needs_parent_and_title():
    api = Scripted((200, {"id": "n", "url": "u"}))
    await notion.create_page(wallet(), parent_id="p", title="T", transport=api, api=API)
    assert "children" not in api.requests[0][3]
    bad = Scripted()
    for kw in ({"parent_id": " ", "title": "T"}, {"parent_id": "p", "title": " "}):
        with pytest.raises(ConnectorError) as exc:
            await notion.create_page(wallet(), transport=bad, api=API, **kw)
        assert exc.value.code == "bad_request"
    assert bad.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, kind", [
    (aiohttp.ServerDisconnectedError(), "unknown"), ((503, {}), "unknown"),
    ((429, {}), "error"),
    # a 2xx the write surely happened for, but an unreadable body: `unknown`,
    # NOT error — error would mean "not created" and the agent would retry.
    ((200, b"<html>"), "unknown")],
    ids=["disconnect", "5xx", "429", "2xx-unreadable"])
async def test_create_page_lost_answer_is_unknown_and_never_reposted(answer, kind):
    api = Scripted(answer)
    if kind == "error":
        with pytest.raises(ConnectorError):
            await notion.create_page(wallet(), parent_id="p", title="T", transport=api, api=API)
    else:
        out = await notion.create_page(wallet(), parent_id="p", title="T", transport=api, api=API)
        assert out["status"] == "unknown" and "may have been applied" in out["detail"]
    assert len(api.requests) == 1


@pytest.mark.asyncio
async def test_update_page_sets_title_then_appends_in_order():
    api = Routed(API, {"/pages/pg1": (200, {"id": "pg1"}),
                       "/blocks/pg1/children": (200, {"results": []})})
    out = await notion.update_page(wallet(), page_id="pg1", title="New", append_content="more",
                                   transport=api, api=API)
    assert out == {"ok": True, "page_id": "pg1", "content_truncated": False}
    assert [(r[0], r[1]) for r in api.requests] == [
        ("PATCH", f"{API}/pages/pg1"), ("PATCH", f"{API}/blocks/pg1/children")]
    assert api.requests[0][3] == {
        "properties": {"title": {"title": [{"type": "text", "text": {"content": "New"}}]}}}
    assert [b["paragraph"]["rich_text"][0]["text"]["content"]
            for b in api.requests[1][3]["children"]] == ["more"]


@pytest.mark.asyncio
async def test_update_page_title_only_or_append_only_and_needs_something():
    api = Routed(API, {"/pages/pg1": (200, {"id": "pg1"})})
    await notion.update_page(wallet(), page_id="pg1", title="T", transport=api, api=API)
    assert len(api.requests) == 1 and api.requests[0][1] == f"{API}/pages/pg1"
    api2 = Routed(API, {"/blocks/pg1/children": (200, {})})
    await notion.update_page(wallet(), page_id="pg1", append_content="x", transport=api2, api=API)
    assert len(api2.requests) == 1 and api2.requests[0][1] == f"{API}/blocks/pg1/children"
    bad = Scripted()
    with pytest.raises(ConnectorError) as exc:
        await notion.update_page(wallet(), page_id="pg1", transport=bad, api=API)
    assert exc.value.code == "bad_request" and bad.requests == []


@pytest.mark.asyncio
async def test_update_page_lost_answer_on_append_is_unknown():
    api = Routed(API, {"/pages/pg1": (200, {}),
                       "/blocks/pg1/children": aiohttp.ServerDisconnectedError()})
    out = await notion.update_page(wallet(), page_id="pg1", title="T", append_content="x",
                                   transport=api, api=API)
    assert out["status"] == "unknown"


@pytest.mark.asyncio
async def test_a_body_over_the_block_ceiling_is_cut_and_flagged_not_dropped_silently():
    """Write honesty isomorphic to read_page: a body past Notion's one-request
    block ceiling is truncated and SAID SO, never silently swallowed."""
    over = "\n".join(f"line {i}" for i in range(notion.MAX_CHILD_BLOCKS + 25))
    under = "\n".join(f"line {i}" for i in range(notion.MAX_CHILD_BLOCKS))

    api = Scripted((200, {"id": "n1", "url": "u1"}))
    out = await notion.create_page(wallet(), parent_id="p", title="T", content=over,
                                   transport=api, api=API)
    assert out["content_truncated"] is True
    assert len(api.requests[0][3]["children"]) == notion.MAX_CHILD_BLOCKS

    at_limit = Scripted((200, {"id": "n2", "url": "u2"}))
    out = await notion.create_page(wallet(), parent_id="p", title="T", content=under,
                                   transport=at_limit, api=API)
    assert out["content_truncated"] is False
    assert len(at_limit.requests[0][3]["children"]) == notion.MAX_CHILD_BLOCKS

    appended = Routed(API, {"/blocks/pg1/children": (200, {"results": []})})
    out = await notion.update_page(wallet(), page_id="pg1", append_content=over,
                                   transport=appended, api=API)
    assert out["content_truncated"] is True
    assert len(appended.requests[0][3]["children"]) == notion.MAX_CHILD_BLOCKS


@pytest.mark.asyncio
async def test_multi_account_held_empty_one_many_and_from_account_hit_miss():
    ok = (200, {"results": []})
    with pytest.raises(ConnectorError) as exc:
        await notion.search(wallet(accounts=[]), transport=Scripted(ok), api=API)
    assert exc.value.code == "no_account"
    assert (await notion.search(wallet(accounts=[(0, "a")]), transport=Scripted(ok), api=API))["count"] == 0
    with pytest.raises(ConnectorError) as exc:
        await notion.search(wallet(accounts=[(0, "a"), (1, "b")]), transport=Scripted(ok), api=API)
    assert exc.value.code == "ambiguous_account"
    assert (await notion.search(wallet(accounts=[(0, "a"), (1, "b")]), from_account="b",
                                transport=Scripted(ok), api=API))["count"] == 0
    with pytest.raises(ConnectorError) as exc:
        await notion.search(wallet(accounts=[(0, "a")]), from_account="c", transport=Scripted(ok), api=API)
    assert exc.value.code == "unknown_account"


@pytest.mark.asyncio
async def test_the_token_never_reaches_the_projection_log_or_error(caplog):
    """§5.7 scrub sentinel with teeth. The token is planted into NON-projected
    response-body fields: a projection that echoed the raw body (rather than
    selecting fields) would leak it, so the forward assert is not vacuous. Plus
    the shared-call scrub (logs, error text) and a positive control that bites."""
    caplog.set_level(logging.DEBUG)
    bugged = {**PAGE_HIT, "secret_echo": TOKEN, "properties": {
        **PAGE_HIT["properties"],
        "Token": {"type": "rich_text", "rich_text": [{"plain_text": FRESH}]}}}
    out = await notion.search(wallet(), transport=Routed(API, {"/search": (200, {"results": [bugged]})}),
                              api=API)
    with pytest.raises(ConnectorError) as exc:
        await notion.search(wallet(),
                            transport=Scripted((404, {"code": "object_not_found", "message": "x"})), api=API)
    for secret in (TOKEN, FRESH):
        assert _clean(out, secret)
        assert secret not in str(exc.value)
        assert secret not in caplog.text
    # positive control: the sentinel is not vacuous — a leak would be caught.
    assert not _clean({"leaked": TOKEN}, TOKEN)


@pytest.mark.asyncio
async def test_the_route_validates_each_op_and_reaches_the_handler(monkeypatch):
    from puffo_agent.portal import rpc_service

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/notion", rpc_service.connector_route("notion"))
    seen = []

    async def connector(ctx, *, provider, op, **fields):
        seen.append((provider, op, fields))
        return {"ok": 1}

    monkeypatch.setattr(rpc_service.host_mcp_handler, "connector", connector)
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: NS(credentials=wallet())
    try:
        async with TestClient(TestServer(app)) as http:
            assert (await http.post("/v1/rpc/a/notion", json={"op": "read_page"})).status == 400
            assert (await http.post("/v1/rpc/a/notion",
                                    json={"op": "create_page", "parent_id": "p"})).status == 400
            assert (await http.post("/v1/rpc/a/notion",
                                    json={"op": "search", "limit": "lots"})).status == 400
            assert (await http.post("/v1/rpc/a/notion", json={"op": "nope"})).status == 400
            resp = await http.post("/v1/rpc/a/notion",
                                   json={"op": "read_page", "page_id": "pg1", "from_account": "a"})
            assert resp.status == 200 and await resp.json() == {"ok": 1}
    finally:
        rpc_service._RPC_RESOLVER = previous
    assert seen == [("notion", "read_page", {"page_id": "pg1", "from_account": "a"})]


@pytest.mark.asyncio
async def test_the_handler_and_the_tools_dispatch_every_notion_operation(monkeypatch):
    from puffo_agent.mcp.core_connector_tools import register_connector_tools
    from puffo_agent.portal import host_mcp_handler

    calls = []

    async def fake(credentials, **kw):
        calls.append(kw)
        return {"ok": 1}

    for name in ("search", "read_page", "create_page", "update_page"):
        monkeypatch.setattr(notion, name, fake)
    for op, kw in (("search", {}), ("read_page", {"page_id": "p"}),
                   ("create_page", {"parent_id": "p", "title": "t"}), ("update_page", {"page_id": "p"})):
        assert await host_mcp_handler.connector(NS(credentials=None), provider="notion", op=op, **kw) == {"ok": 1}
    assert calls == [{}, {"page_id": "p"}, {"parent_id": "p", "title": "t"}, {"page_id": "p"}]

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
    assert {"notion_search", "notion_read_page", "notion_create_page", "notion_update_page"} <= set(registered)
    await registered["notion_search"](query="q")
    await registered["notion_read_page"](page_id="p")
    await registered["notion_create_page"](parent_id="par", title="t", content="c")
    await registered["notion_update_page"](page_id="p", append_content="x")
    assert forwarded == [
        ("notion", "search", {"query": "q", "limit": 10, "page_token": "", "from_account": ""}),
        ("notion", "read_page", {"page_id": "p", "from_account": ""}),
        ("notion", "create_page", {"parent_id": "par", "title": "t", "content": "c", "from_account": ""}),
        ("notion", "update_page", {"page_id": "p", "title": "", "append_content": "x", "from_account": ""}),
    ]
