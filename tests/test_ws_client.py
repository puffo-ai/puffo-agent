import asyncio
import json
import logging
import os
import sys
import tempfile
import time

import pytest
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.crypto.encoding import base64url_decode
from puffo_agent.crypto.keystore import KeyStore, Session, StoredIdentity, encode_secret
from puffo_agent.crypto.primitives import Ed25519KeyPair, ed25519_verify
from puffo_agent.crypto.ws_client import (
    INITIAL_BACKOFF,
    DeliveryResult,
    PuffoCoreWsClient,
    TransportOutcome,
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


@pytest.mark.asyncio
async def test_wss_connect_uses_remote_tls_context(monkeypatch):
    ks, _ = _make_keystore()
    client = PuffoCoreWsClient("https://api.puffo.ai", ks, "alice-0001", FakeHttpClient())
    tls_context = object()
    captured = {}

    class Connection:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    def connect(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Connection()

    async def handshake(_ws):
        return "session"

    async def noop():
        return None

    monkeypatch.setattr("puffo_agent.crypto.ws_client.create_remote_ssl_context", lambda: tls_context)
    monkeypatch.setattr("puffo_agent.crypto.ws_client.websockets.connect", connect)
    client._handshake = handshake
    client._catchup = noop
    client._listen_loop = noop

    await client.connect_once()

    assert captured == {"url": "wss://api.puffo.ai/subscribe", "ssl": tls_context}


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
        self.ack_posts: list[list[str]] = []
        self._ensure_subkey_called = False

    async def get(self, path: str):
        if path == "/messages/pending":
            return {"messages": self.pending_messages}
        return {}

    async def post(self, path: str, body: dict):
        if path == "/messages/ack":
            self.ack_posts.append(list(body.get("envelope_ids") or []))
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
@pytest.mark.parametrize(
    "response",
    [
        {
            "type": "rejected",
            "message": "MESSAGE_PLAINTEXT_SENTINEL",
            "raw_frame": "RAW_PROVIDER_FRAME_SENTINEL",
            "ciphertext": "CIPHERTEXT_SENTINEL",
            "reasoning": "REASONING_SENTINEL",
            "tool_arguments": "TOOL_ARGUMENT_SENTINEL",
            "tool_result": "TOOL_RESULT_SENTINEL",
            "credential": "CREDENTIAL_SENTINEL",
        },
        {
            "type": "connected",
            "session_id": "INVALID SESSION CREDENTIAL_SENTINEL",
        },
    ],
)
async def test_handshake_rejects_adversarial_content_without_echo(
    response, caplog,
):
    class Ws:
        async def send(self, _frame):
            pass

        async def recv(self):
            return json.dumps(response)

    ks, _ = _make_keystore()
    client = PuffoCoreWsClient(
        "http://localhost:3000", ks, "alice-0001", FakeHttpClient()
    )
    with pytest.raises(
        ConnectionError, match="^websocket handshake protocol error$"
    ) as error:
        await client._handshake(Ws())
    rendered = str(error.value) + caplog.text
    for sentinel in (
        "MESSAGE_PLAINTEXT_SENTINEL",
        "RAW_PROVIDER_FRAME_SENTINEL",
        "CIPHERTEXT_SENTINEL",
        "REASONING_SENTINEL",
        "TOOL_ARGUMENT_SENTINEL",
        "TOOL_RESULT_SENTINEL",
        "CREDENTIAL_SENTINEL",
    ):
        assert sentinel not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_id",
    [
        "550e8400-e29b-41d4-a716-446655440000",
        "sess_test",
        "session-ABC_123",
    ],
)
async def test_handshake_accepts_server_compatible_session_ids(session_id):
    class Ws:
        async def send(self, _frame):
            pass

        async def recv(self):
            return json.dumps({"type": "connected", "session_id": session_id})

    ks, _ = _make_keystore()
    client = PuffoCoreWsClient(
        "http://localhost:3000", ks, "alice-0001", FakeHttpClient()
    )
    assert await client._handshake(Ws()) == session_id


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
        {"type": "message", "seq": 7, "envelope": test_envelope}
    ]
    await server.start()

    received = []

    async def on_msg(delivery):
        received.append(delivery)
        return TransportOutcome.ACK

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
    assert received[0] == {"seq": 7, "envelope": test_envelope}

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

    async def on_msg(delivery):
        received.append(delivery)
        return TransportOutcome.ACK

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
    assert received[0]["envelope"]["envelope_id"] == "env_pending1"
    assert received[1]["envelope"]["envelope_id"] == "env_pending2"

    assert len(http.ack_posts) == 1
    assert sorted(http.ack_posts[0]) == ["env_pending1", "env_pending2"]
    await server.stop()


