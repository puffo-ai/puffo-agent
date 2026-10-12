"""The request path every connected provider shares.

What matters: the right account is chosen or the agent is asked, a refused
token is refreshed exactly once, and "never went out" stays distinct from
"the answer was lost". Provider reply semantics are tested with each provider.
"""

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web

from puffo_agent.crypto.http_client import HttpError
from puffo_agent.portal.credentials import HeldCredential
from puffo_agent.portal.oauth_connector import (
    MAX_REPLY_BYTES,
    Account,
    Connector,
    ConnectorError,
    ConnectorLostAnswer,
    access_token,
    call,
    http_transport,
    pick_account,
)

ACME = Connector(name="acme", label="Acme account",
                 rt_type="PUFFO_ACME_OAUTH_v1", at_type="PUFFO_ACME_OAUTH_AT_v1")
TOKEN, FRESH = "at-cached-7f3a", "at-fresh-91c2"


def token(connector, value):
    return HeldCredential(id="x", type=connector.at_type, index=0, version=1,
                          expire_at=None, value=value.encode())


class Wallet:
    """The three AgentCredentials calls a connector makes."""

    def __init__(self, connector=ACME, accounts=((0, "acme-main"),), cached=TOKEN):
        self.connector, self.accounts, self.cached = connector, list(accounts), cached
        self.refreshes, self.refresh_error, self.picked = 0, None, []

    async def held(self, credential_type):
        assert credential_type == self.connector.rt_type
        return self.accounts

    async def get(self, credential_type, index):
        assert credential_type == self.connector.at_type
        self.picked.append(index)
        return token(self.connector, self.cached) if self.cached else None

    async def refresh(self, rt_type, index):
        assert rt_type == self.connector.rt_type
        if self.refresh_error:
            raise self.refresh_error
        self.refreshes += 1
        return token(self.connector, FRESH)


