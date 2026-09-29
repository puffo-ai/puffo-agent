"""gmail_send against a fake Gmail: never the real one.

The cells that matter most are the three outcomes. An agent that reads
"failed" sends again, so a request that went out and lost its answer must
come back as "unknown", exactly once, from the daemon and through the RPC
hop alike (Jeff 227820).
"""

import asyncio
import base64
import email
import json
import logging

import aiohttp
import pytest
from aiohttp import web

from puffo_agent.crypto.http_client import HttpError
from puffo_agent.mcp._host_mcp import PuffoRpcClient
from puffo_agent.portal.credentials import HeldCredential
from puffo_agent.portal.gmail_send import AT_TYPE, RT_TYPE, GmailSendError, send

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
    """Jeff 227820, end to end through the real transport: a server that
    reads the whole request and drops the connection without an id."""
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
    """Boris 227821."""
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
    """Boris 227821."""
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
    """The MCP process talks to the daemon over loopback RPC; losing that
    answer is the same situation one hop earlier and must read the same."""

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
