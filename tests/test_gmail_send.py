"""gmail_send against a fake Gmail.

The cells that matter are the three outcomes: a request that went out and
lost its answer reads as "unknown", exactly once, from the daemon and
through the RPC hop alike.
"""

import asyncio
import base64
import copy
import email
import email.policy
import json
import logging

import aiohttp
import pytest
from aiohttp import web

from puffo_agent.crypto.http_client import HttpError
from puffo_agent.mcp._host_mcp import PuffoRpcClient
from puffo_agent.portal.credentials import HeldCredential
from puffo_agent.portal.gmail_send import (
    AT_TYPE, RT_TYPE, GmailSendError, build_message, create_draft, send, send_draft,
)

TOKEN, FRESH = "at-cached-7f3a", "at-fresh-91c2"


def _token(value):
    return HeldCredential(id="x", type=AT_TYPE, index=0, version=1, expire_at=None,
                          value=value.encode())


class Wallet:
    """The three AgentCredentials calls gmail_send makes."""

    def __init__(self, accounts=((0, "me@example.com"),), cached=TOKEN):
        self.accounts, self.cached, self.refreshes = list(accounts), cached, 0
        self.refresh_error = None

    async def held(self, credential_type):
        assert credential_type == RT_TYPE
        return self.accounts

    async def get(self, credential_type, index):
        assert credential_type == AT_TYPE
        return _token(self.cached) if self.cached else None

    async def refresh(self, rt_type, index):
        assert rt_type == RT_TYPE
        if self.refresh_error:
            raise self.refresh_error
        self.refreshes += 1
        return _token(FRESH)


