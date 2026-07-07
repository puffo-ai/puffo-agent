"""T23 phase 2: keyless bridge message flow.

Under ``puffo_core.transport: "bridge"`` the message path runs over the
plaintext ``CloudBridgeClient`` — the server holds all crypto. This
suite pins the phase-2 seam swap:

  (a) inbound plaintext ``message`` frames persist + surface exactly
      like a native decrypted envelope, but WITHOUT ``decrypt_message``;
  (b) outbound ``send_message`` sends plaintext via ``bridge.send_send``
      WITHOUT ``encrypt_message*``;
  (c) a fresh connect drives ``send_fetch_pending`` exactly once and
      ``pending_delivered`` is recognised as backfill completion (the
      loop keeps running for live frames);
  (d) native (``bridge_client=None``) still encrypts on send and
      decrypts on receive — the extraction is behaviour-preserving;
  (e) attachments over the bridge fail loud (phase 3).

Every fake is offline: no real WS, HTTP, E2B, or LLM.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import puffo_agent.agent.puffo_core_client as pcc_mod
import puffo_agent.mcp.puffo_core_tools as pct_mod
from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.http_client import PuffoCoreHttpClient
from puffo_agent.crypto.keystore import (
    KeyStore,
    Session,
    StoredIdentity,
    encode_secret,
)
from puffo_agent.crypto.message import MessagePayload
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair
from puffo_agent.mcp.puffo_core_tools import PuffoCoreToolsConfig
from puffo_agent.portal.ws_local.tool_dispatch import build_dispatch


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeBridge:
    """Offline ``CloudBridgeClient`` stand-in for the phase-2 flow.

    ``frames()`` replays a scripted list then suspends (mimicking a live
    WS awaiting its next frame) so ``_listen_bridge`` stays connected
    instead of reconnect-storming — the test cancels the task when done.
    ``send_send`` records its kwargs and returns a canned ack;
    ``send_fetch_pending`` / ``connect`` / ``close`` count calls.
    """

    def __init__(self, scripted: list[dict] | None = None, ack: dict | None = None):
        self._scripted = list(scripted or [])
        self._ack = ack or {
            "type": "ack",
            "envelope_id": "msg_bridgeack",
            "client_ref": "r_test",
        }
        self.sent: list[dict] = []
        self.connect_count = 0
        self.fetch_pending_count = 0
        self.close_count = 0
        # Never set → frames() suspends after the script drains.
        self._blocked = asyncio.Event()

    async def connect(self) -> None:
        self.connect_count += 1

    async def send_fetch_pending(self, *, limit=None) -> None:
        self.fetch_pending_count += 1

    async def send_send(
        self, *, plaintext, recipient_slug=None, space_id=None,
        channel_id=None, timeout: float = 30.0,
    ) -> dict:
        self.sent.append({
            "plaintext": plaintext,
            "recipient_slug": recipient_slug,
            "space_id": space_id,
            "channel_id": channel_id,
        })
        return dict(self._ack)

    async def frames(self):
        for frame in self._scripted:
            yield frame
        await self._blocked.wait()  # suspend like a live WS
        yield {}  # pragma: no cover — keeps this an async generator

    async def close(self) -> None:
        self.close_count += 1


class FakeHttp:
    """Async HTTP stub. ``get`` matches on exact path, path-without-
    query, then query-modulo-``since`` (the ``/certs/sync`` cursor), so a
    test registers one canonical key. Everything else returns ``{}`` so
    the inbound enrichment helpers degrade offline rather than crash.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.responses: dict[str, dict] = {}

    def _match(self, path: str) -> dict:
        if path in self.responses:
            return self.responses[path]
        base = path.split("?", 1)[0]
        if base in self.responses:
            return self.responses[base]
        if "?" in path:
            from urllib.parse import parse_qsl
            actual = sorted(
                (k, v)
                for k, v in parse_qsl(path.split("?", 1)[1], keep_blank_values=True)
                if k != "since"
            )
            for key in self.responses:
                if "?" not in key:
                    continue
                key_base, key_qs = key.split("?", 1)
                if key_base != base:
                    continue
                if sorted(parse_qsl(key_qs, keep_blank_values=True)) == actual:
                    return self.responses[key]
        return {}

    async def get(self, path):
        self.calls.append(("GET", path, None))
        return self._match(path)

    async def post(self, path, body=None):
        self.calls.append(("POST", path, body))
        return self.responses.get(path, {"ok": True})


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _bridge_client(
    tmp_path, bridge, *, slug="bot-0001", db="messages.db",
) -> PuffoCoreMessageClient:
    """A bridge-transport message client with a stubbed (offline) http
    so inbound enrichment helpers degrade to ids-for-names."""
    ks = KeyStore(str(tmp_path / f"keys-{db}"))
    http = PuffoCoreHttpClient("http://127.0.0.1:1", ks, slug)
    store = MessageStore(str(tmp_path / db))
    client = PuffoCoreMessageClient(
        slug=slug,
        device_id="dev_test",
        space_id="sp_home",
        keystore=ks,
        http_client=http,
        message_store=store,
        bridge_client=bridge,
    )

    async def _empty_get(path, *a, **k):
        return {}

    client.http.get = _empty_get  # type: ignore[method-assign]
    return client