@pytest.mark.asyncio
async def test_held_recovery_uses_signed_pending_path_until_exact_envelope():
    ks, _ = _make_keystore()
    http = FakeHttpClient()
    http.pending_messages = [
        {"seq": 1, "envelope": {"envelope_id": "before", "sender_slug": "bob"}},
        {"seq": 2, "envelope": {"envelope_id": "required", "sender_slug": "bob"}},
        {"seq": 3, "envelope": {"envelope_id": "after", "sender_slug": "bob"}},
    ]
    received = []

    async def on_msg(delivery):
        received.append(delivery["envelope"]["envelope_id"])
        return TransportOutcome.ACK

    client = PuffoCoreWsClient(
        "http://localhost", ks, "alice-0001", http,
    )
    client.on_message = on_msg
    assert await client.recover_pending_until("required")
    assert received == ["before", "required"]
    assert http.ack_posts == [["before", "required"]]


@pytest.mark.asyncio
async def test_held_recovery_does_not_skip_a_held_delivery_to_reach_watermark():
    ks, _ = _make_keystore()
    http = FakeHttpClient()
    http.pending_messages = [
        {"seq": 1, "envelope": {"envelope_id": "blocked", "sender_slug": "bob"}},
        {"seq": 2, "envelope": {"envelope_id": "required", "sender_slug": "bob"}},
    ]
    received = []

    async def on_msg(delivery):
        envelope_id = delivery["envelope"]["envelope_id"]
        received.append(envelope_id)
        return (
            TransportOutcome.HOLD
            if envelope_id == "blocked"
            else TransportOutcome.ACK
        )

    client = PuffoCoreWsClient(
        "http://localhost", ks, "alice-0001", http,
    )
    client.on_message = on_msg
    assert not await client.recover_pending_until("required")
    assert received == ["blocked"]
    assert http.ack_posts == []


@pytest.mark.asyncio
async def test_signed_pending_defer_allows_later_required_delivery():
    ks, _ = _make_keystore()
    http = FakeHttpClient()
    http.pending_messages = [
        {"seq": 1, "envelope": {"envelope_id": "gated-dm", "sender_slug": "bob"}},
        {"seq": 2, "envelope": {"envelope_id": "required", "sender_slug": "operator"}},
    ]
    received = []

    async def on_msg(delivery):
        envelope_id = delivery["envelope"]["envelope_id"]
        received.append(envelope_id)
        return (
            TransportOutcome.DEFER
            if envelope_id == "gated-dm"
            else TransportOutcome.ACK
        )

    client = PuffoCoreWsClient(
        "http://localhost", ks, "alice-0001", http,
    )
    client.on_message = on_msg

    assert await client.recover_pending_until("required")
    assert received == ["gated-dm", "required"]
    assert http.ack_posts == [["required"]]


