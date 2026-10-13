"""Reading and filing mail against a fake Gmail.

The cells that matter: a lost answer is retryable here and not an "unknown",
nothing can delete mail permanently, and bodies and addresses stay out of
the log.
"""

import base64
import json
import logging
from email.message import EmailMessage

import aiohttp
import pytest
from aiohttp import web

from puffo_agent.portal.gmail_mailbox import (
    ACTIONS,
    GmailLostAnswer,
    create_label,
    get_attachment,
    label,
    list_drafts,
    list_labels,
    organize,
    read,
    search,
)
from puffo_agent.portal.gmail_send import GmailSendError
from tests.test_gmail_send import TOKEN, Wallet

API = "https://gmail.test/v1/users/me"


class Api:
    """Answers keyed by the path that was asked for; records every call."""

    def __init__(self, routes, fail_with=None):
        self.routes, self.calls, self.fail_with = routes, [], fail_with

    async def __call__(self, url, body, headers, *, method="POST"):
        # Works for the test base and for the real one the route uses.
        path = url.split("?", 1)[0].split("/users/me", 1)[-1]
        self.calls.append((method, url, json.loads(body) if body else None))
        if self.fail_with is not None:
            raise self.fail_with
        answer = self.routes[path]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, payload = answer
        return status, json.dumps(payload).encode()