def _native_keystore(tmp_path, slug="bot-0001") -> KeyStore:
    ks = KeyStore(str(tmp_path / "native-keys"))
    ks.save_identity(StoredIdentity(
        slug=slug,
        device_id="dev_test",
        root_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
        device_signing_secret_key=encode_secret(
            Ed25519KeyPair.generate().secret_bytes()
        ),
        kem_secret_key=encode_secret(KemKeyPair.generate().secret_bytes()),
        server_url="http://127.0.0.1:1",
    ))
    ks.save_session(Session(
        slug=slug,
        subkey_id="sk_test",
        subkey_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
        expires_at=32_503_680_000_000,
    ))
    return ks


def _tools_cfg(tmp_path, *, bridge, data_client, http=None, slug="bot-0001"):
    ks = KeyStore(str(tmp_path / "cfg-keys"))
    return PuffoCoreToolsConfig(
        slug=slug,
        device_id="dev_test",
        keystore=ks,
        http_client=http or FakeHttp(),
        data_client=data_client,
        space_id="sp_home",
        workspace=str(tmp_path),
        bridge_client=bridge,
    )


async def _drive_listen_until(client, *, on_message, done: asyncio.Event, timeout=5.0):
    """Run ``_listen_bridge`` as a task until ``done`` fires, then cancel
    it cleanly. Returns nothing — assertions read the store / bridge."""
    task = asyncio.ensure_future(client._listen_bridge(on_message))
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    # The consumer sleeps random.uniform(0, 1.5) before dispatch; zero it
    # so batch-callback tests don't wait seconds.
    monkeypatch.setattr(pcc_mod.random, "uniform", lambda a, b: 0.0)


# --------------------------------------------------------------------------
# (a) inbound plaintext stores like native, no decrypt
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_inbound_stores_like_native_without_decrypt(tmp_path, monkeypatch):
    ENV_ID = "env_bridge_1"
    SENDER = "alice-0001"
    CHANNEL = "ch_xyz"
    SPACE = "sp_1"
    CONTENT = "hello from the bridge"
    SENT_AT = 1_700_000_000_000

    decrypt_calls: list[int] = []

    def _decrypt_spy(*a, **k):  # pragma: no cover — must never run here
        decrypt_calls.append(1)
        raise AssertionError("decrypt_message must not run on the bridge path")

    monkeypatch.setattr(pcc_mod, "decrypt_message", _decrypt_spy)

    frame = {
        "type": "message",
        "envelope_id": ENV_ID,
        "sender_slug": SENDER,
        "envelope_kind": "channel",
        "space_id": SPACE,
        "channel_id": CHANNEL,
        "sent_at": SENT_AT,
        "plaintext": CONTENT,
    }
    # The decrypted-equivalent of the same logical message — what the
    # native decrypt_message would yield. Feeding this straight through
    # the shared tail gives the reference store row to compare against.
    ref_payload = MessagePayload(
        payload_type="puffo.message",
        version=1,
        envelope_id=ENV_ID,
        envelope_kind="channel",
        sender_slug=SENDER,
        sender_subkey_id="",
        sent_at=SENT_AT,
        message_nonce="",
        content_type="text/plain",
        content=CONTENT,
        is_visible_to_human=True,
        space_id=SPACE,
        channel_id=CHANNEL,
    )

    # --- bridge path ---
    bridge = FakeBridge(scripted=[frame])
    client = _bridge_client(tmp_path, bridge, db="bridge.db")
    surfaced: list[tuple] = []
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        surfaced.append((root_id, batch, channel_meta))
        done.set()

    await _drive_listen_until(client, on_message=on_message, done=done)

    # --- native reference path (post-decrypt shared tail) ---
    ref = _bridge_client(tmp_path, FakeBridge(), db="native_ref.db")
    ref._queue = asyncio.PriorityQueue()
    ref._queue_seq = 0
    ref._thread_state = {}
    await ref.store.open()
    await ref._handle_plaintext_payload(ref_payload)

    bridge_row = await client.store.get_message_by_envelope(ENV_ID)
    ref_row = await ref.store.get_message_by_envelope(ENV_ID)
    assert bridge_row is not None, "bridge inbound frame was not persisted"
    assert ref_row is not None

    persisted_fields = (
        "envelope_id", "sender_slug", "channel_id", "space_id",
        "recipient_slug", "content_type", "content", "sent_at",
        "envelope_kind",
    )
    for f in persisted_fields:
        assert getattr(bridge_row, f) == getattr(ref_row, f), (
            f"bridge/native persisted field {f!r} diverged: "
            f"{getattr(bridge_row, f)!r} != {getattr(ref_row, f)!r}"
        )
    # Concrete spot-checks so the equivalence isn't vacuously true.
    assert bridge_row.content == CONTENT
    assert bridge_row.sender_slug == SENDER
    assert bridge_row.envelope_kind == "channel"
    assert bridge_row.content_type == "text/plain"

    # decrypt never ran, and the message surfaced to the agent.
    assert decrypt_calls == []
    assert len(surfaced) == 1
    root_id, batch, channel_meta = surfaced[0]
    assert any(m["envelope_id"] == ENV_ID for m in batch)
    assert channel_meta["channel_id"] == CHANNEL