@pytest.mark.asyncio
async def test_event_and_cert_handlers():
    ks, subkey = _make_keystore()
    server = FakeWsServer()
    server._push_after_connect = [
        {"type": "cert_update", "entry": {"seq": 5, "kind": "subkey_cert"}},
        {"type": "event", "scope": "sp_123", "event": {"action": "join"}},
        {"type": "space_membership_changed", "space_id": "sp_123"},
    ]
    await server.start()

    cert_updates = []
    events = []
    membership_changes = []

    async def on_cert(entry):
        cert_updates.append(entry)

    async def on_event(scope, event):
        events.append((scope, event))

    async def on_membership_change(space_id):
        membership_changes.append(space_id)

    http = FakeHttpClient()
    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{server.port}",
        ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{server.port}"
    client.on_cert_update = on_cert
    client.on_event = on_event
    client.on_space_membership_changed = on_membership_change

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
    assert membership_changes == ["sp_123"]
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
@pytest.mark.parametrize(
    ("error_type", "category"),
    [
        (ConnectionError, "protocol"),
        (TimeoutError, "timeout"),
        (OSError, "transport"),
        (RuntimeError, "unexpected"),
    ],
    ids=["expected-protocol", "expected-timeout", "expected-transport", "catch-all"],
)
async def test_reconnect_diagnostics_are_type_derived_and_content_free(
    monkeypatch, caplog, error_type, category,
):
    import logging as _stdlib_logging

    sentinel = f"{category.upper()}_EXCEPTION_TEXT_SENTINEL"
    ks, _ = _make_keystore()
    client = PuffoCoreWsClient(
        "http://localhost:3000", ks, "alice-0001", FakeHttpClient()
    )
    attempts = 0
    delays = []

    async def connect_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error_type(sentinel)
        client.stop()

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(client, "connect_once", connect_once)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with caplog.at_level(
        _stdlib_logging.WARNING, logger="puffo_agent.crypto.ws_client"
    ):
        await client.run()

    assert attempts == 2
    assert delays == [INITIAL_BACKOFF]
    assert f"category={category}" in caplog.text
    assert f"exception={error_type.__name__}" in caplog.text
    assert "alice-0001" in caplog.text
    assert "retry_delay=1s" in caplog.text
    assert sentinel not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    categories = {
        record.getMessage().split("category=", 1)[1].split()[0]
        for record in caplog.records
        if "category=" in record.getMessage()
    }
    assert categories <= {
        "connection_closed", "timeout", "protocol", "transport", "unexpected",
    }


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
async def test_catchup_logs_zero_pending_count(caplog):
    """The catch-up INFO log must fire on every reconnect, including
    ``N=0`` — a future tidy-up that re-suppresses the zero line must
    trip this test.
    """
    import logging as _stdlib_logging

    ks, _subkey = _make_keystore()
    server = FakeWsServer()
    await server.start()

    http = FakeHttpClient()
    http.pending_messages = []  # explicit zero

    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{server.port}",
        ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{server.port}"

    with caplog.at_level(_stdlib_logging.INFO, logger="puffo_agent.crypto.ws_client"):
        task = asyncio.create_task(client.connect_once())
        await asyncio.sleep(0.5)
        client.stop()
        try:
            await asyncio.wait_for(task, timeout=2)
        except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError, OSError):
            pass

    catchup_lines = [r for r in caplog.records if "Catch-up:" in r.getMessage()]
    assert len(catchup_lines) == 1, (
        f"expected exactly one Catch-up log line on reconnect with N=0; got {len(catchup_lines)}"
    )
    assert "0 pending messages" in catchup_lines[0].getMessage()
    assert "session=sess_test" in catchup_lines[0].getMessage()
    await server.stop()


@pytest.mark.asyncio
async def test_callback_exception_stops_before_later_delivery():
    ks, subkey = _make_keystore()
    server = FakeWsServer()
    server._push_after_connect = [
        {"type": "message", "seq": 1, "envelope": {"envelope_id": "env_fail", "sender_slug": "bob"}},
        {"type": "message", "seq": 2, "envelope": {"envelope_id": "env_ok", "sender_slug": "carol"}},
    ]
    await server.start()

    call_count = 0

    async def on_msg(delivery):
        nonlocal call_count
        call_count += 1
        if delivery["envelope"]["envelope_id"] == "env_fail":
            raise RuntimeError("simulated callback failure")
        return TransportOutcome.ACK

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

    assert call_count == 1

    ack_frames = [f for f in server.received_frames if f.get("type") == "ack"]
    acked_ids = []
    for a in ack_frames:
        acked_ids.extend(a["envelope_ids"])
    assert "env_fail" not in acked_ids, "failed classification must remain unACKed"
    assert "env_ok" not in acked_ids, "later messages cannot cross an unACKed gap"
    await server.stop()


@pytest.mark.asyncio
async def test_on_connect_failure_does_not_kill_the_loop():
    """An on_connect callback that throws must not tear down the
    connection — catch-up + listen still run."""
    ks, _ = _make_keystore()
    server = FakeWsServer()
    await server.start()

    received = []

    async def on_msg(delivery):
        received.append(delivery)
        return TransportOutcome.ACK

    async def boom():
        raise RuntimeError("warm blew up")

    http = FakeHttpClient()
    http.pending_messages = [
        {"seq": 1, "envelope": {"envelope_id": "env_p1", "sender_slug": "bob"}},
    ]

    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{server.port}", ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{server.port}"
    client.on_message = on_msg
    client.on_connect = boom  # raises — must be swallowed, not kill the WS

    task = asyncio.create_task(client.connect_once())
    await asyncio.sleep(0.5)
    client.stop()
    try:
        await asyncio.wait_for(task, timeout=2)
    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError, OSError):
        pass

    # Catch-up still delivered the pending message → the on_connect
    # exception was caught, not propagated out of connect_once.
    assert [e["envelope"]["envelope_id"] for e in received] == ["env_p1"]
    await server.stop()