class Gmail:
    """Scripted answers; records every request it was sent."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    async def __call__(self, url, body, headers):
        self.requests.append((headers["authorization"], json.loads(body)))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, reply = answer
        return status, json.dumps(reply).encode()


def _sent(msg_id="m-1"):
    return 200, {"id": msg_id, "threadId": "t-1"}


async def _send(wallet, gmail, **kw):
    kw = {"to": "bob@example.com", "subject": "Hi", "body": "hello there", **kw}
    return await send(wallet, transport=gmail, **kw)


@pytest.mark.asyncio
async def test_sends_the_message_with_the_held_access_token():
    gmail = Gmail(_sent())
    assert await _send(Wallet(), gmail) == {"status": "sent", "message_id": "m-1", "thread_id": "t-1"}
    (auth, body), = gmail.requests
    assert auth == f"Bearer {TOKEN}"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
    assert (msg["To"], msg["Subject"], msg.get_payload().strip()) == ("bob@example.com", "Hi", "hello there")
    assert msg["From"] is None                       # Gmail sets the authorized account


@pytest.mark.asyncio
async def test_without_a_usable_access_token_it_refreshes_first():
    gmail, wallet = Gmail(_sent()), Wallet(cached=None)
    await _send(wallet, gmail)
    assert gmail.requests[0][0] == f"Bearer {FRESH}" and wallet.refreshes == 1


@pytest.mark.asyncio
async def test_a_refused_token_is_refreshed_and_sent_once_more():
    gmail, wallet = Gmail((401, {}), _sent()), Wallet()
    assert (await _send(wallet, gmail))["status"] == "sent"
    assert [auth for auth, _ in gmail.requests] == [f"Bearer {TOKEN}", f"Bearer {FRESH}"]


@pytest.mark.asyncio
async def test_a_token_refused_twice_is_a_definite_failure_not_a_third_try():
    gmail = Gmail((401, {}), (401, {}))
    with pytest.raises(GmailSendError) as exc:
        await _send(Wallet(), gmail)
    assert exc.value.code == "gmail_401" and len(gmail.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    aiohttp.ServerDisconnectedError(),
    asyncio.TimeoutError(),
    (503, {}),
    (200, {"no": "id"}),
], ids=["disconnect", "timeout", "5xx", "2xx-without-id"])
async def test_a_lost_answer_is_unknown_and_never_resent(answer):
    gmail = Gmail(answer)
    result = await _send(Wallet(), gmail)
    assert result["status"] == "unknown" and "may have been sent" in result["detail"]
    assert len(gmail.requests) == 1


@pytest.mark.asyncio
async def test_gmail_taking_the_request_and_hanging_up_is_unknown(unused_tcp_port):
    """Through the real transport: read the request, then hang up."""
    received = []

    async def swallow(request):
        received.append(await request.read())
        request.transport.close()
        return web.Response()

    app = web.Application()
    app.router.add_post("/send", swallow)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    try:
        result = await send(Wallet(), to="bob@example.com", subject="s", body="b",
                            send_url=f"http://127.0.0.1:{unused_tcp_port}/send")
    finally:
        await runner.cleanup()
    assert result["status"] == "unknown" and len(received) == 1


@pytest.mark.asyncio
async def test_gmail_unreachable_is_a_definite_failure(unused_tcp_port):
    with pytest.raises(GmailSendError) as exc:
        await send(Wallet(), to="bob@example.com", subject="s", body="b",
                   send_url=f"http://127.0.0.1:{unused_tcp_port}/send")
    assert exc.value.code == "unreachable"


@pytest.mark.asyncio
async def test_a_failure_before_the_request_goes_out_is_definite():
    wallet = Wallet(cached=None)
    wallet.refresh_error = HttpError(503, "{}")
    gmail = Gmail()
    with pytest.raises(GmailSendError) as exc:
        await _send(wallet, gmail)
    assert exc.value.code == "refresh_failed" and gmail.requests == []


@pytest.mark.asyncio
async def test_with_two_accounts_it_asks_which_rather_than_guessing():
    wallet = Wallet(accounts=[(0, "a@example.com"), (3, "b@example.com")])
    with pytest.raises(GmailSendError, match="a@example.com, b@example.com") as exc:
        await _send(wallet, Gmail())
    assert exc.value.code == "ambiguous_account"
    picked = []

    async def get(credential_type, index):
        picked.append(index)
        return _token(TOKEN)

    wallet.get = get
    await _send(wallet, Gmail(_sent()), from_account="b@example.com")
    assert picked == [3]
    with pytest.raises(GmailSendError) as exc:
        await _send(wallet, Gmail(), from_account="c@example.com")
    assert exc.value.code == "unknown_account"


@pytest.mark.asyncio
async def test_no_shared_account_and_no_credentials_are_definite_failures():
    with pytest.raises(GmailSendError) as exc:
        await _send(Wallet(accounts=[]), Gmail())
    assert exc.value.code == "no_account"
    with pytest.raises(GmailSendError) as exc:
        await _send(None, Gmail())
    assert exc.value.code == "no_credentials"


@pytest.mark.asyncio
async def test_a_recipient_cannot_smuggle_in_a_header():
    gmail = Gmail()
    with pytest.raises(GmailSendError) as exc:
        await _send(Wallet(), gmail, to="bob@example.com\r\nBcc: eve@example.com")
    assert exc.value.code == "bad_request" and gmail.requests == []


@pytest.mark.asyncio
async def test_the_log_carries_codes_and_ids_not_tokens_recipients_or_bodies(caplog):
    caplog.set_level(logging.DEBUG)
    await _send(Wallet(), Gmail(_sent("m-42")))
    with pytest.raises(GmailSendError):
        await _send(Wallet(), Gmail((401, {}), (401, {})))
    await _send(Wallet(), Gmail(aiohttp.ServerDisconnectedError()))
    text = caplog.text
    assert "m-42" in text and "gmail_401" in text
    for secret in (TOKEN, FRESH, "bob@example.com", "hello there"):
        assert secret not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("behaviour, expected", [
    ("hang-up", "unknown"), ("500", "unknown"), ("sent", "sent"), ("400", "error"),
])
async def test_the_rpc_hop_keeps_unknown_distinct_from_failure(unused_tcp_port, behaviour, expected):
    """Losing the answer one hop earlier must read the same."""

    async def route(request):
        await request.read()
        if behaviour == "hang-up":
            request.transport.close()
            return web.Response()
        if behaviour == "500":
            return web.json_response({"error": "handler raised"}, status=500)
        if behaviour == "400":
            return web.json_response({"error": "Gmail refused", "code": "gmail_400"}, status=400)
        return web.json_response({"status": "sent", "message_id": "m-1"})

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/gmail-send", route)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    client = PuffoRpcClient(f"http://127.0.0.1:{unused_tcp_port}", "agent-1")
    try:
        if expected == "error":
            with pytest.raises(RuntimeError, match="not sent"):
                await client.gmail_send(to="b@x", subject="s", body="b")
        else:
            assert (await client.gmail_send(to="b@x", subject="s", body="b"))["status"] == expected
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_no_running_worker_is_a_definite_failure_not_unknown():
    """'no warm worker' must not read as 'may have been sent'."""
    from aiohttp.test_utils import TestClient, TestServer

    from puffo_agent.portal import rpc_service

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/gmail-send", rpc_service.gmail_send_route)
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: None
    try:
        async with TestClient(TestServer(app)) as http:
            resp = await http.post("/v1/rpc/agent-1/gmail-send",
                                   json={"to": "b@x", "subject": "s", "body": "b"})
            assert resp.status == 409 and (await resp.json())["code"] == "no_worker"
    finally:
        rpc_service._RPC_RESOLVER = previous


@pytest.mark.asyncio
@pytest.mark.parametrize("to", ["", "   "])
async def test_a_message_with_no_recipient_never_reaches_gmail(to):
    gmail = Gmail()
    with pytest.raises(GmailSendError) as exc:
        await _send(Wallet(), gmail, to=to)
    assert exc.value.code == "bad_request" and gmail.requests == []


@pytest.mark.asyncio
async def test_a_share_that_yields_no_token_at_all_is_a_definite_failure():
    """Revoked between the list and the send: nothing to authorize with."""
    wallet = Wallet(cached=None)
    wallet.refresh = lambda *a, **k: _none_token()
    gmail = Gmail()
    with pytest.raises(GmailSendError) as exc:
        await _send(wallet, gmail)
    assert exc.value.code == "not_held" and gmail.requests == []


async def _none_token():
    return None


@pytest.mark.asyncio
async def test_a_reply_that_is_not_json_is_read_by_status_alone():
    """An HTML error page from a proxy: 2xx may have gone out, 4xx did not."""
    class Raw(Gmail):
        async def __call__(self, url, body, headers):
            self.requests.append((headers["authorization"], json.loads(body)))
            status = self.answers.pop(0)
            return status, b"<html>no json here</html>"

    assert (await _send(Wallet(), Raw(200)))["status"] == "unknown"
    with pytest.raises(GmailSendError) as exc:
        await _send(Wallet(), Raw(400))
    assert exc.value.code == "gmail_400"


@pytest.mark.asyncio
async def test_gmails_own_refusal_text_reaches_the_agent():
    gmail = Gmail((400, {"error": {"message": "Recipient address required"}}))
    with pytest.raises(GmailSendError) as exc:
        await _send(Wallet(), gmail)
    assert "Recipient address required" in str(exc.value)


@pytest.mark.asyncio
async def test_the_real_transport_returns_the_status_and_body_it_was_given(unused_tcp_port):
    """``gmail_transport`` itself, against a local server standing in for Gmail."""
    from puffo_agent.portal.gmail_send import gmail_transport

    seen = {}

    async def route(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = await request.read()
        return web.json_response({"id": "m-real", "threadId": "t-real"})

    app = web.Application()
    app.router.add_post("/send", route)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    try:
        status, reply = await gmail_transport(
            f"http://127.0.0.1:{unused_tcp_port}/send", b'{"raw":"x"}',
            {"authorization": "Bearer t", "content-type": "application/json"},
        )
        assert (status, json.loads(reply)["id"]) == (200, "m-real")
        assert seen["auth"] == "Bearer t" and seen["body"] == b'{"raw":"x"}'
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_the_mcp_tool_hands_the_call_to_the_daemon():
    """The tool registers only with an RPC link, and forwards verbatim."""
    from types import SimpleNamespace as NS

    from puffo_agent.mcp.core_gmail_tools import register_gmail_tools

    registered = {}

    class FakeMcp:
        def tool(self):
            def decorate(fn):
                registered[fn.__name__] = fn
                return fn
            return decorate

    register_gmail_tools(FakeMcp(), NS(rpc_client=None))
    assert registered == {}                       # no link, no tool

    calls = []

    async def rpc_gmail_send(**kw):
        calls.append(kw)
        return {"status": "sent", "message_id": "m-9"}

    register_gmail_tools(FakeMcp(), NS(rpc_client=NS(gmail_send=rpc_gmail_send)))
    out = await registered["gmail_send"](
        to="b@x", subject="s", body="hello", from_account="me@example.com")
    assert out == {"status": "sent", "message_id": "m-9"}
    assert calls == [{"to": "b@x", "subject": "s", "body": "hello", "cc": "", "bcc": "",
                      "html_body": "", "reply_to_message_id": "",
                      "forward_message_id": "", "attachments": [],
                      "from_account": "me@example.com"}]


async def _rpc_against(port, route):
    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/gmail-send", route)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, PuffoRpcClient(f"http://127.0.0.1:{port}", "agent-1")


@pytest.mark.asyncio
async def test_the_rpc_hop_passes_the_chosen_account_on(unused_tcp_port):
    seen = {}

    async def route(request):
        seen.update(await request.json())
        return web.json_response({"status": "sent", "message_id": "m-1"})

    runner, client = await _rpc_against(unused_tcp_port, route)
    try:
        await client.gmail_send(to="b@x", subject="s", body="b", from_account="me@example.com")
        assert seen["from_account"] == "me@example.com"
        seen.clear()
        await client.gmail_send(to="b@x", subject="s", body="b")
        assert "from_account" not in seen          # omitted, not sent empty
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"not json at all", b'"a string, not an object"'])
async def test_an_unreadable_daemon_answer_is_unknown_not_a_failure(unused_tcp_port, payload):
    """A 200 we cannot read may still have sent the message."""

    async def route(request):
        return web.Response(body=payload, content_type="application/json")

    runner, client = await _rpc_against(unused_tcp_port, route)
    try:
        out = await client.gmail_send(to="b@x", subject="s", body="b")
        assert out["status"] == "unknown"
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_a_daemon_that_is_not_listening_is_a_definite_failure(unused_tcp_port):
    client = PuffoRpcClient(f"http://127.0.0.1:{unused_tcp_port}", "agent-1")
    try:
        with pytest.raises(RuntimeError, match="nothing was sent"):
            await client.gmail_send(to="b@x", subject="s", body="b")
    finally:
        await client.close()


def _route_app():
    from puffo_agent.portal import rpc_service

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/gmail-send", rpc_service.gmail_send_route)
    return app, rpc_service


@pytest.mark.asyncio
@pytest.mark.parametrize("body, data", [
    ("not json", None),
    (None, {"to": "b@x", "subject": "s", "body": "b", "extra": "no"}),
    (None, {"to": "b@x", "subject": "s", "body": 7}),
    (None, ["not", "an", "object"]),
])
async def test_the_route_refuses_a_malformed_request_outright(body, data):
    from aiohttp.test_utils import TestClient, TestServer

    app, _ = _route_app()
    async with TestClient(TestServer(app)) as http:
        kw = {"data": body} if body is not None else {"json": data}
        resp = await http.post("/v1/rpc/agent-1/gmail-send", **kw)
        assert resp.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["sent", "refused"])
async def test_the_route_reaches_the_worker_and_keeps_the_two_outcomes_apart(outcome, monkeypatch):
    """Route -> host_mcp_handler -> gmail_send, the seam the smoke test uses."""
    from types import SimpleNamespace as NS

    from aiohttp.test_utils import TestClient, TestServer

    from puffo_agent.portal import gmail_send as gs

    answer = _sent("m-route") if outcome == "sent" else (400, {"error": {"message": "nope"}})

    async def transport(url, body, headers):
        return answer[0], json.dumps(answer[1]).encode()

    monkeypatch.setattr(gs, "gmail_transport", transport)
    app, rpc_service = _route_app()
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: NS(credentials=Wallet(), workspace=None)
    try:
        async with TestClient(TestServer(app)) as http:
            resp = await http.post("/v1/rpc/agent-1/gmail-send",
                                   json={"to": "b@x", "subject": "s", "body": "b"})
            payload = await resp.json()
            if outcome == "sent":
                assert resp.status == 200 and payload["message_id"] == "m-route"
            else:
                assert resp.status == 400 and payload["code"] == "gmail_400"
    finally:
        rpc_service._RPC_RESOLVER = previous


@pytest.mark.asyncio
async def test_a_refusal_the_mcp_side_cannot_parse_stays_a_definite_failure(unused_tcp_port):
    """The daemon answers 4xx only when nothing was sent, so an unreadable
    body must not soften into "may have been sent"."""

    async def route(request):
        return web.Response(body=b"<html>gateway</html>", status=400,
                            content_type="application/json")

    runner, client = await _rpc_against(unused_tcp_port, route)
    try:
        with pytest.raises(RuntimeError, match="not sent"):
            await client.gmail_send(to="b@x", subject="s", body="b")
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_a_reply_split_across_chunks_is_read_whole(unused_tcp_port):
    """``StreamReader.read(n)`` hands back whatever is buffered, so a large
    answer came back truncated and read as malformed JSON."""
    from puffo_agent.portal.gmail_send import gmail_transport

    payload = json.dumps({"id": "m-big", "raw": "x" * 400_000}).encode()

    async def route(request):
        resp = web.StreamResponse(headers={"content-type": "application/json"})
        await resp.prepare(request)
        for start in range(0, len(payload), 32_768):
            await resp.write(payload[start:start + 32_768])
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/big", route)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    try:
        status, reply = await gmail_transport(
            f"http://127.0.0.1:{unused_tcp_port}/big", None, {}, method="GET")
        assert status == 200
        assert len(reply) == len(payload)
        assert json.loads(reply)["id"] == "m-big"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_an_answer_past_the_cap_is_refused_rather_than_buffered(unused_tcp_port, monkeypatch):
    from puffo_agent.portal import gmail_send as gs

    monkeypatch.setattr(gs, "MAX_REPLY_BYTES", 100_000)

    async def route(request):
        resp = web.StreamResponse(headers={"content-type": "application/json"})
        await resp.prepare(request)
        for _ in range(8):
            await resp.write(b"x" * 32_768)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/huge", route)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    try:
        with pytest.raises(GmailSendError) as exc:
            await gs.gmail_transport(
                f"http://127.0.0.1:{unused_tcp_port}/huge", None, {}, method="GET")
        assert exc.value.code == "too_large"
    finally:
        await runner.cleanup()


# ── composing: cc, bcc, html, replies, forwards, attachments ───────

API = "https://gmail.test/v1/users/me"


class Routes:
    """Answers by method and path; records every request."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def __call__(self, url, body, headers, *, method="POST"):
        path = url.split("?", 1)[0].split("/users/me", 1)[-1]
        self.calls.append((method, path, url, json.loads(body) if body else None))
        answer = self.routes[(method, path)]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer[0], json.dumps(answer[1]).encode()

    def sent(self):
        posts = [c for c in self.calls if c[0] == "POST"]
        assert len(posts) == 1, posts
        body = posts[0][3]
        message = body.get("message", body)
        return message, email.message_from_bytes(
            base64.urlsafe_b64decode(message["raw"]), policy=email.policy.default)