@pytest.mark.asyncio
async def test_a_inbound_dm_frame_routes_as_dm(tmp_path):
    """A frame with ``recipient_slug`` and no explicit ``envelope_kind``
    is inferred as a DM (mapper fallback) and stashed for reply routing.
    """
    bridge = FakeBridge(scripted=[{
        "type": "message",
        "envelope_id": "env_dm_1",
        "sender_slug": "carol-0001",
        "recipient_slug": "bot-0001",
        "sent_at": 1_700_000_000_001,
        "plaintext": "ping",
    }])
    client = _bridge_client(tmp_path, bridge, db="dm.db")
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        done.set()

    await _drive_listen_until(client, on_message=on_message, done=done)

    row = await client.store.get_message_by_envelope("env_dm_1")
    assert row is not None
    assert row.envelope_kind == "dm"
    assert row.recipient_slug == "bot-0001"
    # DM sender stashed so send_fallback_message("") can reply to them.
    assert client._last_dm_sender == "carol-0001"


def test_payload_from_bridge_frame_skips_missing_envelope_id(tmp_path):
    client = _bridge_client(tmp_path, FakeBridge(), db="skip.db")
    assert client._payload_from_bridge_frame({"plaintext": "x"}) is None


# --------------------------------------------------------------------------
# (b) bridge send, no encrypt
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_send_message_dm_uses_bridge_no_encrypt(tmp_path, monkeypatch):
    enc_calls: list[int] = []
    monkeypatch.setattr(
        pct_mod, "encrypt_message_with_content_key",
        lambda *a, **k: enc_calls.append(1),
    )
    monkeypatch.setattr(
        pct_mod, "encrypt_message", lambda *a, **k: enc_calls.append(1),
    )

    ms = MessageStore(str(tmp_path / "b_dm.db"))
    bridge = FakeBridge()
    cfg = _tools_cfg(tmp_path, bridge=bridge, data_client=ms)
    tools = build_dispatch(cfg)

    result = await tools["send_message"](channel="@alice-0001", text="hi alice")

    assert len(bridge.sent) == 1
    sent = bridge.sent[0]
    assert sent["plaintext"] == "hi alice"
    assert sent["recipient_slug"] == "alice-0001"
    assert sent["space_id"] is None and sent["channel_id"] is None
    assert "msg_bridgeack" in result
    assert enc_calls == []


@pytest.mark.asyncio
async def test_b_send_message_channel_uses_bridge_no_encrypt(tmp_path, monkeypatch):
    enc_calls: list[int] = []
    monkeypatch.setattr(
        pct_mod, "encrypt_message_with_content_key",
        lambda *a, **k: enc_calls.append(1),
    )
    monkeypatch.setattr(
        pct_mod, "encrypt_message", lambda *a, **k: enc_calls.append(1),
    )

    ms = MessageStore(str(tmp_path / "b_ch.db"))
    await ms.mark_channel_space("ch_xyz", "sp_1")
    bridge = FakeBridge()
    cfg = _tools_cfg(tmp_path, bridge=bridge, data_client=ms)
    tools = build_dispatch(cfg)

    result = await tools["send_message"](channel="ch_xyz", text="team update")

    assert len(bridge.sent) == 1
    sent = bridge.sent[0]
    assert sent["plaintext"] == "team update"
    assert sent["space_id"] == "sp_1"
    assert sent["channel_id"] == "ch_xyz"
    assert sent["recipient_slug"] is None
    assert "msg_bridgeack" in result
    assert enc_calls == []


