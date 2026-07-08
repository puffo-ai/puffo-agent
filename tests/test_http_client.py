import asyncio
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from aiohttp_socks import ProxyConnector

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.crypto.certs import SUBKEY_TTL_HOURS
from puffo_agent.crypto.encoding import base64url_decode, base64url_encode
from puffo_agent.crypto.http_auth import sign_request
from puffo_agent.crypto.http_client import HttpError, PuffoCoreHttpClient
from puffo_agent.crypto.http_session import create_remote_http_session
from puffo_agent.crypto.keystore import KeyStore, Session, StoredIdentity, encode_secret
from puffo_agent.crypto.primitives import Ed25519KeyPair, ed25519_verify


def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_keystore_with_identity():
    d = tempfile.mkdtemp()
    ks = KeyStore(os.path.join(d, "keys"))
    root_key = Ed25519KeyPair.generate()
    device_key = Ed25519KeyPair.generate()
    kem_key = Ed25519KeyPair.generate()
    identity = StoredIdentity(
        slug="alice-0001",
        device_id="dev_test",
        root_secret_key=encode_secret(root_key.secret_bytes()),
        device_signing_secret_key=encode_secret(device_key.secret_bytes()),
        kem_secret_key=encode_secret(kem_key.secret_bytes()),
        server_url="http://localhost:3000",
    )
    ks.save_identity(identity)
    return ks, device_key


def _make_keystore_with_session(ks, subkey):
    session = Session(
        slug="alice-0001",
        subkey_id="sk_test123",
        subkey_secret_key=encode_secret(subkey.secret_bytes()),
        expires_at=_now_ms() + 3_600_000,
    )
    ks.save_session(session)
    return session


def _clear_proxy_env(monkeypatch):
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "SOCKS_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "socks_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(key, raising=False)


def test_remote_http_session_trusts_env_without_socks_proxy(monkeypatch):
    _clear_proxy_env(monkeypatch)

    async def run():
        session = create_remote_http_session("https://api.puffo.ai")
        try:
            assert getattr(session, "_trust_env", None) is True
            assert not isinstance(session.connector, ProxyConnector)
        finally:
            await session.close()

    asyncio.run(run())