@pytest.mark.asyncio
async def test_catchup_acks_in_chunks():
    """A big backlog must ack incrementally — one end-of-loop ack loses
    all progress when the connection dies mid-catch-up and the whole
    batch redelivers forever."""
    ks, _ = _make_keystore()
    server = FakeWsServer()
    await server.start()

    http = FakeHttpClient()
    http.pending_messages = [
        {"seq": i, "envelope": {"envelope_id": f"env_{i}", "sender_slug": "bob"}}
        for i in range(1, 61)
    ] + [
        {"seq": 100 + i, "envelope": {"envelope_id": f"held_{i}", "sender_slug": "bob"}}
        for i in range(3)
    ]

    client = PuffoCoreWsClient(
        f"http://127.0.0.1:{server.port}", ks, "alice-0001", http,
    )
    client.ws_url = f"ws://127.0.0.1:{server.port}"

    async def on_msg(delivery):
        if delivery["envelope"]["envelope_id"].startswith("held_"):
            return TransportOutcome.HOLD
        return TransportOutcome.ACK

    client.on_message = on_msg
    task = asyncio.create_task(client.connect_once())
    await asyncio.sleep(0.8)
    client.stop()
    try:
        await asyncio.wait_for(task, timeout=2)
    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError, OSError):
        pass

    # 60 messages at 25/chunk → 25 + 25 + 10, over HTTP (immune to a
    # WS death mid-catch-up).
    assert [len(c) for c in http.ack_posts] == [25, 25, 10]
    acked = [e for c in http.ack_posts for e in c]
    assert acked == [f"env_{i}" for i in range(1, 61)]
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        {},
        {"seq": True, "envelope": {"envelope_id": "env"}},
        {"seq": "1", "envelope": {"envelope_id": "env"}},
        {"seq": 0, "envelope": {"envelope_id": "env"}},
        {"seq": -1, "envelope": {"envelope_id": "env"}},
        {"seq": 1, "envelope": None},
        {"seq": 1, "envelope": {}},
        {"seq": 1, "envelope": {"envelope_id": ""}},
    ],
)
async def test_invalid_seq_or_envelope_holds_without_callback(item):
    ks, _ = _make_keystore()
    client = PuffoCoreWsClient("http://localhost", ks, "alice-0001", FakeHttpClient())
    calls = []

    async def handler(delivery):
        calls.append(delivery)
        return TransportOutcome.ACK

    client.on_message = handler
    result = await client.dispatch_delivery(item)
    assert result.outcome is TransportOutcome.HOLD
    assert calls == []


@pytest.mark.asyncio
async def test_ack_waits_for_explicit_post_commit_outcome_and_later_delivery_continues():
    ks, _ = _make_keystore()
    client = PuffoCoreWsClient("http://localhost", ks, "alice-0001", FakeHttpClient())

    class CaptureWs:
        def __init__(self):
            self.frames = []

        async def send(self, raw):
            self.frames.append(json.loads(raw))

    ws = CaptureWs()
    client._ws = ws
    committed = asyncio.Event()

    async def handler(delivery):
        if delivery["seq"] == 1:
            await committed.wait()
        return TransportOutcome.ACK

    client.on_message = handler
    first = asyncio.create_task(client._handle_frame(json.dumps({
        "type": "message",
        "seq": 1,
        "envelope": {"envelope_id": "env_1"},
    })))
    await asyncio.sleep(0)
    assert ws.frames == []
    committed.set()
    await first
    await client._handle_frame(json.dumps({
        "type": "message",
        "seq": 2,
        "envelope": {"envelope_id": "env_2"},
    }))
    assert ws.frames == [
        {"type": "ack", "envelope_ids": ["env_1"]},
        {"type": "ack", "envelope_ids": ["env_2"]},
    ]


