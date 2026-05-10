import asyncio
import json
import os
import sys
import tempfile
import time

import pytest
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.crypto.encoding import base64url_decode, base64url_encode
from puffo_agent.crypto.http_client import PuffoCoreHttpClient
from puffo_agent.crypto.keystore import KeyStore, Session, StoredIdentity, encode_secret
from puffo_agent.crypto.primitives import Ed25519KeyPair, ed25519_verify
from puffo_agent.crypto.ws_client import (
    CONNECT_TIMEOUT,
    INITIAL_BACKOFF,
    PuffoCoreWsClient,
    _http_to_ws,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_keystore():
    d = tempfile.mkdtemp()
    ks = KeyStore(os.path.join(d, "keys"))
    device_key = Ed25519KeyPair.generate()
    identity = StoredIdentity(
        slug="alice-0001",
        device_id="dev_test",
        root_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
        device_signing_secret_key=encode_secret(device_key.secret_bytes()),
        kem_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
        server_url="http://localhost:3000",
    )
    ks.save_identity(identity)
    subkey = Ed25519KeyPair.generate()
    session = Session(
        slug="alice-0001",
        subkey_id="sk_test",
        subkey_secret_key=encode_secret(subkey.secret_bytes()),
        expires_at=_now_ms() + 3_600_000,
    )
    ks.save_session(session)
    return ks, subkey


class TestHttpToWs:
    def test_http(self):
        assert _http_to_ws("http://localhost:3000") == "ws://localhost:3000"

    def test_https(self):
        assert _http_to_ws("https://example.com") == "wss://example.com"

    def test_already_ws(self):
        assert _http_to_ws("ws://localhost:3000") == "ws://localhost:3000"


class FakeWsServer:
    """Minimal WS server for testing handshake and message flows."""

    def __init__(self):
        self.received_frames: list[dict] = []
        self.subkey_pk: bytes | None = None
        self.port: int = 0
        self._server = None
        self._pending_messages: list[dict] = []
        self._push_after_connect: list[dict] = []
        self._reject_connect = False

    async def _handler(self, ws):
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        frame = json.loads(raw)
        self.received_frames.append(frame)

        if self._reject_connect:
            await ws.close()
            return

        if frame.get("type") == "connect":
            if self.subkey_pk:
                sig = base64url_decode(frame["signature"])
                msg = f"ws-connect\n{frame['slug']}\n{frame['subkey_id']}\n{frame['nonce']}\n{frame['timestamp']}".encode()
                if not ed25519_verify(self.subkey_pk, msg, sig):
                    await ws.close()
                    return

            await ws.send(json.dumps({
                "type": "connected",
                "session_id": "sess_test",
            }))

            for push in self._push_after_connect:
                await ws.send(json.dumps(push))

            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    self.received_frames.append(frame)
            except websockets.exceptions.ConnectionClosed:
                pass

    async def start(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()


class FakeHttpClient:
    """Stub for the catch-up code path."""

    def __init__(self):
        self.pending_messages: list[dict] = []
        self._ensure_subkey_called = False

    async def get(self, path: str):
        if path == "/messages/pending":
            return {"messages": self.pending_messages}
        return {}

    async def _ensure_subkey(self):
        self._ensure_subkey_called = True


@pytest.mark.asyncio
async def test_handshake_sends_correct_frame():
    ks, subkey = _make_keystore()
    server = FakeWsServer()
    server.subkey_pk = subkey.public_key_bytes()
    await server.start()

    http = FakeHttpClient()
    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{server.port}",
        ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{server.port}"

    task = asyncio.create_task(client.connect_once())
    await asyncio.sleep(0.3)
    client.stop()
    try:
        await asyncio.wait_for(task, timeout=2)
    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError, OSError):
        pass

    assert len(server.received_frames) >= 1
    connect = server.received_frames[0]
    assert connect["type"] == "connect"
    assert connect["slug"] == "alice-0001"
    assert connect["subkey_id"] == "sk_test"
    assert len(connect["nonce"]) == 22
    assert isinstance(connect["timestamp"], int)

    sig = base64url_decode(connect["signature"])
    msg = f"ws-connect\nalice-0001\nsk_test\n{connect['nonce']}\n{connect['timestamp']}".encode()
    assert ed25519_verify(subkey.public_key_bytes(), msg, sig)

    assert client.session_id == "sess_test"
    await server.stop()


@pytest.mark.asyncio
async def test_ping_pong():
    ks, subkey = _make_keystore()
    server = FakeWsServer()
    server._push_after_connect = [{"type": "ping"}]
    await server.start()

    http = FakeHttpClient()
    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{server.port}",
        ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{server.port}"

    task = asyncio.create_task(client.connect_once())
    await asyncio.sleep(0.5)
    client.stop()
    try:
        await asyncio.wait_for(task, timeout=2)
    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError, OSError):
        pass

    pong_frames = [f for f in server.received_frames if f.get("type") == "pong"]
    assert len(pong_frames) == 1
    await server.stop()


@pytest.mark.asyncio
async def test_message_callback_and_ack():
    ks, subkey = _make_keystore()
    server = FakeWsServer()
    test_envelope = {
        "type": "message_envelope",
        "envelope_id": "env_abc",
        "sender_slug": "bob-0001",
    }
    server._push_after_connect = [
        {"type": "message", "envelope": test_envelope}
    ]
    await server.start()

    received = []

    async def on_msg(envelope):
        received.append(envelope)

    http = FakeHttpClient()
    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{server.port}",
        ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{server.port}"
    client.on_message = on_msg

    task = asyncio.create_task(client.connect_once())
    await asyncio.sleep(0.5)
    client.stop()
    try:
        await asyncio.wait_for(task, timeout=2)
    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError, OSError):
        pass

    assert len(received) == 1
    assert received[0]["envelope_id"] == "env_abc"

    ack_frames = [f for f in server.received_frames if f.get("type") == "ack"]
    assert len(ack_frames) == 1
    assert ack_frames[0]["envelope_ids"] == ["env_abc"]
    await server.stop()