def _raw(subject="Hi", body="the body", frm="alice@example.com", html=None):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = frm, "me@example.com", subject
    msg["Date"] = "Mon, 29 Sep 2026 10:00:00 -0700"
    if html is None:
        msg.set_content(body)
    else:
        msg.set_content(body)
        msg.add_alternative(html, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


def _meta(message_id, subject="Hi", labels=("INBOX", "UNREAD")):
    return 200, {
        "id": message_id, "threadId": f"t-{message_id}", "snippet": "a snippet",
        "labelIds": list(labels),
        "payload": {"headers": [
            {"name": "From", "value": "alice@example.com"},
            {"name": "Subject", "value": subject},
            {"name": "Date", "value": "Mon, 29 Sep 2026 10:00:00 -0700"},
        ]},
    }


@pytest.mark.asyncio
async def test_a_search_returns_a_summary_for_every_result():
    api = Api({
        "/messages": (200, {"messages": [{"id": "m1"}, {"id": "m2"}]}),
        "/messages/m1": _meta("m1", "First"),
        "/messages/m2": _meta("m2", "Second", labels=("INBOX",)),
    })
    out = await search(Wallet(), query="is:unread", limit=5, transport=api, api=API)
    assert out["count"] == 2
    assert [m["subject"] for m in out["messages"]] == ["First", "Second"]
    assert [m["unread"] for m in out["messages"]] == [True, False]
    listing = api.calls[0][1]
    assert "q=is%3Aunread" in listing and "maxResults=5" in listing
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("asked, sent", [(0, "1"), (999, "50"), (7, "7")])
async def test_the_result_limit_is_clamped_to_something_gmail_accepts(asked, sent):
    api = Api({"/messages": (200, {"messages": []})})
    await search(Wallet(), limit=asked, transport=api, api=API)
    assert f"maxResults={sent}" in api.calls[0][1]


@pytest.mark.asyncio
async def test_a_search_with_no_matches_is_an_empty_answer_not_an_error():
    api = Api({"/messages": (200, {})})
    assert await search(Wallet(), transport=api, api=API) == {
        "messages": [], "count": 0, "next_page_token": "",
    }


@pytest.mark.asyncio
async def test_reading_a_message_gives_its_headers_and_plain_text():
    api = Api({"/messages/m1": (200, {
        "id": "m1", "threadId": "t-1", "labelIds": ["INBOX"], "raw": _raw(body="hello there"),
    })})
    out = await read(Wallet(), message_id="m1", transport=api, api=API)
    assert out["subject"] == "Hi" and out["from"] == "alice@example.com"
    assert out["body"].strip() == "hello there" and out["body_truncated"] is False
    assert out["attachments"] == [] and out["labels"] == ["INBOX"]
    assert api.calls[0][0] == "GET" and "format=raw" in api.calls[0][1]


HTML = "<html><style>p{color:red}</style><body><p>Hello</p><p>World</p></body></html>"


def _html_only():
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "a@x", "b@x", "Hi"
    msg.set_content(HTML, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


@pytest.mark.asyncio
async def test_an_html_only_message_is_read_as_text_not_as_markup():
    api = Api({"/messages/m1": (200, {"id": "m1", "raw": _html_only()})})
    out = await read(Wallet(), message_id="m1", transport=api, api=API)
    assert "<p>" not in out["body"] and "color:red" not in out["body"]
    assert "Hello" in out["body"] and "World" in out["body"]


@pytest.mark.asyncio
async def test_an_empty_plain_part_falls_through_to_the_html_one():
    """A blank text/plain beside real HTML must not read as empty."""
    api = Api({"/messages/m1": (200, {"id": "m1", "raw": _raw(body="", html=HTML)})})
    out = await read(Wallet(), message_id="m1", transport=api, api=API)
    assert "Hello" in out["body"] and "World" in out["body"]


@pytest.mark.asyncio
async def test_a_very_long_body_is_cut_and_says_so():
    from puffo_agent.portal.gmail_mailbox import MAX_BODY_CHARS

    api = Api({"/messages/m1": (200, {"id": "m1", "raw": _raw(body="x" * (MAX_BODY_CHARS + 500))})})
    out = await read(Wallet(), message_id="m1", transport=api, api=API)
    assert out["body_truncated"] is True and len(out["body"]) == MAX_BODY_CHARS


@pytest.mark.asyncio
async def test_attachment_names_are_listed_without_their_contents():
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "a@x", "b@x", "with a file"
    msg.set_content("see attached")
    msg.add_attachment(b"SECRETBYTES", maintype="application", subtype="pdf",
                       filename="invoice.pdf")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    api = Api({"/messages/m1": (200, {"id": "m1", "raw": raw})})
    out = await read(Wallet(), message_id="m1", transport=api, api=API)
    assert out["attachments"] == ["invoice.pdf"]
    assert "SECRETBYTES" not in json.dumps(out)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload, code", [
    ({"id": "m1"}, "bad_reply"),                       # no raw at all
    ({"id": "m1", "raw": "!!!not base64!!!"}, "bad_reply"),
])
async def test_a_message_we_cannot_read_is_refused_not_guessed(payload, code):
    api = Api({"/messages/m1": (200, payload)})
    with pytest.raises(GmailSendError) as exc:
        await read(Wallet(), message_id="m1", transport=api, api=API)
    assert exc.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("action, add, remove", [
    ("archive", [], ["INBOX"]),
    ("move_to_inbox", ["INBOX"], []),
    ("mark_read", [], ["UNREAD"]),
    ("mark_unread", ["UNREAD"], []),
    ("star", ["STARRED"], []),
    ("unstar", [], ["STARRED"]),
])
async def test_every_label_action_sends_the_edit_gmail_expects(action, add, remove):
    api = Api({"/messages/m1/modify": (200, {"id": "m1", "labelIds": ["INBOX"]})})
    out = await organize(Wallet(), message_id="m1", action=action, transport=api, api=API)
    assert out == {"status": "done", "action": action, "message_id": "m1", "labels": ["INBOX"]}
    method, url, sent = api.calls[0]
    assert method == "POST" and url.endswith("/messages/m1/modify")
    assert sent == {"addLabelIds": add, "removeLabelIds": remove}


@pytest.mark.asyncio
@pytest.mark.parametrize("action, path", [("trash", "trash"), ("untrash", "untrash")])
async def test_trash_is_reversible_and_never_a_permanent_delete(action, path):
    api = Api({f"/messages/m1/{path}": (200, {"id": "m1", "labelIds": ["TRASH"]})})
    out = await organize(Wallet(), message_id="m1", action=action, transport=api, api=API)
    assert out["action"] == path
    method, url, _ = api.calls[0]
    assert method == "POST" and url.endswith(f"/messages/m1/{path}")
    # The DELETE verb needs mail.google.com, which is deliberately not granted.
    assert method != "DELETE"