class Scripted:
    """Answers in order; records every request. An answer is an exception,
    ``(status, json-able)`` or ``(status, bytes)``."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    async def __call__(self, url, body, headers, *, method="POST"):
        self.requests.append((method, url, headers, json.loads(body) if body else None))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, reply = answer
        return status, reply if isinstance(reply, bytes) else json.dumps(reply).encode()


class Routed(Scripted):
    """Answers keyed by the path under ``base``; a list answers in order."""

    def __init__(self, base, routes, fail_with=None):
        super().__init__()
        self.base, self.routes, self.fail_with = base, routes, fail_with

    async def __call__(self, url, body, headers, *, method="POST"):
        self.requests.append((method, url, headers, json.loads(body) if body else None))
        if self.fail_with is not None:
            raise self.fail_with
        answer = self.routes[url[len(self.base):].split("?", 1)[0]]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, reply = answer
        return status, reply if isinstance(reply, bytes) else json.dumps(reply).encode()


def refused_401(status, data):
    return status == 401


async def _call(wallet, transport, **kw):
    account = await pick_account(ACME, wallet, kw.pop("from_account", ""))
    return await call(ACME, account, kw.pop("method", "POST"), "https://acme.test/do",
                      transport=transport, refused=refused_401, **kw)


@pytest.mark.asyncio
async def test_the_held_token_is_sent_and_params_and_body_are_encoded():
    api = Scripted((200, {"ok": True}))
    status, data = await _call(Wallet(), api, params={"q": "a b", "n": 2},
                               payload={"text": "hi"}, headers={"user-agent": "x"})
    assert (status, data) == (200, {"ok": True})
    (method, url, headers, body), = api.requests
    assert (method, url, body) == ("POST", "https://acme.test/do?q=a+b&n=2", {"text": "hi"})
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["content-type"].startswith("application/json")
    assert headers["user-agent"] == "x"


@pytest.mark.asyncio
async def test_a_get_sends_no_body_and_no_content_type():
    api = Scripted((200, {}))
    await _call(Wallet(), api, method="GET")
    (method, _, headers, body), = api.requests
    assert (method, body) == ("GET", None) and "content-type" not in headers


@pytest.mark.asyncio
async def test_without_a_usable_token_it_refreshes_first():
    api, wallet = Scripted((200, {})), Wallet(cached=None)
    await _call(wallet, api)
    assert api.requests[0][2]["authorization"] == f"Bearer {FRESH}" and wallet.refreshes == 1


@pytest.mark.asyncio
async def test_a_refused_token_is_refreshed_once_and_the_second_answer_is_returned():
    api, wallet = Scripted((401, {}), (401, {"still": "no"})), Wallet()
    assert await _call(wallet, api) == (401, {"still": "no"})
    assert [h["authorization"] for _, _, h, _ in api.requests] == [f"Bearer {TOKEN}", f"Bearer {FRESH}"]
    assert wallet.refreshes == 1


@pytest.mark.asyncio
async def test_a_reply_that_is_not_json_is_handed_over_as_none():
    assert await _call(Wallet(), Scripted((502, b"<html>bad gateway</html>"))) == (502, None)


@pytest.mark.asyncio
async def test_unreachable_is_definite_but_a_dropped_connection_is_a_lost_answer():
    with pytest.raises(ConnectorError) as exc:
        await _call(Wallet(), Scripted(aiohttp.ClientConnectorError(None, OSError("refused"))))
    assert exc.value.code == "unreachable"
    for failure in (aiohttp.ServerDisconnectedError(), asyncio.TimeoutError()):
        with pytest.raises(ConnectorLostAnswer):
            await _call(Wallet(), Scripted(failure))


@pytest.mark.asyncio
async def test_a_failure_before_the_request_goes_out_is_definite():
    wallet = Wallet(cached=None)
    wallet.refresh_error = HttpError(503, "{}")
    api = Scripted()
    with pytest.raises(ConnectorError) as exc:
        await _call(wallet, api)
    assert exc.value.code == "refresh_failed" and api.requests == []
    wallet = Wallet(cached=None)
    wallet.refresh = _none
    with pytest.raises(ConnectorError) as exc:
        await _call(wallet, api)
    assert exc.value.code == "not_held" and api.requests == []


async def _none(*_a, **_k):
    return None


@pytest.mark.asyncio
async def test_with_two_accounts_it_asks_which_rather_than_guessing():
    wallet = Wallet(accounts=[(0, "acme-a"), (3, "acme-b")])
    with pytest.raises(ConnectorError, match="acme-a, acme-b") as exc:
        await pick_account(ACME, wallet, "")
    assert exc.value.code == "ambiguous_account"
    assert await pick_account(ACME, wallet, "acme-b") == Account(wallet, 3)
    with pytest.raises(ConnectorError) as exc:
        await pick_account(ACME, wallet, "acme-c")
    assert exc.value.code == "unknown_account"


@pytest.mark.asyncio
async def test_no_shared_account_and_no_credentials_are_definite_failures():
    with pytest.raises(ConnectorError) as exc:
        await pick_account(ACME, Wallet(accounts=[]), "")
    assert exc.value.code == "no_account" and "Acme account" in str(exc.value)
    with pytest.raises(ConnectorError) as exc:
        await pick_account(ACME, None, "")
    assert exc.value.code == "no_credentials"
    wallet = Wallet()
    wallet.held = _boom
    with pytest.raises(ConnectorError) as exc:
        await pick_account(ACME, wallet, "")
    assert exc.value.code == "list_failed"


async def _boom(*_a, **_k):
    raise HttpError(500, "{}")


@pytest.mark.asyncio
async def test_a_fresh_token_skips_the_cache():
    wallet = Wallet()
    assert await access_token(ACME, Account(wallet, 0), fresh=True) == FRESH
    assert wallet.picked == [] and wallet.refreshes == 1


@pytest.mark.asyncio
async def test_the_real_transport_returns_status_and_body_and_honours_the_method(unused_tcp_port):
    seen = {}

    async def route(request):
        seen.update(method=request.method, auth=request.headers.get("authorization"),
                    body=await request.read())
        return web.json_response({"id": "real"})

    async def huge(request):
        return web.Response(body=b"x" * (MAX_REPLY_BYTES + 1))

    app = web.Application()
    app.router.add_route("*", "/do", route)
    app.router.add_get("/huge", huge)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    base = f"http://127.0.0.1:{unused_tcp_port}"
    try:
        status, reply = await http_transport(f"{base}/do", None, {"authorization": "Bearer t"},
                                             method="GET")
        assert (status, json.loads(reply)) == (200, {"id": "real"})
        assert seen == {"method": "GET", "auth": "Bearer t", "body": b""}
        with pytest.raises(ConnectorError) as exc:
            await http_transport(f"{base}/huge", None, {}, method="GET")
        assert exc.value.code == "too_large"
    finally:
        await runner.cleanup()