ORIGINAL = (200, {"id": "orig", "threadId": "thread-9", "payload": {"headers": [
    {"name": "Message-ID", "value": "<abc@mail.example>"},
    {"name": "References", "value": "<root@mail.example>"},
    {"name": "Subject", "value": "DMARC report"},
    {"name": "From", "value": "Alice <alice@example.com>"},
    {"name": "Reply-To", "value": "reports@example.com"},
]}})


def test_cc_bcc_and_an_html_alternative_are_carried():
    msg = email.message_from_bytes(build_message(
        "b@x", "s", "plain", cc="c@x", bcc="d@x", html_body="<p>rich</p>"),
        policy=email.policy.default)
    assert (msg["Cc"], msg["Bcc"]) == ("c@x", "d@x")
    assert msg.get_body(preferencelist=("html",)).get_content().strip() == "<p>rich</p>"
    assert msg.get_body(preferencelist=("plain",)).get_content().strip() == "plain"


def test_a_header_injection_through_cc_is_refused():
    with pytest.raises(GmailSendError) as exc:
        build_message("b@x", "s", "b", cc="c@x\r\nBcc: evil@x")
    assert exc.value.code == "bad_request"


@pytest.mark.asyncio
async def test_a_reply_stays_in_the_conversation_and_answers_the_sender():
    gmail = Routes({("GET", "/messages/orig"): ORIGINAL,
                    ("POST", "/messages/send"): _sent()})
    out = await send(Wallet(), to="", subject="", body="ack", reply_to_message_id="orig",
                     transport=gmail, send_url=f"{API}/messages/send", api=API)
    assert out["status"] == "sent"
    message, msg = gmail.sent()
    assert message["threadId"] == "thread-9"
    assert msg["In-Reply-To"] == "<abc@mail.example>"
    assert msg["References"] == "<root@mail.example> <abc@mail.example>"
    assert (msg["To"], msg["Subject"]) == ("reports@example.com", "Re: DMARC report")