def test_the_action_vocabulary_has_no_permanent_delete():
    assert "delete" not in ACTIONS and "untrash" in ACTIONS


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "purge", "", "ARCHIVE"])
async def test_an_action_we_do_not_offer_is_refused_before_any_request(action):
    api = Api({})
    with pytest.raises(GmailSendError) as exc:
        await organize(Wallet(), message_id="m1", action=action, transport=api, api=API)
    assert exc.value.code == "bad_request" and api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["search", "read", "organize"])
async def test_a_missing_message_id_or_account_never_reaches_gmail(call):
    api = Api({})
    kw = {"transport": api, "api": API}
    with pytest.raises(GmailSendError) as exc:
        if call == "search":
            await search(None, **kw)
        elif call == "read":
            await read(None, message_id="m1", **kw)
        else:
            await organize(None, message_id="m1", action="archive", **kw)
    assert exc.value.code == "no_credentials" and api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["read", "organize"])
async def test_a_blank_message_id_is_refused(call):
    api = Api({})
    with pytest.raises(GmailSendError) as exc:
        if call == "read":
            await read(Wallet(), message_id="   ", transport=api, api=API)
        else:
            await organize(Wallet(), message_id="  ", action="archive", transport=api, api=API)
    assert exc.value.code == "bad_request" and api.calls == []


@pytest.mark.asyncio
async def test_a_refused_token_is_refreshed_once_and_the_call_repeated():
    api = Api({"/messages/m1/modify": [(401, {}), (200, {"id": "m1", "labelIds": []})]})
    wallet = Wallet()
    await organize(wallet, message_id="m1", action="archive", transport=api, api=API)
    assert wallet.refreshes == 1
    assert [call[0] for call in api.calls] == ["POST", "POST"]


@pytest.mark.asyncio
async def test_a_token_refused_twice_is_a_definite_failure():
    api = Api({"/messages/m1/modify": [(401, {}), (401, {})]})
    with pytest.raises(GmailSendError) as exc:
        await organize(Wallet(), message_id="m1", action="archive", transport=api, api=API)
    assert exc.value.code == "gmail_401" and len(api.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    aiohttp.ServerDisconnectedError(),
    (503, {"error": {"message": "backend"}}),
])
async def test_a_lost_answer_is_retryable_here_not_a_third_outcome(failure):
    """Send cannot know; filing is idempotent, so the answer is "try again"."""
    if isinstance(failure, BaseException):
        api = Api({"/messages/m1/modify": None}, fail_with=failure)
    else:
        api = Api({"/messages/m1/modify": failure})
    with pytest.raises(GmailLostAnswer):
        await organize(Wallet(), message_id="m1", action="archive", transport=api, api=API)


@pytest.mark.asyncio
async def test_a_mailbox_we_cannot_reach_is_a_plain_failure():
    api = Api({"/messages": None},
              fail_with=aiohttp.ClientConnectorError(connection_key=None, os_error=OSError("x")))
    with pytest.raises(GmailSendError) as exc:
        await search(Wallet(), transport=api, api=API)
    assert exc.value.code == "unreachable"


@pytest.mark.asyncio
async def test_a_403_points_at_the_scope_rather_than_looking_like_a_bug():
    """A pre-widening token keeps its scope through every refresh."""
    api = Api({"/messages": (403, {"error": {"message": "Insufficient Permission"}})})
    with pytest.raises(GmailSendError) as exc:
        await search(Wallet(), transport=api, api=API)
    assert exc.value.code == "insufficient_scope" and "reconnect" in str(exc.value)


@pytest.mark.asyncio
async def test_gmails_refusal_text_reaches_the_agent():
    api = Api({"/messages/m1/modify": (400, {"error": {"message": "Invalid label"}})})
    with pytest.raises(GmailSendError) as exc:
        await organize(Wallet(), message_id="m1", action="archive", transport=api, api=API)
    assert exc.value.code == "gmail_400" and "Invalid label" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"<html>", b'"a string"'])
