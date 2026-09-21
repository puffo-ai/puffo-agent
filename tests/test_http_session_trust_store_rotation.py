"""A cached HTTP session must not outlive the trust store it was built from.

Regression for PUF-377 (staging 2026-09-21, sera-5081): the daemon survives
every E2B pause→resume; a resume that lands on another node rewrites the
system CA bundle with that node's proxy CA, and the session created before
the move can no longer verify the relay — four sends failed in 25 ms each,
nothing reached puffo-server, while a fresh connection from the same sandbox
verified fine. What is pinned: the session is rebuilt when the trust store's
fingerprint changes (before any request is attempted, so nothing replays);
an unchanged store keeps the session; a certificate error mid-request drops
the session for the next attempt but is surfaced, never replayed.
"""

from __future__ import annotations

import json
import ssl

import aiohttp
import pytest

from puffo_agent.crypto import http_client as hc
from puffo_agent.crypto import http_session as hs
from puffo_agent.crypto.http_client import PuffoCoreHttpClient


class _FakeResponse:
    status = 200

    async def text(self) -> str:
        return json.dumps({"ok": True})


class _RequestCtx:
    def __init__(self, exc: BaseException | None):
        self._exc = exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return _FakeResponse()

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, exc: BaseException | None = None):
        self._exc = exc
        self.closed = False
        self.requests = 0

    def request(self, method, url, **kwargs):
        self.requests += 1
        return _RequestCtx(self._exc)

    async def close(self):
        self.closed = True


def _client(monkeypatch, sessions: list[_FakeSession]) -> PuffoCoreHttpClient:
    client = PuffoCoreHttpClient.__new__(PuffoCoreHttpClient)
    client.server_url = "https://relay.test"
    client.keyless = True
    client._session = None
    client._session_trust = None
    made = iter(sessions)
    monkeypatch.setattr(hc, "create_remote_http_session", lambda *_a, **_k: next(made))
    monkeypatch.setattr(client, "_egress_headers", lambda base=None: dict(base or {}))
    return client


def _cert_error() -> aiohttp.ClientConnectorCertificateError:
    key = aiohttp.client_reqrep.ConnectionKey("relay.test", 443, True, True, None, None, None)
    return aiohttp.ClientConnectorCertificateError(
        key, ssl.SSLCertVerificationError("unable to get local issuer certificate")
    )


@pytest.mark.asyncio
async def test_session_is_rebuilt_when_the_trust_store_moves(monkeypatch):
    """The E2B node move: the CA bundle's mtime changes between two sends."""
    fingerprints = iter([("/etc/ssl/certs/ca-certificates.crt", 1, 100)] * 2 + [("/etc/ssl/certs/ca-certificates.crt", 2, 101)] * 2)
    monkeypatch.setattr(hc, "trust_store_fingerprint", lambda: fingerprints.__next__())
    sessions = [_FakeSession(), _FakeSession()]
    client = _client(monkeypatch, sessions)

    assert await client.get_unsigned("/one") == {"ok": True}
    assert await client.get_unsigned("/two") == {"ok": True}  # same store → same session
    assert sessions[0].requests == 2 and not sessions[0].closed

    assert await client.get_unsigned("/three") == {"ok": True}  # store moved → rebuilt first
    assert sessions[0].closed
    assert sessions[1].requests == 1
    assert client._session is sessions[1]


@pytest.mark.asyncio
async def test_unchanged_store_keeps_the_session(monkeypatch):
    monkeypatch.setattr(hc, "trust_store_fingerprint", lambda: (("/ca.crt", 7, 7),))
    sessions = [_FakeSession(), _FakeSession()]
    client = _client(monkeypatch, sessions)
    for _ in range(5):
        await client.get_unsigned("/ping")
    assert sessions[0].requests == 5 and not sessions[0].closed
    assert sessions[1].requests == 0


@pytest.mark.asyncio
async def test_certificate_error_drops_the_session_but_is_never_replayed(monkeypatch):
    """Belt to the fingerprint's suspenders: if verification still fails
    (a CA change the fingerprint did not see), the session is dropped so
    the caller's *next* attempt gets a fresh context — the send
    coordinator retries once — but this attempt surfaces, because past a
    redirect hop bytes may already be on the wire."""
    monkeypatch.setattr(hc, "trust_store_fingerprint", lambda: (("/ca.crt", 1, 1),))
    sessions = [_FakeSession(exc=_cert_error()), _FakeSession()]
    client = _client(monkeypatch, sessions)

    with pytest.raises(aiohttp.ClientConnectorCertificateError):
        await client.get_unsigned("/ping")
    assert sessions[0].requests == 1 and sessions[0].closed
    assert sessions[1].requests == 0  # not replayed

    assert await client.get_unsigned("/ping") == {"ok": True}  # next attempt: fresh session
    assert sessions[1].requests == 1


def test_trust_store_fingerprint_tracks_the_system_bundle(tmp_path, monkeypatch):
    ca = tmp_path / "ca-certificates.crt"
    ca.write_text("one")
    paths = ssl.DefaultVerifyPaths(str(ca), None, "SSL_CERT_FILE", str(ca), "SSL_CERT_DIR", None)
    monkeypatch.setattr(hs.ssl, "get_default_verify_paths", lambda: paths)
    monkeypatch.setattr(hs.certifi, "where", lambda: str(tmp_path / "missing.pem"))

    before = hs.trust_store_fingerprint()
    assert [p for p, *_ in before] == [str(ca), str(tmp_path / "missing.pem")]
    assert before[1][1:] == (-1, -1)  # a missing file is "absent", not an error

    ca.write_text("two-longer")
    after = hs.trust_store_fingerprint()
    assert after != before and after[0][2] == len("two-longer")