@pytest.mark.asyncio
async def test_a_reply_does_not_stack_re_prefixes():
    original = copy.deepcopy(ORIGINAL)
    original[1]["payload"]["headers"][2]["value"] = "RE: DMARC report"
    gmail = Routes({("GET", "/messages/orig"): original, ("POST", "/messages/send"): _sent()})
    await send(Wallet(), to="x@y", subject="", body="ack", reply_to_message_id="orig",
               transport=gmail, send_url=f"{API}/messages/send", api=API)
    assert gmail.sent()[1]["Subject"] == "RE: DMARC report"


@pytest.mark.asyncio
async def test_a_forward_carries_the_original_with_its_attachments():
    inner = email.message.EmailMessage()
    inner["Subject"] = "Q3 numbers"
    inner["Bcc"] = "hidden@x"
    inner.set_content("see attached")
    inner.add_attachment(b"1,2,3", maintype="text", subtype="csv", filename="q3.csv")
    raw = base64.urlsafe_b64encode(inner.as_bytes()).decode()
    gmail = Routes({("GET", "/messages/orig"): (200, {"id": "orig", "threadId": "t-7", "raw": raw}),
                    ("POST", "/messages/send"): _sent()})
    await send(Wallet(), to="boss@x", subject="", body="fyi", forward_message_id="orig",
               transport=gmail, send_url=f"{API}/messages/send", api=API)
    message, msg = gmail.sent()
    assert message["threadId"] == "t-7" and msg["Subject"] == "Fwd: Q3 numbers"
    (forwarded,) = [p for p in msg.iter_attachments() if p.get_content_type() == "message/rfc822"]
    assert "q3.csv" in forwarded.as_string()
    assert "hidden@x" not in forwarded.as_string()