async def test_an_unreadable_success_body_is_refused(payload):
    class Raw(Api):
        async def __call__(self, url, body, headers, *, method="POST"):
            self.calls.append((method, url, None))
            return 200, payload

    with pytest.raises(GmailSendError) as exc:
        await search(Wallet(), transport=Raw({}), api=API)
    assert exc.value.code == "bad_reply"


@pytest.mark.asyncio
async def test_the_log_carries_ids_and_codes_not_bodies_or_addresses(caplog):
    caplog.set_level(logging.DEBUG)
    api = Api({"/messages/m1": (200, {
        "id": "m1", "raw": _raw(body="a private sentence", frm="secret@example.com"),
    })})
    await read(Wallet(), message_id="m1", transport=api, api=API)
    api2 = Api({"/messages/m1/modify": (400, {"error": {"message": "nope"}})})
    with pytest.raises(GmailSendError):
        await organize(Wallet(), message_id="m1", action="archive", transport=api2, api=API)
    text = caplog.text
    assert "m1" in text and "gmail_400" in text
    for secret in ("a private sentence", "secret@example.com", TOKEN):
        assert secret not in text


# ── the RPC route and the tools on top of it ───────────────────────


def _route_app():
    from puffo_agent.portal import rpc_service

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/gmail-mailbox", rpc_service.gmail_mailbox_route)
    return app, rpc_service


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    "not json",
    {"op": "delete_everything", "message_id": "m1"},
    {"message_id": "m1"},                               # no op
    {"op": "read"},                                     # missing message_id
    {"op": "read", "message_id": "m1", "extra": "no"},  # unknown field
    {"op": "read", "message_id": 7},                    # wrong type
    {"op": "search", "limit": "ten"},                   # wrong type
    {"op": "search", "limit": True},                    # bool is not an int here
])
async def test_the_route_refuses_anything_it_does_not_recognise(payload):
    from aiohttp.test_utils import TestClient, TestServer

    app, _ = _route_app()
    async with TestClient(TestServer(app)) as http:
        kw = {"data": payload} if isinstance(payload, str) else {"json": payload}
        resp = await http.post("/v1/rpc/agent-1/gmail-mailbox", **kw)
        assert resp.status == 400


@pytest.mark.asyncio
async def test_the_route_reports_no_worker_rather_than_failing_obscurely():
    from aiohttp.test_utils import TestClient, TestServer

    app, rpc_service = _route_app()
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: None
    try:
        async with TestClient(TestServer(app)) as http:
            resp = await http.post("/v1/rpc/agent-1/gmail-mailbox",
                                   json={"op": "search"})
            assert resp.status == 409 and (await resp.json())["code"] == "no_worker"
    finally:
        rpc_service._RPC_RESOLVER = previous


async def _through_route(op, fields, api, monkeypatch):
    from types import SimpleNamespace as NS

    from aiohttp.test_utils import TestClient, TestServer

    from puffo_agent.portal import gmail_mailbox as mailbox

    monkeypatch.setattr(mailbox, "gmail_transport", api)
    app, rpc_service = _route_app()
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: NS(credentials=Wallet())
    try:
        async with TestClient(TestServer(app)) as http:
            resp = await http.post("/v1/rpc/agent-1/gmail-mailbox",
                                   json={"op": op, **fields})
            return resp.status, await resp.json()
    finally:
        rpc_service._RPC_RESOLVER = previous


@pytest.mark.asyncio
async def test_the_route_reaches_the_worker_for_each_operation(monkeypatch):
    api = Api({
        "/messages": (200, {"messages": [{"id": "m1"}]}),
        "/messages/m1": _meta("m1", "Hello"),
    })
    status, payload = await _through_route("search", {"query": "is:unread"}, api, monkeypatch)
    assert status == 200 and payload["messages"][0]["subject"] == "Hello"

    api = Api({"/messages/m1": (200, {"id": "m1", "raw": _raw(body="the text")})})
    status, payload = await _through_route("read", {"message_id": "m1"}, api, monkeypatch)
    assert status == 200 and payload["body"].strip() == "the text"

    api = Api({"/messages/m1/modify": (200, {"id": "m1", "labelIds": []})})
    status, payload = await _through_route(
        "organize", {"message_id": "m1", "action": "archive"}, api, monkeypatch)
    assert status == 200 and payload["action"] == "archive"