@pytest.mark.asyncio
async def test_b_send_message_threaded_note_on_bridge(tmp_path):
    """root_id isn't wired on bridge yet — send top-level with a note."""
    ms = MessageStore(str(tmp_path / "b_thread.db"))
    bridge = FakeBridge()
    cfg = _tools_cfg(tmp_path, bridge=bridge, data_client=ms)
    tools = build_dispatch(cfg)

    result = await tools["send_message"](
        channel="@alice-0001", text="reply", root_id="msg_parent",
    )
    assert len(bridge.sent) == 1
    assert "top-level" in result.lower() or "phase 3" in result.lower() \
        or "not wired" in result.lower()


# --------------------------------------------------------------------------
# (c) connect drives exactly one fetch_pending; pending_delivered ends backfill
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_connect_drives_one_fetch_pending_and_survives_pending_delivered(
    tmp_path, caplog,
):
    # backfill message, the terminator, then a live message — proving the
    # loop kept running for live delivery after pending_delivered.
    scripted = [
        {
            "type": "message", "envelope_id": "env_backfill",
            "sender_slug": "alice-0001", "envelope_kind": "channel",
            "space_id": "sp_1", "channel_id": "ch_a",
            "sent_at": 1_700_000_000_010, "plaintext": "backfilled",
        },
        {"type": "pending_delivered", "count": 1},
        {
            "type": "message", "envelope_id": "env_live",
            "sender_slug": "alice-0001", "envelope_kind": "channel",
            "space_id": "sp_1", "channel_id": "ch_a",
            "sent_at": 1_700_000_000_020, "plaintext": "live one",
        },
    ]
    bridge = FakeBridge(scripted=scripted)
    client = _bridge_client(tmp_path, bridge, db="c.db")

    seen_roots: list[str] = []
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        seen_roots.append(root_id)
        if len(seen_roots) >= 2:
            done.set()

    with caplog.at_level(logging.INFO, logger="puffo_agent.agent.puffo_core_client"):
        await _drive_listen_until(client, on_message=on_message, done=done)

    # Exactly one connect + one fetch_pending drove the cold-start drain.
    assert bridge.connect_count == 1
    assert bridge.fetch_pending_count == 1
    # pending_delivered was recognised as backfill completion.
    assert "backfill complete" in caplog.text
    # Both the backfill and the post-terminator live message landed.
    assert await client.store.get_message_by_envelope("env_backfill") is not None
    assert await client.store.get_message_by_envelope("env_live") is not None


@pytest.mark.asyncio
async def test_c_uncorrelated_error_frame_does_not_crash_loop(tmp_path, caplog):
    scripted = [
        {"type": "error", "code": "INTERNAL", "message": "transient blip"},
        {
            "type": "message", "envelope_id": "env_after_err",
            "sender_slug": "alice-0001", "envelope_kind": "channel",
            "space_id": "sp_1", "channel_id": "ch_a",
            "sent_at": 1_700_000_000_030, "plaintext": "still alive",
        },
    ]
    bridge = FakeBridge(scripted=scripted)
    client = _bridge_client(tmp_path, bridge, db="c_err.db")
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        done.set()

    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.puffo_core_client"):
        await _drive_listen_until(client, on_message=on_message, done=done)

    assert "bridge error frame" in caplog.text
    # The loop survived the error frame and delivered the next message.
    assert await client.store.get_message_by_envelope("env_after_err") is not None