@pytest.mark.asyncio
async def test_a_failed_lookup_of_the_original_sends_nothing():
    gmail = Routes({("GET", "/messages/orig"): (503, {})})
    with pytest.raises(GmailSendError) as exc:
        await send(Wallet(), to="", subject="", body="ack", reply_to_message_id="orig",
                   transport=gmail, send_url=f"{API}/messages/send", api=API)
    assert exc.value.code == "lookup_failed" and "nothing was sent" in str(exc.value)
    assert all(m == "GET" for m, *_ in gmail.calls)


@pytest.mark.asyncio
async def test_reply_and_forward_together_are_refused_before_any_call():
    gmail = Routes({})
    with pytest.raises(GmailSendError) as exc:
        await send(Wallet(), to="x@y", subject="s", body="b", reply_to_message_id="a",
                   forward_message_id="b", transport=gmail, api=API)
    assert exc.value.code == "bad_request" and gmail.calls == []


@pytest.mark.asyncio
async def test_workspace_files_are_attached(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "summary.csv").write_bytes(b"a,b\n1,2\n")
    gmail = Routes({("POST", "/messages/send"): _sent()})
    await send(Wallet(), to="x@y", subject="s", body="b", attachments=["out/summary.csv"],
               workspace=tmp_path, transport=gmail, send_url=f"{API}/messages/send", api=API)
    (part,) = list(gmail.sent()[1].iter_attachments())
    assert (part.get_filename(), part.get_content_type()) == ("summary.csv", "text/csv")
    assert part.get_content() == "a,b\n1,2\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("make, code", [
    (lambda ws: "/etc/hostname", "bad_request"),
    (lambda ws: "../escape.txt", "bad_request"),
    (lambda ws: "missing.txt", "bad_request"),
    (lambda ws: "link.txt", "bad_request"),
    (lambda ws: "big.bin", "too_large"),
])
async def test_an_attachment_outside_the_rules_sends_nothing(tmp_path, make, code):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "escape.txt").write_text("secret")
    (workspace / "link.txt").symlink_to(tmp_path / "escape.txt")
    with open(workspace / "big.bin", "wb") as f:
        f.truncate(25 * 1024 * 1024 + 1)
    gmail = Routes({})
    with pytest.raises(GmailSendError) as exc:
        await send(Wallet(), to="x@y", subject="s", body="b", attachments=[make(workspace)],
                   workspace=workspace, transport=gmail, api=API)
    assert exc.value.code == code and gmail.calls == []