@pytest.mark.asyncio
async def test_a_lost_answer_reaches_the_agent_as_retryable_not_as_unknown(monkeypatch):
    """Not "may have been sent": nothing changed twice, so try again."""
    api = Api({"/messages/m1/modify": (503, {})})
    status, payload = await _through_route(
        "organize", {"message_id": "m1", "action": "archive"}, api, monkeypatch)
    assert status == 400 and payload["code"] == "no_answer"
    assert "safe to try again" in payload["error"]


@pytest.mark.asyncio
async def test_a_gmail_refusal_keeps_its_code_through_the_route(monkeypatch):
    api = Api({"/messages/m1/modify": (403, {"error": {"message": "Insufficient Permission"}})})
    status, payload = await _through_route(
        "organize", {"message_id": "m1", "action": "archive"}, api, monkeypatch)
    assert status == 400 and payload["code"] == "insufficient_scope"


@pytest.mark.asyncio
async def test_the_mailbox_tools_forward_to_the_daemon():
    from types import SimpleNamespace as NS

    from puffo_agent.mcp.core_gmail_tools import register_gmail_tools

    registered, calls = {}, []

    class FakeMcp:
        def tool(self):
            def decorate(fn):
                registered[fn.__name__] = fn
                return fn
            return decorate

    async def gmail_mailbox(op, **fields):
        calls.append((op, fields))
        return {"ok": op}

    register_gmail_tools(FakeMcp(), NS(rpc_client=NS(gmail_mailbox=gmail_mailbox,
                                                     gmail_send=None)))
    assert await registered["gmail_search"](query="is:unread", limit=3) == {"ok": "search"}
    assert await registered["gmail_read"](message_id="m1") == {"ok": "read"}
    assert await registered["gmail_organize"](message_id="m1", action="trash") == {"ok": "organize"}
    assert await registered["gmail_list_labels"]() == {"ok": "list_labels"}
    assert await registered["gmail_create_label"](name="DMARC") == {"ok": "create_label"}
    assert await registered["gmail_label"](message_ids=["m1"], add=["DMARC"]) == {"ok": "label"}
    assert await registered["gmail_list_drafts"]() == {"ok": "list_drafts"}
    assert await registered["gmail_get_attachment"](
        message_id="m1", filename="r.xml") == {"ok": "get_attachment"}
    assert calls == [
        ("search", {"query": "is:unread", "limit": 3, "page_token": "", "from_account": ""}),
        ("read", {"message_id": "m1", "from_account": ""}),
        ("organize", {"message_id": "m1", "action": "trash", "from_account": ""}),
        ("list_labels", {"from_account": ""}),
        ("create_label", {"name": "DMARC", "from_account": ""}),
        ("label", {"message_ids": ["m1"], "add": ["DMARC"], "remove": [], "from_account": ""}),
        ("list_drafts", {"limit": 10, "page_token": "", "from_account": ""}),
        ("get_attachment", {"message_id": "m1", "filename": "r.xml", "from_account": ""}),
    ]


@pytest.mark.asyncio
async def test_the_rpc_client_posts_the_op_and_returns_the_object(unused_tcp_port):
    from puffo_agent.mcp._host_mcp import PuffoRpcClient

    seen = {}

    async def route(request):
        seen.update(await request.json())
        return web.json_response({"count": 0, "messages": []})

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/gmail-mailbox", route)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    client = PuffoRpcClient(f"http://127.0.0.1:{unused_tcp_port}", "agent-1")
    try:
        out = await client.gmail_mailbox("search", query="x", limit=2, from_account="")
        assert out == {"count": 0, "messages": []}
        assert seen == {"op": "search", "query": "x", "limit": 2, "from_account": ""}
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_a_message_with_no_text_part_reads_as_empty_not_as_an_error():
    """An attachment-only message still has headers worth returning."""
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "a@x", "b@x", "just a file"
    msg.set_content(b"BYTES", maintype="application", subtype="octet-stream")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    api = Api({"/messages/m1": (200, {"id": "m1", "raw": raw})})
    out = await read(Wallet(), message_id="m1", transport=api, api=API)
    assert out["body"] == "" and out["body_truncated"] is False
    assert out["subject"] == "just a file"