# --------------------------------------------------------------------------
# (d) native untouched
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d_native_send_message_still_encrypts_and_posts(tmp_path, monkeypatch):
    enc_calls: list[int] = []

    def _enc_spy(inp, signing_key, *, now_ms=None):
        enc_calls.append(1)
        return ({"envelope_id": "msg_native", "type": "message_envelope"}, b"ck")

    monkeypatch.setattr(pct_mod, "encrypt_message_with_content_key", _enc_spy)

    ks = _native_keystore(tmp_path)
    http = FakeHttp()
    http.responses["/certs/sync?slugs=bot-0001,alice-0001"] = {
        "entries": [{
            "seq": 1,
            "kind": "device_cert",
            "slug": "alice-0001",
            "cert": {
                "device_id": "dev_a",
                "kem_public_key": base64url_encode(
                    KemKeyPair.generate().public_key_bytes()
                ),
            },
        }],
        "has_more": False,
    }
    ms = MessageStore(str(tmp_path / "d_send.db"))
    cfg = PuffoCoreToolsConfig(
        slug="bot-0001",
        device_id="dev_test",
        keystore=ks,
        http_client=http,
        data_client=ms,
        space_id="sp_home",
        bridge_client=None,  # native
    )
    tools = build_dispatch(cfg)

    result = await tools["send_message"](channel="@alice-0001", text="hi native")

    assert enc_calls, "native send_message must still encrypt"
    assert any(p == "/messages" for m, p, _ in http.calls if m == "POST"), (
        f"native send must POST via http_client; calls={http.calls}"
    )
    assert "posted" in result


class _RecordingWs:
    """PuffoCoreWsClient stand-in: captures ``on_message`` (the native
    ``handle_envelope`` closure) and no-ops ``run()`` so ``listen()``
    returns instead of blocking."""

    instances: list["_RecordingWs"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.on_message = None
        self.on_event = None
        _RecordingWs.instances.append(self)

    async def run(self) -> None:
        return


@pytest.mark.asyncio
async def test_d_native_inbound_still_decrypts_before_storing(tmp_path, monkeypatch):
    monkeypatch.setattr(_RecordingWs, "instances", [])
    monkeypatch.setattr(pcc_mod, "PuffoCoreWsClient", _RecordingWs)

    decrypt_calls: list[int] = []
    canned = MessagePayload(
        payload_type="puffo.message",
        version=1,
        envelope_id="env_native_in",
        envelope_kind="channel",
        sender_slug="alice-0001",
        sender_subkey_id="",
        sent_at=1_700_000_000_050,
        message_nonce="",
        content_type="text/plain",
        content="decrypted body",
        is_visible_to_human=True,
        space_id="sp_1",
        channel_id="ch_a",
    )

    def _decrypt_spy(*a, **k):
        decrypt_calls.append(1)
        return canned

    monkeypatch.setattr(pcc_mod, "decrypt_message", _decrypt_spy)

    ks = _native_keystore(tmp_path)
    http = PuffoCoreHttpClient("http://127.0.0.1:1", ks, "bot-0001")
    store = MessageStore(str(tmp_path / "d_in.db"))
    client = PuffoCoreMessageClient(
        slug="bot-0001",
        device_id="dev_test",
        space_id="sp_home",
        keystore=ks,
        http_client=http,
        message_store=store,
    )  # no bridge → native

    async def _empty_get(path, *a, **k):
        return {}

    client.http.get = _empty_get  # type: ignore[method-assign]

    async def _fake_signing_keys(slug):
        return [object()]  # one non-empty pubkey so the decrypt loop runs

    client._key_cache.get_signing_keys = _fake_signing_keys  # type: ignore[method-assign]

    async def on_message(*a):
        return

    # Native listen() wires handle_envelope onto the (recording) WS and
    # returns because run() is a no-op.
    await client.listen(on_message)
    assert len(_RecordingWs.instances) == 1
    handle_envelope = _RecordingWs.instances[0].on_message
    assert handle_envelope is not None

    await handle_envelope({
        "envelope_id": "env_native_in",
        "sender_slug": "alice-0001",
    })

    assert decrypt_calls, "native inbound must call decrypt_message"
    row = await client.store.get_message_by_envelope("env_native_in")
    assert row is not None
    assert row.content == "decrypted body"


# --------------------------------------------------------------------------
# (e) attachment guard
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e_attachments_refused_on_bridge_no_encrypt(tmp_path, monkeypatch):
    enc_calls: list[int] = []
    monkeypatch.setattr(
        pct_mod, "encrypt_message_with_content_key",
        lambda *a, **k: enc_calls.append(1),
    )

    ms = MessageStore(str(tmp_path / "e.db"))
    bridge = FakeBridge()
    cfg = _tools_cfg(tmp_path, bridge=bridge, data_client=ms)
    tools = build_dispatch(cfg)

    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(RuntimeError) as excinfo:
        await tools["send_message_with_attachments"](
            paths=["note.txt"], channel="@alice-0001", caption="see file",
        )
    msg = str(excinfo.value).lower()
    assert "bridge" in msg and "not supported" in msg
    assert enc_calls == []
    assert bridge.sent == []
