"""PuffoCoreHttpClient recovery from a stale keep-alive connection.

Regression for the cloud-agent status-Log bug: after an E2B pause/resume the
cached aiohttp session's pooled socket is dead but the session is neither
``None`` nor ``.closed``, so every reused-session request failed forever and the
agent stopped reporting status. The client must drop the session and retry once
on a fresh connection.
"""

import asyncio
import os
import sys
import tempfile
import time

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.crypto import http_client as hc
from puffo_agent.crypto.http_client import PuffoCoreHttpClient
from puffo_agent.crypto.keystore import KeyStore, Session, StoredIdentity, encode_secret
from puffo_agent.crypto.primitives import Ed25519KeyPair


def _client() -> PuffoCoreHttpClient:
    d = tempfile.mkdtemp()
    ks = KeyStore(os.path.join(d, "keys"))
    device_key = Ed25519KeyPair.generate()
    ks.save_identity(
        StoredIdentity(
            slug="alice-0001",
            device_id="dev_test",
            root_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
            device_signing_secret_key=encode_secret(device_key.secret_bytes()),
            kem_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
            server_url="http://localhost:3000",
        )
    )
    subkey = Ed25519KeyPair.generate()
    ks.save_session(
        Session(
            slug="alice-0001",
            subkey_id="sk_test",
            subkey_secret_key=encode_secret(subkey.secret_bytes()),
            expires_at=int(time.time() * 1000) + 3_600_000,  # fresh -> no rotation/network
        )
    )
    return PuffoCoreHttpClient("http://localhost:3000", ks, "alice-0001")


class _FakeResp:
    def __init__(self, status=200, text='{"ok": true}'):
        self._status = status
        self._text = text

    @property
    def status(self):
        return self._status

    async def text(self):
        return self._text

    async def read(self):
        return self._text.encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _RaisingCM:
    """Async ctx manager that raises on enter — models a dead pooled socket."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, *, raise_exc=None, resp=None):
        self.closed = False
        self._raise_exc = raise_exc  # raised on the FIRST request, then cleared
        self._resp = resp or _FakeResp()
        self.request_calls = 0

    def request(self, method, url, **kw):
        self.request_calls += 1
        if self._raise_exc is not None:
            exc, self._raise_exc = self._raise_exc, None
            return _RaisingCM(exc)
        return self._resp

    async def close(self):
        self.closed = True


def _patch_sessions(monkeypatch, sessions):
    it = iter(sessions)
    monkeypatch.setattr(hc, "create_remote_http_session", lambda url, **k: next(it))


def test_resets_and_retries_on_stale_connection(monkeypatch):
    # First (post-resume) session's socket is dead; second is fresh.
    s1 = _FakeSession(raise_exc=aiohttp.ServerDisconnectedError("stale keep-alive"))
    s2 = _FakeSession(resp=_FakeResp(200, '{"ok": true}'))
    _patch_sessions(monkeypatch, [s1, s2])

    c = _client()
    out = asyncio.run(c.post("/agents/me/heartbeat", {"status": "busy"}))

    assert out == {"ok": True}
    assert s1.closed is True          # dead session torn down
    assert s2.request_calls == 1      # retried on a fresh connection


def test_retries_on_timeout(monkeypatch):
    s1 = _FakeSession(raise_exc=asyncio.TimeoutError())
    s2 = _FakeSession(resp=_FakeResp(200, '{"ok": true}'))
    _patch_sessions(monkeypatch, [s1, s2])

    c = _client()
    out = asyncio.run(c.get("/spaces"))

    assert out == {"ok": True}
    assert s1.closed is True
    assert s2.request_calls == 1


def test_no_reset_on_success(monkeypatch):
    s1 = _FakeSession(resp=_FakeResp(200, '{"ok": true}'))
    _patch_sessions(monkeypatch, [s1])

    c = _client()
    out = asyncio.run(c.post("/agents/me/heartbeat", {"status": "idle"}))

    assert out == {"ok": True}
    assert s1.closed is False         # healthy session reused, not torn down
    assert s1.request_calls == 1


def test_gives_up_after_one_retry(monkeypatch):
    # Both attempts hit dead sockets -> the connection error propagates.
    s1 = _FakeSession(raise_exc=aiohttp.ServerDisconnectedError("dead"))
    s2 = _FakeSession(raise_exc=aiohttp.ServerDisconnectedError("still dead"))
    _patch_sessions(monkeypatch, [s1, s2])

    c = _client()
    raised = False
    try:
        asyncio.run(c.post("/agents/me/heartbeat", {"status": "busy"}))
    except aiohttp.ClientConnectionError:
        raised = True
    assert raised is True
    assert s1.closed is True           # reset once
    assert s2.request_calls == 1       # exactly one retry, no infinite loop