@pytest.mark.parametrize("part", [
    type("Undecodable", (), {
        "get_content": lambda self: (_ for _ in ()).throw(LookupError("unknown charset")),
        "get_content_subtype": lambda self: "plain",
    })(),
    type("NotText", (), {
        "get_content": lambda self: b"bytes, not str",
        "get_content_subtype": lambda self: "plain",
    })(),
])
def test_a_part_we_cannot_turn_into_text_yields_nothing_rather_than_raising(part):
    from puffo_agent.portal.gmail_mailbox import _part_text

    assert _part_text(part) == ""


# ── paging ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_pages_with_the_token_gmail_returned():
    api = Api({"/messages": (200, {"messages": [], "nextPageToken": "p2"})})
    first = await search(Wallet(), query="from:x", transport=api, api=API)
    assert first["next_page_token"] == "p2"
    await search(Wallet(), query="from:x", page_token="p2", transport=api, api=API)
    assert "pageToken=p2" in api.calls[1][1] and "pageToken" not in api.calls[0][1]


# ── labels ─────────────────────────────────────────────────────────

LABELS = (200, {"labels": [
    {"id": "INBOX", "name": "INBOX", "type": "system"},
    {"id": "Label_7", "name": "DMARC", "type": "user"},
]})


@pytest.mark.asyncio
async def test_create_label_returns_the_existing_one_rather_than_failing_a_retry():
    api = Api({"/labels": [(409, {"error": {"message": "exists"}}), LABELS]})
    out = await create_label(Wallet(), name="dmarc", transport=api, api=API)
    assert out == {"status": "exists", "id": "Label_7", "name": "DMARC", "type": "user"}


@pytest.mark.asyncio
async def test_create_label_still_fails_a_conflict_it_cannot_explain():
    api = Api({"/labels": [(409, {}), LABELS]})
    with pytest.raises(GmailSendError) as exc:
        await create_label(Wallet(), name="Elsewhere", transport=api, api=API)
    assert exc.value.code == "gmail_409"


@pytest.mark.asyncio
async def test_create_label_posts_a_visible_label():
    api = Api({"/labels": (200, {"id": "Label_9", "name": "Reports", "type": "user"})})
    out = await create_label(Wallet(), name=" Reports ", transport=api, api=API)
    assert out["status"] == "created" and out["id"] == "Label_9"
    assert api.calls[0][2]["name"] == "Reports"


@pytest.mark.asyncio
async def test_label_resolves_names_and_ids_into_one_batch_modify():
    api = Api({"/labels": LABELS, "/messages/batchModify": (204, {})})
    out = await label(Wallet(), message_ids=["m1", "m2"], add=["dmarc"], remove=["INBOX"],
                      transport=api, api=API)
    method, url, body = api.calls[-1]
    assert (method, url.endswith("/messages/batchModify")) == ("POST", True)
    assert body == {"ids": ["m1", "m2"], "addLabelIds": ["Label_7"], "removeLabelIds": ["INBOX"]}
    assert out == {"status": "done", "count": 2, "added": ["Label_7"], "removed": ["INBOX"]}