@pytest.mark.asyncio
async def test_an_absolute_attachment_path_is_told_to_be_relative(tmp_path):
    with pytest.raises(GmailSendError) as exc:
        await send(Wallet(), to="x@y", subject="s", body="b", attachments=[str(tmp_path / "a")],
                   workspace=tmp_path, transport=Routes({}), api=API)
    assert "relative to your workspace" in str(exc.value)


@pytest.mark.asyncio
async def test_attachments_need_a_workspace_and_at_most_ten_files(tmp_path):
    for i in range(11):
        (tmp_path / f"f{i}").write_text("x")
    with pytest.raises(GmailSendError) as exc:
        await send(Wallet(), to="x@y", subject="s", body="b", attachments=["f0"],
                   workspace=None, transport=Routes({}), api=API)
    assert exc.value.code == "no_workspace"
    with pytest.raises(GmailSendError) as exc:
        await send(Wallet(), to="x@y", subject="s", body="b",
                   attachments=[f"f{i}" for i in range(11)], workspace=tmp_path,
                   transport=Routes({}), api=API)
    assert exc.value.code == "bad_request"


# ── drafts ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_draft_is_saved_not_sent():
    gmail = Routes({("POST", "/drafts"): (200, {"id": "r-1", "message": {"id": "m-5", "threadId": "t-5"}})})
    out = await create_draft(Wallet(), to="x@y", subject="s", body="b", transport=gmail, api=API)
    assert out == {"status": "created", "draft_id": "r-1", "message_id": "m-5", "thread_id": "t-5"}
    assert [(m, p) for m, p, *_ in gmail.calls] == [("POST", "/drafts")]
    assert gmail.calls[0][3]["message"]["raw"]