def test_remote_http_session_uses_socks_connector(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:9")

    async def run():
        session = create_remote_http_session("https://api.puffo.ai")
        try:
            assert getattr(session, "_trust_env", None) is False
            assert isinstance(session.connector, ProxyConnector)
        finally:
            await session.close()

    asyncio.run(run())


def test_remote_http_session_uses_socks_proxy_env(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("SOCKS_PROXY", "socks5://127.0.0.1:9")

    async def run():
        session = create_remote_http_session("https://api.puffo.ai")
        try:
            assert getattr(session, "_trust_env", None) is False
            assert isinstance(session.connector, ProxyConnector)
        finally:
            await session.close()

    asyncio.run(run())


class TestHttpClientSigning(AioHTTPTestCase):
    """Tests that the HTTP client sends correct signed requests."""

    def setUp(self):
        self.ks, self.device_key = _make_keystore_with_identity()
        self.subkey = Ed25519KeyPair.generate()
        _make_keystore_with_session(self.ks, self.subkey)
        self.captured_headers = {}
        self.captured_body = b""
        super().setUp()

    async def get_application(self):
        app = web.Application()
        app.router.add_route("GET", "/health", self._handle)
        app.router.add_route("POST", "/messages", self._handle)
        app.router.add_route("PUT", "/update", self._handle)
        app.router.add_route("DELETE", "/remove", self._handle)
        return app

    async def _handle(self, request: web.Request):
        self.captured_headers = dict(request.headers)
        self.captured_body = await request.read()
        return web.json_response({"ok": True})

    @unittest_run_loop
    async def test_get_sends_auth_headers(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            result = await client.get("/health")
            assert result == {"ok": True}
            h = {k.lower(): v for k, v in self.captured_headers.items()}
            assert h["x-puffo-version"] == "v1"
            assert h["x-puffo-slug"] == "alice-0001"
            assert h["x-puffo-signer-id"] == "sk_test123"
            assert "x-puffo-timestamp" in h
            assert "x-puffo-nonce" in h
            assert "x-puffo-signature" in h
        finally:
            await client.close()

    @unittest_run_loop
    async def test_post_sends_body_and_auth(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            result = await client.post("/messages", {"text": "hello"})
            assert result == {"ok": True}
            body = json.loads(self.captured_body)
            assert body["text"] == "hello"
            h = {k.lower(): v for k, v in self.captured_headers.items()}
            assert "x-puffo-signature" in h
        finally:
            await client.close()

    @unittest_run_loop
    async def test_signature_is_verifiable(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            await client.post("/messages", {"text": "verify me"})
            h = {k.lower(): v for k, v in self.captured_headers.items()}
            method = "POST"
            path = "/messages"
            ts = h["x-puffo-timestamp"]
            nonce = h["x-puffo-nonce"]
            sig = base64url_decode(h["x-puffo-signature"])
            expected_msg = f"{method}\n{path}\n{ts}\n{nonce}\n".encode() + self.captured_body
            assert ed25519_verify(self.subkey.public_key_bytes(), expected_msg, sig)
        finally:
            await client.close()

    @unittest_run_loop
    async def test_put_and_delete(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            result = await client.put("/update", {"v": 1})
            assert result == {"ok": True}
            result = await client.delete("/remove")
            assert result == {"ok": True}
        finally:
            await client.close()


class TestHttpClientErrors(AioHTTPTestCase):
    """Tests error handling."""

    def setUp(self):
        self.ks, self.device_key = _make_keystore_with_identity()
        self.subkey = Ed25519KeyPair.generate()
        _make_keystore_with_session(self.ks, self.subkey)
        super().setUp()

    async def get_application(self):
        app = web.Application()
        app.router.add_route("GET", "/not-found", self._handle_404)
        app.router.add_route("GET", "/server-error", self._handle_500)
        app.router.add_route("GET", "/html-ok", self._handle_html_ok)
        app.router.add_route("GET", "/empty-ok", self._handle_empty_ok)
        return app

    async def _handle_404(self, request):
        return web.json_response({"error": "not found"}, status=404)

    async def _handle_500(self, request):
        return web.json_response({"error": "internal"}, status=500)

    async def _handle_html_ok(self, request):
        # A 2xx with a non-JSON body — the shape a proxy / CDN error
        # page or gateway interstitial takes when it slips past the
        # status check.
        return web.Response(
            text="<!doctype html><html><body>gateway error</body></html>",
            status=200,
            content_type="text/html",
        )

    async def _handle_empty_ok(self, request):
        # A legitimately empty 2xx (204 No Content) — must NOT raise.
        return web.Response(status=204)

    @unittest_run_loop
    async def test_404_raises_http_error(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            await client.get("/not-found")
            assert False, "should have raised"
        except HttpError as e:
            assert e.status == 404
        finally:
            await client.close()

    @unittest_run_loop
    async def test_500_raises_http_error(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            await client.get("/server-error")
            assert False, "should have raised"
        except HttpError as e:
            assert e.status == 500
        finally:
            await client.close()

    @unittest_run_loop
    async def test_non_json_2xx_raises_http_error(self):
        # A 2xx whose body isn't JSON must fail loud here — not three
        # layers up as `'str' object has no attribute 'get'`.
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            await client.get("/html-ok")
            assert False, "should have raised"
        except HttpError as e:
            assert e.status == 200
            assert "non-JSON body" in e.body
            assert "gateway error" in e.body
        finally:
            await client.close()

    @unittest_run_loop
    async def test_empty_2xx_does_not_raise(self):
        # An empty 2xx body (204 No Content etc.) is legitimate —
        # callers that ignore the result must keep working.
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            result = await client.get("/empty-ok")
            assert not result, f"expected falsy empty result, got {result!r}"
        finally:
            await client.close()


class TestHttpClientSubkeyRotation(AioHTTPTestCase):
    """Tests automatic subkey rotation on 401 and TTL expiry."""

    def setUp(self):
        self.ks, self.device_key = _make_keystore_with_identity()
        self.rotation_called = False
        self.attempt_count = 0
        super().setUp()

    async def get_application(self):
        app = web.Application()
        app.router.add_route("POST", "/devices/subkeys", self._handle_rotate)
        app.router.add_route("GET", "/data", self._handle_data)
        return app

    async def _handle_rotate(self, request: web.Request):
        self.rotation_called = True
        body = json.loads(await request.read())
        cert = body["subkey_cert"]
        return web.json_response({
            "ok": True,
            "subkey_id": cert["subkey_id"],
            "seq": 1,
        })

    async def _handle_data(self, request: web.Request):
        self.attempt_count += 1
        if self.attempt_count == 1 and not self.rotation_called:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"value": 42})

    @unittest_run_loop
    async def test_auto_rotation_on_no_session(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            result = await client.get("/data")
            assert result == {"value": 42}
            assert self.rotation_called
            sess = self.ks.load_session("alice-0001")
            assert sess.subkey_id.startswith("sk_")
        finally:
            await client.close()

    @unittest_run_loop
    async def test_retry_on_401(self):
        subkey = Ed25519KeyPair.generate()
        sess = Session(
            slug="alice-0001",
            subkey_id="sk_old",
            subkey_secret_key=encode_secret(subkey.secret_bytes()),
            expires_at=_now_ms() + 3_600_000,
        )
        self.ks.save_session(sess)
        self.attempt_count = 0

        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            result = await client.get("/data")
            assert result == {"value": 42}
            assert self.attempt_count == 2
            assert self.rotation_called
            new_sess = self.ks.load_session("alice-0001")
            assert new_sess.subkey_id != "sk_old"
        finally:
            await client.close()

    @unittest_run_loop
    async def test_rotation_on_near_expiry(self):
        subkey = Ed25519KeyPair.generate()
        sess = Session(
            slug="alice-0001",
            subkey_id="sk_expiring",
            subkey_secret_key=encode_secret(subkey.secret_bytes()),
            expires_at=_now_ms() + 60_000,  # 1 min left, within rotation margin
        )
        self.ks.save_session(sess)

        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            result = await client.get("/data")
            assert result == {"value": 42}
            assert self.rotation_called
            new_sess = self.ks.load_session("alice-0001")
            assert new_sess.subkey_id != "sk_expiring"
        finally:
            await client.close()


class TestHttpClientUnsigned(AioHTTPTestCase):
    """Tests unsigned request methods."""

    def setUp(self):
        self.ks, _ = _make_keystore_with_identity()
        super().setUp()

    async def get_application(self):
        app = web.Application()
        app.router.add_route("POST", "/signup", self._handle_signup)
        app.router.add_route("GET", "/invites/{code}/check", self._handle_check)
        return app

    async def _handle_signup(self, request: web.Request):
        body = json.loads(await request.read())
        return web.json_response({"slug": "alice-0001", "device_id": "dev_1"})

    async def _handle_check(self, request: web.Request):
        code = request.match_info["code"]
        return web.json_response({"code": code, "available": True})

    @unittest_run_loop
    async def test_post_unsigned(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            result = await client.post_unsigned("/signup", {"invite_code": "ABC"})
            assert result["slug"] == "alice-0001"
        finally:
            await client.close()

    @unittest_run_loop
    async def test_get_unsigned(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            result = await client.get_unsigned("/invites/ABC/check")
            assert result["available"] is True
        finally:
            await client.close()


class TestHttpClientKeylessEgress(AioHTTPTestCase):
    """T23 keyless transport: ``post_bytes_unsigned`` + the test-only
    egress ``x-sandbox-token`` shim on the three unsigned methods. An
    echo handler captures the headers + body the server received."""

    def setUp(self):
        self.ks, _ = _make_keystore_with_identity()
        self.subkey = Ed25519KeyPair.generate()
        _make_keystore_with_session(self.ks, self.subkey)
        self.captured_headers = {}
        self.captured_body = b""
        super().setUp()

    async def get_application(self):
        app = web.Application()
        app.router.add_route("POST", "/v2/cloud-agents/blobs/upload", self._echo)
        app.router.add_route("POST", "/v2/cloud-agents/messages", self._echo)
        app.router.add_route("GET", "/v2/cloud-agents/spaces", self._echo)
        # Signed routes — used to prove the shim never touches them.
        app.router.add_route("GET", "/health", self._echo)
        app.router.add_route("POST", "/messages", self._echo)
        return app

    async def _echo(self, request: web.Request):
        self.captured_headers = {k.lower(): v for k, v in request.headers.items()}
        self.captured_body = await request.read()
        return web.json_response({"ok": True, "blob_id": "b1"})

    @unittest_run_loop
    async def test_post_bytes_unsigned_sends_raw_bytes_no_signature(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001", keyless=True)
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PUFFO_LOCAL_SANDBOX_TOKEN", None)
                result = await client.post_bytes_unsigned(
                    "/v2/cloud-agents/blobs/upload", b"\x00\x01raw-bytes",
                )
            assert result["blob_id"] == "b1"
            # Raw body posted verbatim; octet-stream content type; unsigned.
            assert self.captured_body == b"\x00\x01raw-bytes"
            assert self.captured_headers.get("content-type", "").startswith(
                "application/octet-stream"
            )
            assert "x-puffo-signature" not in self.captured_headers
            assert "x-sandbox-token" not in self.captured_headers
        finally:
            await client.close()

    @unittest_run_loop
    async def test_egress_shim_adds_token_when_env_set(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001", keyless=True)
        try:
            with patch.dict(os.environ, {"PUFFO_LOCAL_SANDBOX_TOKEN": "tok-abc"}):
                await client.get_unsigned("/v2/cloud-agents/spaces")
                assert self.captured_headers.get("x-sandbox-token") == "tok-abc"
                await client.post_unsigned(
                    "/v2/cloud-agents/messages", {"plaintext": "hi"},
                )
                assert self.captured_headers.get("x-sandbox-token") == "tok-abc"
                await client.post_bytes_unsigned(
                    "/v2/cloud-agents/blobs/upload", b"blobbytes",
                )
                assert self.captured_headers.get("x-sandbox-token") == "tok-abc"
        finally:
            await client.close()

    @unittest_run_loop
    async def test_egress_shim_absent_when_env_unset(self):
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001", keyless=True)
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PUFFO_LOCAL_SANDBOX_TOKEN", None)
                await client.get_unsigned("/v2/cloud-agents/spaces")
                assert "x-sandbox-token" not in self.captured_headers
                await client.post_unsigned(
                    "/v2/cloud-agents/messages", {"plaintext": "hi"},
                )
                assert "x-sandbox-token" not in self.captured_headers
                await client.post_bytes_unsigned(
                    "/v2/cloud-agents/blobs/upload", b"x",
                )
                assert "x-sandbox-token" not in self.captured_headers
        finally:
            await client.close()

    @unittest_run_loop
    async def test_signed_methods_never_carry_sandbox_token(self):
        # Even with the env var set, the signed get/post must stay
        # untouched — the shim lives only on the unsigned methods.
        url = f"http://localhost:{self.server.port}"
        client = PuffoCoreHttpClient(url, self.ks, "alice-0001")
        try:
            with patch.dict(os.environ, {"PUFFO_LOCAL_SANDBOX_TOKEN": "tok-abc"}):
                await client.get("/health")
                assert "x-sandbox-token" not in self.captured_headers
                assert "x-puffo-signature" in self.captured_headers
                await client.post("/messages", {"x": 1})
                assert "x-sandbox-token" not in self.captured_headers
                assert "x-puffo-signature" in self.captured_headers
        finally:
            await client.close()