@pytest.mark.asyncio
async def test_live_hold_stops_before_later_delivery_can_be_acked():
    ks, _ = _make_keystore()
    client = PuffoCoreWsClient("http://localhost", ks, "alice-0001", FakeHttpClient())
    handled = []

    class SequenceWs:
        def __init__(self):
            self.frames = []
            self.items = iter([
                json.dumps({
                    "type": "message",
                    "seq": 1,
                    "envelope": {"envelope_id": "env_1"},
                }),
                json.dumps({
                    "type": "message",
                    "seq": 2,
                    "envelope": {"envelope_id": "env_2"},
                }),
            ])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.items)
            except StopIteration:
                raise StopAsyncIteration

        async def send(self, raw):
            self.frames.append(json.loads(raw))

    async def handler(delivery):
        handled.append(delivery["seq"])
        return (
            TransportOutcome.HOLD
            if delivery["seq"] == 1
            else TransportOutcome.ACK
        )

    ws = SequenceWs()
    client._ws = ws
    client.on_message = handler

    with pytest.raises(ConnectionError, match="unacknowledged"):
        await client._listen_loop()

    assert handled == [1]
    assert ws.frames == []


@pytest.mark.asyncio
async def test_live_defer_allows_later_delivery_to_be_acked():
    ks, _ = _make_keystore()
    client = PuffoCoreWsClient("http://localhost", ks, "alice-0001", FakeHttpClient())
    handled = []

    class CaptureWs:
        def __init__(self):
            self.frames = []

        async def send(self, raw):
            self.frames.append(json.loads(raw))

    async def handler(delivery):
        handled.append(delivery["seq"])
        return (
            TransportOutcome.DEFER
            if delivery["seq"] == 1
            else TransportOutcome.ACK
        )

    ws = CaptureWs()
    client._ws = ws
    client.on_message = handler

    await client._handle_frame(json.dumps({
        "type": "message",
        "seq": 1,
        "envelope": {"envelope_id": "gated-dm"},
    }))
    await client._handle_frame(json.dumps({
        "type": "message",
        "seq": 2,
        "envelope": {"envelope_id": "operator-approval"},
    }))

    assert handled == [1, 2]
    assert ws.frames == [
        {"type": "ack", "envelope_ids": ["operator-approval"]},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [TransportOutcome.HOLD, None, False, "ack"])
async def test_unexpected_or_hold_callback_result_emits_no_ack(returned):
    ks, _ = _make_keystore()
    client = PuffoCoreWsClient("http://localhost", ks, "alice-0001", FakeHttpClient())

    async def handler(_delivery):
        return returned

    client.on_message = handler
    result = await client.dispatch_delivery({
        "seq": 1,
        "envelope": {"envelope_id": "env"},
    })
    assert isinstance(result, DeliveryResult)
    assert result.outcome is TransportOutcome.HOLD


@pytest.mark.asyncio
async def test_catchup_holds_one_delivery_without_wedging_the_rest(caplog):
    """One unownable envelope must not wedge every message behind it.

    ``_run_catchup`` used to break at the first non-ACK/non-DEFER outcome
    and report failure, and ``_catchup`` turned that into a
    ``ConnectionError`` — raised *before* ``_listen_loop``. So a single
    permanently unopenable envelope (an undecryptable payload, an
    unreadable blocklist) reconnected, failed the same way, and never
    reached live listen again: not one message lost, all of them.

    Acks on this path carry explicit envelope ids, so leaving one unacked
    proves nothing about the ones after it — the server simply redelivers
    it. What the hold must not do is disappear silently, hence the record.
    """
    ks, _ = _make_keystore()
    http = FakeHttpClient()
    http.pending_messages = [
        {"seq": 1, "envelope": {"envelope_id": "unopenable", "sender_slug": "bob"}},
        {"seq": 2, "envelope": {"envelope_id": "later", "sender_slug": "carol"}},
    ]
    received = []

    async def on_msg(delivery):
        envelope_id = delivery["envelope"]["envelope_id"]
        received.append(envelope_id)
        return (
            TransportOutcome.HOLD
            if envelope_id == "unopenable"
            else TransportOutcome.ACK
        )

    client = PuffoCoreWsClient("http://localhost", ks, "alice-0001", http)
    client.on_message = on_msg

    with caplog.at_level(logging.WARNING):
        # The connection-drain entry point: it must not raise, or the
        # listen loop it guards is never reached.
        await client._catchup()

    assert received == ["unopenable", "later"]
    # The held envelope is not acked; the one behind it is.
    assert http.ack_posts == [["later"]]
    assert any(
        "unopenable" in record.getMessage() for record in caplog.records
    ), "the hold must leave an auditable record"