@pytest.mark.asyncio
async def test_a_lost_draft_answer_is_unknown_and_points_at_the_draft_list():
    gmail = Routes({("POST", "/drafts"): aiohttp.ServerDisconnectedError()})
    out = await create_draft(Wallet(), to="x@y", subject="s", body="b", transport=gmail, api=API)
    assert out["status"] == "unknown" and "gmail_list_drafts" in out["detail"]


@pytest.mark.asyncio
async def test_a_refused_draft_is_a_definite_failure():
    gmail = Routes({("POST", "/drafts"): (400, {"error": {"message": "bad"}})})
    with pytest.raises(GmailSendError) as exc:
        await create_draft(Wallet(), to="x@y", subject="s", body="b", transport=gmail, api=API)
    assert exc.value.code == "gmail_400"


@pytest.mark.asyncio
async def test_sending_a_draft_keeps_the_three_outcomes():
    sent = Routes({("POST", "/drafts/send"): _sent("m-8")})
    assert (await send_draft(Wallet(), draft_id="r-1", transport=sent, api=API))["status"] == "sent"
    assert sent.calls[0][3] == {"id": "r-1"}
    lost = Routes({("POST", "/drafts/send"): aiohttp.ServerDisconnectedError()})
    out = await send_draft(Wallet(), draft_id="r-1", transport=lost, api=API)
    assert out["status"] == "unknown" and "Sent folder" in out["detail"]
    with pytest.raises(GmailSendError) as exc:
        await send_draft(Wallet(), draft_id=" ", transport=Routes({}), api=API)
    assert exc.value.code == "bad_request"


@pytest.mark.asyncio
async def test_the_draft_route_validates_then_reaches_the_worker(monkeypatch):
    from types import SimpleNamespace as NS

    from aiohttp.test_utils import TestClient, TestServer

    from puffo_agent.portal import gmail_send as gs
    from puffo_agent.portal import rpc_service

    async def transport(url, body, headers):
        return 200, json.dumps({"id": "r-9", "message": {"id": "m", "threadId": "t"}}).encode()

    monkeypatch.setattr(gs, "gmail_transport", transport)
    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/gmail-draft", rpc_service.gmail_draft_route)
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: NS(credentials=Wallet(), workspace=None)
    try:
        async with TestClient(TestServer(app)) as http:
            ok = await http.post("/v1/rpc/agent-1/gmail-draft",
                                 json={"op": "create", "to": "b@x", "subject": "s", "body": "b"})
            assert ok.status == 200 and (await ok.json())["draft_id"] == "r-9"
            for bad in ({"op": "delete"}, {"op": "send"}, {"op": "send", "draft_id": 1},
                        {"op": "create", "attachments": "a.txt"}, {"op": "create", "x": "y"}):
                assert (await http.post("/v1/rpc/agent-1/gmail-draft", json=bad)).status == 400
            rpc_service._RPC_RESOLVER = lambda agent_id: None
            no_worker = await http.post("/v1/rpc/agent-1/gmail-draft",
                                        json={"op": "send", "draft_id": "r-9"})
            assert no_worker.status == 409
    finally:
        rpc_service._RPC_RESOLVER = previous


@pytest.mark.asyncio
async def test_a_lost_draft_answer_on_the_rpc_hop_is_unknown(unused_tcp_port):
    async def route(request):
        raise web.HTTPInternalServerError()

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/gmail-draft", route)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    client = PuffoRpcClient(f"http://127.0.0.1:{unused_tcp_port}", "agent-1")
    try:
        created = await client.gmail_draft("create", to="b@x", subject="s", body="b")
        assert created["status"] == "unknown" and "gmail_list_drafts" in created["detail"]
        sent = await client.gmail_draft("send", draft_id="r-1")
        assert sent["status"] == "unknown" and "Sent folder" in sent["detail"]
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_the_draft_tools_forward_to_the_daemon():
    from types import SimpleNamespace as NS

    from puffo_agent.mcp.core_gmail_tools import register_gmail_tools

    registered, calls = {}, []

    class FakeMcp:
        def tool(self):
            def decorate(fn):
                registered[fn.__name__] = fn
                return fn
            return decorate

    async def gmail_draft(op, **fields):
        calls.append((op, fields))
        return {"ok": op}

    register_gmail_tools(FakeMcp(), NS(rpc_client=NS(gmail_draft=gmail_draft,
                                                     gmail_send=None, gmail_mailbox=None)))
    assert await registered["gmail_create_draft"](to="b@x", body="hi") == {"ok": "create"}
    assert await registered["gmail_send_draft"](draft_id="r-1") == {"ok": "send"}
    assert calls[0][1]["to"] == "b@x" and calls[0][1]["attachments"] == []
    assert calls[1] == ("send", {"draft_id": "r-1", "from_account": ""})


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [
    (200, {"id": "orig", "threadId": "t"}),
    (200, {"id": "orig", "threadId": "t", "raw": "!!!not base64!!!"}),
])
async def test_an_unreadable_original_is_not_forwarded(original):
    gmail = Routes({("GET", "/messages/orig"): original})
    with pytest.raises(GmailSendError) as exc:
        await send(Wallet(), to="x@y", subject="", body="b", forward_message_id="orig",
                   transport=gmail, api=API)
    assert exc.value.code == "bad_reply" and all(m == "GET" for m, *_ in gmail.calls)


