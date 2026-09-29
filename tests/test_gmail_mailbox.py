"""Reading and filing mail, against a fake Gmail: never the real one.

Everything here is idempotent, which is the whole reason it is separate from
sending: a lost answer is a failure the agent may retry, not a "may have
happened". The cells that matter check that split, that nothing here can
delete mail permanently, and that bodies and addresses stay out of the log.
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
    assert await search(Wallet(), transport=api, api=API) == {"messages": [], "count": 0}


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
    """Bulk senders ship a blank text/plain next to the real HTML; preferring
    plain blindly would hand the agent an empty message."""
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
    """The send path answers "unknown" because it cannot know. Filing mail
    is idempotent, so the honest answer is "try again"."""
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
    """A token minted before the mailbox scope was widened keeps the old
    scope through every refresh, so re-consent is the only fix."""
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
    """The send route would say "may have been sent". Here the honest answer
    is that nothing changed twice, so try again."""
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
    assert calls == [
        ("search", {"query": "is:unread", "limit": 3, "from_account": ""}),
        ("read", {"message_id": "m1", "from_account": ""}),
        ("organize", {"message_id": "m1", "action": "trash", "from_account": ""}),
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