@pytest.mark.asyncio
async def test_catchup_on_connect():
    ks, subkey = _make_keystore()
    server = FakeWsServer()
    await server.start()

    received = []

    async def on_msg(envelope):
        received.append(envelope)

    http = FakeHttpClient()
    http.pending_messages = [
        {"seq": 1, "envelope": {"envelope_id": "env_pending1", "sender_slug": "bob"}},
        {"seq": 2, "envelope": {"envelope_id": "env_pending2", "sender_slug": "carol"}},
    ]

    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{server.port}",
        ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{server.port}"
    client.on_message = on_msg

    task = asyncio.create_task(client.connect_once())
    await asyncio.sleep(0.5)
    client.stop()
    try:
        await asyncio.wait_for(task, timeout=2)
    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError, OSError):
        pass

    assert len(received) == 2
    assert received[0]["envelope_id"] == "env_pending1"
    assert received[1]["envelope_id"] == "env_pending2"

    ack_frames = [f for f in server.received_frames if f.get("type") == "ack"]
    assert len(ack_frames) == 1
    assert sorted(ack_frames[0]["envelope_ids"]) == ["env_pending1", "env_pending2"]
    await server.stop()


@pytest.mark.asyncio
async def test_event_and_cert_handlers():
    ks, subkey = _make_keystore()
    server = FakeWsServer()
    server._push_after_connect = [
        {"type": "cert_update", "entry": {"seq": 5, "kind": "subkey_cert"}},
        {"type": "event", "scope": "sp_123", "event": {"action": "join"}},
    ]
    await server.start()

    cert_updates = []
    events = []

    async def on_cert(entry):
        cert_updates.append(entry)

    async def on_event(scope, event):
        events.append((scope, event))

    http = FakeHttpClient()
    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{server.port}",
        ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{server.port}"
    client.on_cert_update = on_cert
    client.on_event = on_event

    task = asyncio.create_task(client.connect_once())
    await asyncio.sleep(0.5)
    client.stop()
    try:
        await asyncio.wait_for(task, timeout=2)
    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError, OSError):
        pass

    assert len(cert_updates) == 1
    assert cert_updates[0]["kind"] == "subkey_cert"
    assert len(events) == 1
    assert events[0][0] == "sp_123"
    assert events[0][1]["action"] == "join"
    await server.stop()


@pytest.mark.asyncio
async def test_reconnect_on_disconnect():
    ks, subkey = _make_keystore()
    connect_count = 0

    async def handler(ws):
        nonlocal connect_count
        raw = await ws.recv()
        frame = json.loads(raw)
        if frame.get("type") == "connect":
            connect_count += 1
            await ws.send(json.dumps({"type": "connected", "session_id": f"sess_{connect_count}"}))
            if connect_count == 1:
                await ws.close()
                return
            try:
                async for _ in ws:
                    pass
            except websockets.exceptions.ConnectionClosed:
                pass

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    http = FakeHttpClient()
    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{port}",
        ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{port}"

    task = asyncio.create_task(client.run())
    await asyncio.sleep(2.5)
    client.stop()
    try:
        await asyncio.wait_for(task, timeout=2)
    except asyncio.CancelledError:
        pass

    assert connect_count >= 2
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_nonce_unique_per_connect():
    ks, subkey = _make_keystore()
    nonces = []

    async def handler(ws):
        raw = await ws.recv()
        frame = json.loads(raw)
        nonces.append(frame.get("nonce"))
        await ws.send(json.dumps({"type": "connected", "session_id": "sess"}))
        await ws.close()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    http = FakeHttpClient()
    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{port}",
        ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{port}"

    task = asyncio.create_task(client.run())
    await asyncio.sleep(3)
    client.stop()
    try:
        await asyncio.wait_for(task, timeout=2)
    except asyncio.CancelledError:
        pass

    assert len(nonces) >= 2
    assert len(set(nonces)) == len(nonces), "nonces must be unique"
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_callback_exception_does_not_kill_loop():
    ks, subkey = _make_keystore()
    server = FakeWsServer()
    server._push_after_connect = [
        {"type": "message", "envelope": {"envelope_id": "env_fail", "sender_slug": "bob"}},
        {"type": "message", "envelope": {"envelope_id": "env_ok", "sender_slug": "carol"}},
    ]
    await server.start()

    call_count = 0

    async def on_msg(envelope):
        nonlocal call_count
        call_count += 1
        if envelope["envelope_id"] == "env_fail":
            raise RuntimeError("simulated callback failure")

    http = FakeHttpClient()
    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{server.port}",
        ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{server.port}"
    client.on_message = on_msg

    task = asyncio.create_task(client.connect_once())
    await asyncio.sleep(0.5)
    client.stop()
    try:
        await asyncio.wait_for(task, timeout=2)
    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError, OSError):
        pass

    assert call_count == 2, "both messages should be processed despite first callback failing"

    ack_frames = [f for f in server.received_frames if f.get("type") == "ack"]
    acked_ids = []
    for a in ack_frames:
        acked_ids.extend(a["envelope_ids"])
    assert "env_fail" in acked_ids, "failed message should still be ACKed"
    assert "env_ok" in acked_ids, "second message should be ACKed"
    await server.stop()