@pytest.mark.asyncio
async def test_an_oversized_message_and_a_bare_string_of_paths_are_refused():
    with pytest.raises(GmailSendError) as exc:
        await send(Wallet(), to="x@y", subject="s", body="x" * (35 * 1024 * 1024),
                   transport=Routes({}), api=API)
    assert exc.value.code == "too_large"
    with pytest.raises(GmailSendError) as exc:
        await send(Wallet(), to="x@y", subject="s", body="b", attachments="a.txt",
                   transport=Routes({}), api=API)
    assert exc.value.code == "bad_request"


@pytest.mark.asyncio
async def test_an_unreadable_draft_answer_is_unknown_and_an_odd_refusal_still_fails():
    async def garbled(url, body, headers):
        return 200, b"<html>"

    out = await create_draft(Wallet(), to="x@y", subject="s", body="b", transport=garbled, api=API)
    assert out["status"] == "unknown"

    async def plain_refusal(url, body, headers):
        return 403, json.dumps({"error": "forbidden"}).encode()

    with pytest.raises(GmailSendError) as exc:
        await create_draft(Wallet(), to="x@y", subject="s", body="b",
                           transport=plain_refusal, api=API)
    assert exc.value.code == "gmail_403"


@pytest.mark.asyncio
async def test_the_routes_hand_the_workspace_and_drafts_through(tmp_path, monkeypatch):
    """Route -> handler for the two branches only a real context exercises:
    sending a draft, and saving an attachment into ``ctx.workspace``."""
    from types import SimpleNamespace as NS

    from aiohttp.test_utils import TestClient, TestServer

    from puffo_agent.portal import gmail_mailbox as gm
    from puffo_agent.portal import gmail_send as gs
    from puffo_agent.portal import rpc_service

    data = base64.urlsafe_b64encode(b"<feedback/>").decode()
    message = (200, {"id": "m1", "payload": {"parts": [
        {"filename": "r.xml", "mimeType": "application/xml", "body": {"data": data, "size": 11}}]}})
    reads = Routes({("GET", "/messages/m1"): message})

    async def read_transport(url, body, headers, *, method="POST"):
        return await reads(url.replace("https://gmail.googleapis.com/gmail/v1", "x"), body, headers, method=method)

    answers = [(200, {"id": "m-2", "threadId": "t"}), (404, {"error": {"message": "gone"}})]

    async def send_transport(url, body, headers):
        status, reply = answers.pop(0)
        return status, json.dumps(reply).encode()

    monkeypatch.setattr(gm, "gmail_transport", read_transport)
    monkeypatch.setattr(gs, "gmail_transport", send_transport)
    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/gmail-draft", rpc_service.gmail_draft_route)
    app.router.add_post("/v1/rpc/{agent_id}/gmail-mailbox", rpc_service.gmail_mailbox_route)
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: NS(credentials=Wallet(), workspace=tmp_path)
    try:
        async with TestClient(TestServer(app)) as http:
            sent = await http.post("/v1/rpc/agent-1/gmail-draft", json={"op": "send", "draft_id": "r-1"})
            assert sent.status == 200 and (await sent.json())["status"] == "sent"
            gone = await http.post("/v1/rpc/agent-1/gmail-draft", json={"op": "send", "draft_id": "r-1"})
            assert gone.status == 400 and (await gone.json())["code"] == "gmail_404"
            garbled = await http.post("/v1/rpc/agent-1/gmail-draft", data=b"{not json",
                                      headers={"content-type": "application/json"})
            assert garbled.status == 400
            saved = await http.post("/v1/rpc/agent-1/gmail-mailbox", json={
                "op": "get_attachment", "message_id": "m1", "filename": "r.xml"})
            assert saved.status == 200
            assert (tmp_path / (await saved.json())["path"]).read_bytes() == b"<feedback/>"
    finally:
        rpc_service._RPC_RESOLVER = previous