@pytest.mark.asyncio
async def test_an_unknown_label_is_refused_before_anything_is_changed():
    api = Api({"/labels": LABELS})
    with pytest.raises(GmailSendError) as exc:
        await label(Wallet(), message_ids=["m1"], add=["Typo"], transport=api, api=API)
    assert exc.value.code == "unknown_label" and "gmail_create_label" in str(exc.value)
    assert all("batchModify" not in url for _, url, _ in api.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [
    {"message_ids": []},
    {"message_ids": ["m"] * 1001},
    {"message_ids": ["m1"], "add": [], "remove": []},
    {"message_ids": "m1"},
    {"message_ids": ["m1"], "add": [3]},
])
async def test_label_refuses_a_malformed_request_without_calling_gmail(kw):
    api = Api({})
    kw = {"add": ["DMARC"], **kw}
    with pytest.raises(GmailSendError) as exc:
        await label(Wallet(), transport=api, api=API, **kw)
    assert exc.value.code == "bad_request" and api.calls == []


@pytest.mark.asyncio
async def test_list_labels_reports_ids_names_and_kind():
    out = await list_labels(Wallet(), transport=Api({"/labels": LABELS}), api=API)
    assert out["count"] == 2 and out["labels"][1] == {"id": "Label_7", "name": "DMARC", "type": "user"}


# ── drafts ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_drafts_names_each_draft_and_pages():
    api = Api({
        "/drafts": (200, {"drafts": [{"id": "d1"}], "nextPageToken": "n"}),
        "/drafts/d1": (200, {"id": "d1", "message": {
            "id": "m1", "threadId": "t1",
            "payload": {"headers": [{"name": "Subject", "value": "Weekly"}]}}}),
    })
    out = await list_drafts(Wallet(), transport=api, api=API)
    assert out["next_page_token"] == "n"
    assert out["drafts"][0]["draft_id"] == "d1" and out["drafts"][0]["subject"] == "Weekly"


# ── attachments ────────────────────────────────────────────────────


def _with_part(filename, body):
    return (200, {"id": "m1", "payload": {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "filename": "", "body": {"size": 3}},
        {"mimeType": "application/xml", "filename": filename, "body": body},
    ]}})


@pytest.mark.asyncio
async def test_an_attachment_is_saved_inside_the_workspace(tmp_path):
    data = base64.urlsafe_b64encode(b"<feedback/>").decode().rstrip("=")
    api = Api({"/messages/m1": _with_part("report.xml", {"attachmentId": "a1", "size": 11}),
               "/messages/m1/attachments/a1": (200, {"data": data, "size": 11})})
    out = await get_attachment(Wallet(), message_id="m1", filename="report.xml",
                               workspace=tmp_path, transport=api, api=API)
    assert out == {"status": "saved", "path": "gmail-attachments/m1/report.xml", "size": 11,
                   "mime_type": "application/xml"}
    assert (tmp_path / out["path"]).read_bytes() == b"<feedback/>"


@pytest.mark.asyncio
async def test_a_hostile_attachment_name_cannot_leave_the_folder(tmp_path):
    data = base64.urlsafe_b64encode(b"x").decode()
    api = Api({"/messages/m1": _with_part("../../.bashrc", {"data": data, "size": 1})})
    out = await get_attachment(Wallet(), message_id="m1", filename="../../.bashrc",
                               workspace=tmp_path, transport=api, api=API)
    assert out["path"] == "gmail-attachments/m1/bashrc"
    assert not (tmp_path.parent / ".bashrc").exists()


@pytest.mark.asyncio
async def test_a_symlinked_attachment_folder_cannot_redirect_the_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "gmail-attachments").symlink_to(outside)
    data = base64.urlsafe_b64encode(b"x").decode()
    api = Api({"/messages/m1": _with_part("r.xml", {"data": data, "size": 1})})
    with pytest.raises(GmailSendError) as exc:
        await get_attachment(Wallet(), message_id="m1", filename="r.xml",
                             workspace=workspace, transport=api, api=API)
    assert exc.value.code == "bad_request"
    assert not any(outside.rglob("r.xml"))


@pytest.mark.asyncio
async def test_a_missing_attachment_names_what_is_attached(tmp_path):
    api = Api({"/messages/m1": _with_part("report.xml", {"attachmentId": "a1", "size": 1})})
    with pytest.raises(GmailSendError) as exc:
        await get_attachment(Wallet(), message_id="m1", filename="other.pdf",
                             workspace=tmp_path, transport=api, api=API)
    assert exc.value.code == "unknown_attachment" and "report.xml" in str(exc.value)


@pytest.mark.asyncio
async def test_an_oversized_attachment_is_refused_before_its_data_is_fetched(tmp_path):
    api = Api({"/messages/m1": _with_part("big.zip", {"attachmentId": "a1",
                                                       "size": 26 * 1024 * 1024})})
    with pytest.raises(GmailSendError) as exc:
        await get_attachment(Wallet(), message_id="m1", filename="big.zip",
                             workspace=tmp_path, transport=api, api=API)
    assert exc.value.code == "too_large" and len(api.calls) == 1


@pytest.mark.asyncio
async def test_without_a_workspace_nothing_is_fetched():
    api = Api({})
    with pytest.raises(GmailSendError) as exc:
        await get_attachment(Wallet(), message_id="m1", filename="r.xml",
                             workspace=None, transport=api, api=API)
    assert exc.value.code == "no_workspace" and api.calls == []


@pytest.mark.asyncio
async def test_create_label_needs_a_name_and_passes_other_refusals_on():
    with pytest.raises(GmailSendError) as exc:
        await create_label(Wallet(), name="  ", transport=Api({}), api=API)
    assert exc.value.code == "bad_request"
    with pytest.raises(GmailSendError) as exc:
        await create_label(Wallet(), name="X", transport=Api({"/labels": (400, {})}), api=API)
    assert exc.value.code == "gmail_400"


@pytest.mark.asyncio
async def test_list_drafts_passes_the_page_token_and_skips_malformed_entries():
    api = Api({"/drafts": (200, {"drafts": [{"no": "id"}, "junk"]})})
    out = await list_drafts(Wallet(), page_token="p3", transport=api, api=API)
    assert out["drafts"] == [] and "pageToken=p3" in api.calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("routes", [
    {"/messages/m1": _with_part("r.xml", {"attachmentId": "a1", "size": 1}),
     "/messages/m1/attachments/a1": (200, {"size": 1})},
    {"/messages/m1": _with_part("r.xml", {"size": 1})},
])
async def test_an_attachment_gmail_returns_without_data_is_a_bad_reply(tmp_path, routes):
    with pytest.raises(GmailSendError) as exc:
        await get_attachment(Wallet(), message_id="m1", filename="r.xml",
                             workspace=tmp_path, transport=Api(routes), api=API)
    assert exc.value.code == "bad_reply"


@pytest.mark.asyncio
async def test_get_attachment_needs_both_ids_and_tolerates_odd_parts(tmp_path):
    with pytest.raises(GmailSendError) as exc:
        await get_attachment(Wallet(), message_id="m1", filename=" ",
                             workspace=tmp_path, transport=Api({}), api=API)
    assert exc.value.code == "bad_request"
    data = base64.urlsafe_b64encode(b"x").decode()
    nested = (200, {"id": "m1", "payload": {"parts": ["junk", {"parts": [
        {"filename": "deep.txt", "mimeType": "text/plain", "body": {"data": data, "size": 1}}]}]}})
    out = await get_attachment(Wallet(), message_id="m1", filename="deep.txt",
                               workspace=tmp_path, transport=Api({"/messages/m1": nested}), api=API)
    assert out["path"] == "gmail-attachments/m1/deep.txt"


@pytest.mark.asyncio
async def test_a_failed_write_leaves_no_temp_file_behind(tmp_path):
    (tmp_path / "gmail-attachments" / "m1" / "r.xml").mkdir(parents=True)
    data = base64.urlsafe_b64encode(b"x").decode()
    api = Api({"/messages/m1": _with_part("r.xml", {"data": data, "size": 1})})
    with pytest.raises(OSError):
        await get_attachment(Wallet(), message_id="m1", filename="r.xml",
                             workspace=tmp_path, transport=api, api=API)
    assert not list((tmp_path / "gmail-attachments" / "m1").glob(".part-*"))

