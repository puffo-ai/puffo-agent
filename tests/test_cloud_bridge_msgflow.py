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
  (e) attachments over the bridge upload PLAINTEXT bytes keyless
      (``upload_blob``) + ref them by ``blob_id`` in the ``send`` frame
      on the way out, and download by ``blob_id`` (``download_blob``,
      no decrypt) into the inbox on the way in — with native attachment
      send still encrypting + using signed HTTP.

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
    ``send_send`` records its kwargs (including ``attachments``) and
    returns a canned ack; ``send_fetch_pending`` / ``connect`` / ``close``
    count calls. ``upload_blob`` mints a fresh ``blob_id`` per call and
    records the raw bytes; ``download_blob`` serves from a ``blob_id`` →
    bytes map (``None`` for an unknown id, mimicking a missing/oversized
    blob) and records every id requested.
    """

    def __init__(
        self,
        scripted: list[dict] | None = None,
        ack: dict | None = None,
        blobs: dict[str, bytes] | None = None,
    ):
        self._scripted = list(scripted or [])
        self._ack = ack or {
            "type": "ack",
            "envelope_id": "msg_bridgeack",
            "client_ref": "r_test",
        }
        self.sent: list[dict] = []
        # F1: every send_ack call records its envelope_ids, so a test can
        # assert exactly one ack per handled bridge message.
        self.acked: list[list[str]] = []
        self.connect_count = 0
        self.fetch_pending_count = 0
        self.close_count = 0
        # Keyless blob surface.
        self.uploaded: list[bytes] = []
        self.downloaded: list[str] = []
        self._blobs: dict[str, bytes] = dict(blobs or {})
        self._upload_seq = 0
        # Never set → frames() suspends after the script drains.
        self._blocked = asyncio.Event()

    async def connect(self) -> None:
        self.connect_count += 1

    async def send_fetch_pending(self, *, limit=None) -> None:
        self.fetch_pending_count += 1

    async def upload_blob(self, data: bytes) -> dict:
        self._upload_seq += 1
        blob_id = f"blob_{self._upload_seq:04d}"
        self.uploaded.append(data)
        self._blobs[blob_id] = data
        return {"blob_id": blob_id, "size_bytes": len(data), "uploaded_at": 0}

    async def download_blob(self, blob_id: str):
        self.downloaded.append(blob_id)
        return self._blobs.get(blob_id)

    async def send_ack(
        self, envelope_ids, *, timeout: float = 30.0,
    ) -> dict:
        self.acked.append(list(envelope_ids))
        return {"type": "ack_result", "acked": list(envelope_ids)}

    async def send_send(
        self, *, plaintext, recipient_slug=None, space_id=None,
        channel_id=None, reply_to_id=None, thread_root_id=None,
        attachments=None, timeout: float = 30.0,
    ) -> dict:
        self.sent.append({
            "plaintext": plaintext,
            "recipient_slug": recipient_slug,
            "space_id": space_id,
            "channel_id": channel_id,
            "reply_to_id": reply_to_id,
            "thread_root_id": thread_root_id,
            "attachments": attachments,
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
        # T23 keyless surface. Off by default (native/inbound tests keep
        # the signed path); ``_tools_cfg`` flips it on for the send-tool
        # tests so they exercise the unsigned ``/v2/cloud-agents/*`` seam.
        self.keyless = False
        self.server_url = "http://sandbox.local"
        self.uploaded: list[bytes] = []
        self._blob_seq = 0

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

    async def post_bytes(self, path, body=None):
        # Native attachment path uploads ciphertext here; return a
        # canned blob_id unless a test registers its own response.
        self.calls.append(("POST_BYTES", path, body))
        return self.responses.get(path, {"blob_id": "blob_native_1"})

    # ── keyless (T23) unsigned methods ──────────────────────────────

    async def get_unsigned(self, path):
        self.calls.append(("GET_UNSIGNED", path, None))
        return self._match(path)

    async def post_unsigned(self, path, body=None):
        self.calls.append(("POST_UNSIGNED", path, body))
        if path in self.responses:
            return self.responses[path]
        return {"envelope_id": "msg_bridgeack"}

    async def post_bytes_unsigned(self, path, body):
        self._blob_seq += 1
        self.uploaded.append(body)
        self.calls.append(
            ("POST_BYTES_UNSIGNED", path, len(body) if body else 0)
        )
        return {
            "blob_id": f"blob_{self._blob_seq:04d}",
            "size_bytes": len(body) if body else 0,
        }


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _bridge_client(
    tmp_path, bridge, *, slug="bot-0001", db="messages.db", workspace=None,
) -> PuffoCoreMessageClient:
    """A bridge-transport message client with a stubbed (offline) http
    so inbound enrichment helpers degrade to ids-for-names. Pass
    ``workspace`` when the test exercises inbound attachment saving
    (the saver no-ops on an empty workspace)."""
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
        workspace=workspace or "",
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
    http = http or FakeHttp()
    # T23: outbound send tools run keyless over ``/v2/cloud-agents/*``.
    # The ``bridge`` stays on the cfg (inbound + lifecycle) but must NOT
    # be touched by send — the tests assert ``bridge.sent == []``.
    http.keyless = True
    return PuffoCoreToolsConfig(
        slug=slug,
        device_id="dev_test",
        keystore=ks,
        http_client=http,
        data_client=data_client,
        space_id="sp_home",
        workspace=str(tmp_path),
        bridge_client=bridge,
    )


def _http_sends(http):
    """Bodies of every keyless ``POST /v2/cloud-agents/messages``."""
    return [b for m, p, b in http.calls if m == "POST_UNSIGNED"]


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

    # Keyless: one unsigned POST /v2/cloud-agents/messages, no bridge send.
    sends = _http_sends(cfg.http_client)
    assert [p for m, p, _ in cfg.http_client.calls if m == "POST_UNSIGNED"] == [
        "/v2/cloud-agents/messages",
    ]
    assert len(sends) == 1
    body = sends[0]
    assert body["plaintext"] == "hi alice"
    assert body["recipient_slug"] == "alice-0001"
    assert "space_id" not in body and "channel_id" not in body
    assert bridge.sent == []
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

    sends = _http_sends(cfg.http_client)
    assert len(sends) == 1
    body = sends[0]
    assert body["plaintext"] == "team update"
    assert body["space_id"] == "sp_1"
    assert body["channel_id"] == "ch_xyz"
    assert "recipient_slug" not in body
    assert bridge.sent == []
    assert "msg_bridgeack" in result
    assert enc_calls == []


@pytest.mark.asyncio
async def test_b_send_message_channel_threads_on_bridge(tmp_path):
    """root_id now threads on bridge (replaces the old top-level-note
    test). ``send_send`` carries ``thread_root_id`` = the resolved TRUE
    root of ``root_id`` and ``reply_to_id`` = the raw id the agent
    passed. Seed a root + a reply so resolution makes a real hop
    (resolved root != passed id), proving the resolver ran."""
    ms = MessageStore(str(tmp_path / "b_thread.db"))
    await ms.mark_channel_space("ch_xyz", "sp_1")
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_xyz",
        "space_id": "sp_1", "content": "root",
        "sent_at": 1_700_000_000_000,
        "thread_root_id": None, "reply_to_id": None,
    })
    await ms.store({
        "envelope_id": "msg_reply", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_xyz",
        "space_id": "sp_1", "content": "a reply",
        "sent_at": 1_700_000_000_001,
        "thread_root_id": "msg_root", "reply_to_id": "msg_root",
    })
    bridge = FakeBridge()
    cfg = _tools_cfg(tmp_path, bridge=bridge, data_client=ms)
    tools = build_dispatch(cfg)

    result = await tools["send_message"](
        channel="ch_xyz", text="reply", root_id="msg_reply",
    )
    sends = _http_sends(cfg.http_client)
    assert len(sends) == 1
    body = sends[0]
    # Resolved to the true root, not the intermediate reply id.
    assert body["thread_root_id"] == "msg_root"
    # Raw parent id the agent passed rides reply_to_id.
    assert body["reply_to_id"] == "msg_reply"
    assert body["space_id"] == "sp_1"
    assert body["channel_id"] == "ch_xyz"
    assert bridge.sent == []
    # No stale "top-level" / "not wired" note — threading is live now.
    assert "top-level" not in result.lower()
    assert "not wired" not in result.lower()
    assert "msg_bridgeack" in result


@pytest.mark.asyncio
async def test_b_send_message_dm_toplevel_on_bridge(tmp_path):
    """DM route of ``send_message`` posts TOP-LEVEL: even with a real DM
    root seeded locally (so resolution + same-channel validation would
    otherwise keep it), the DM reply drops ``thread_root_id`` /
    ``reply_to_id`` so it renders inline in the linear DM view instead of
    hiding behind an "N replies" thread badge. Channels keep threading —
    see ``test_b_send_message_channel_threads_on_bridge``."""
    ms = MessageStore(str(tmp_path / "b_dm_thread.db"))
    # Seed a real DM root: proves the top-level behavior is the DM guard,
    # not a resolution/validation miss silently dropping the id.
    await ms.store({
        "envelope_id": "dm_root", "envelope_kind": "dm",
        "sender_slug": "alice-0001", "channel_id": None,
        "space_id": None, "recipient_slug": "bot-0001",
        "content": "root dm", "sent_at": 1_700_000_000_002,
        "thread_root_id": None, "reply_to_id": None,
    })
    bridge = FakeBridge()
    cfg = _tools_cfg(tmp_path, bridge=bridge, data_client=ms)
    tools = build_dispatch(cfg)

    result = await tools["send_message"](
        channel="@alice-0001", text="reply", root_id="dm_root",
    )
    sends = _http_sends(cfg.http_client)
    assert len(sends) == 1
    body = sends[0]
    assert body["recipient_slug"] == "alice-0001"
    # DM reply is top-level: no thread keys ride the body.
    assert "thread_root_id" not in body
    assert "reply_to_id" not in body
    assert bridge.sent == []
    assert "msg_bridgeack" in result


@pytest.mark.asyncio
async def test_send_fallback_message_channel_threads_dm_toplevel_on_bridge(tmp_path):
    """``send_fallback_message`` on the bridge (the keyless path cloud/E2B
    agents use): a CHANNEL reply keeps threading — ``root_id`` rides as
    both ``thread_root_id`` and ``reply_to_id``; a DM reply posts
    TOP-LEVEL — both thread keys are dropped so it renders inline instead
    of behind an "N replies" badge."""
    bridge = FakeBridge()
    client = _bridge_client(tmp_path, bridge, db="fallback_thread.db")
    await client.store.mark_channel_space("ch_a", "sp_1")

    # channel route
    await client.send_fallback_message("ch_a", "chan reply", root_id="msg_root")
    # DM route: stash a DM sender so empty channel_id routes to them.
    client._last_dm_sender = "carol-0001"
    await client.send_fallback_message("", "dm reply", root_id="msg_root2")

    assert len(bridge.sent) == 2
    chan = bridge.sent[0]
    assert chan["channel_id"] == "ch_a" and chan["space_id"] == "sp_1"
    # Channel reply still threads (unchanged).
    assert chan["thread_root_id"] == "msg_root"
    assert chan["reply_to_id"] == "msg_root"
    dm = bridge.sent[1]
    assert dm["recipient_slug"] == "carol-0001"
    # DM reply is top-level: no thread ids even though a root_id was passed.
    assert dm["thread_root_id"] is None
    assert dm["reply_to_id"] is None


@pytest.mark.asyncio
async def test_inbound_thread_ids_surface_on_stored_row(tmp_path):
    """IN: an inbound ``message`` frame carrying
    ``thread_root_id``/``reply_to_id`` yields a stored row with those ids
    populated. The parent root arrives on the same connection (same
    channel) so the strict admit-time ``_validate_incoming_parent_id``
    check keeps them instead of wiping to None."""
    scripted = [
        {
            "type": "message", "envelope_id": "env_root_in",
            "sender_slug": "alice-0001", "envelope_kind": "channel",
            "space_id": "sp_1", "channel_id": "ch_a",
            "sent_at": 1_700_000_000_100, "plaintext": "root",
        },
        {
            "type": "message", "envelope_id": "env_reply_in",
            "sender_slug": "alice-0001", "envelope_kind": "channel",
            "space_id": "sp_1", "channel_id": "ch_a",
            "sent_at": 1_700_000_000_200, "plaintext": "threaded inbound",
            "thread_root_id": "env_root_in", "reply_to_id": "env_root_in",
        },
    ]
    bridge = FakeBridge(scripted=scripted)
    client = _bridge_client(tmp_path, bridge, db="in_thread.db")
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        if any(m.get("envelope_id") == "env_reply_in" for m in batch):
            done.set()

    await _drive_listen_until(client, on_message=on_message, done=done)

    row = await client.store.get_message_by_envelope("env_reply_in")
    assert row is not None
    assert row.thread_root_id == "env_root_in"
    assert row.reply_to_id == "env_root_in"


@pytest.mark.asyncio
async def test_enrichment_prefers_frame_display_name_no_http(tmp_path):
    """c-1: when the inbound frame carries a sender display name, the
    rendered ``sender_display_name`` uses it and NO
    ``/identities/profiles`` GET is made (the pre-seed makes
    ``_fetch_display_name`` a cache hit)."""
    calls: list[str] = []

    async def _recording_get(path, *a, **k):
        calls.append(path)
        return {}

    bridge = FakeBridge(scripted=[{
        "type": "message", "envelope_id": "env_named",
        "sender_slug": "alice-0001", "envelope_kind": "channel",
        "space_id": "sp_1", "channel_id": "ch_a",
        "sent_at": 1_700_000_000_300, "plaintext": "hi",
        "sender_display_name": "Alice Cooper",
    }])
    client = _bridge_client(tmp_path, bridge, db="enrich_named.db")
    client.http.get = _recording_get  # type: ignore[method-assign]

    surfaced: list = []
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        surfaced.append(batch)
        done.set()

    await _drive_listen_until(client, on_message=on_message, done=done)

    named = [m for m in surfaced[0] if m["envelope_id"] == "env_named"][0]
    assert named["sender_display_name"] == "Alice Cooper"
    # No /identities/profiles GET at all — resolution came off the frame.
    assert not any("identities/profiles" in p for p in calls), calls
    # The pre-seed actually populated the profile cache.
    assert client._profile_cache.get("alice-0001", (None,))[0] == "Alice Cooper"


@pytest.mark.asyncio
async def test_enrichment_degrades_without_frame_name(tmp_path):
    """c-2: without a frame-carried name the helpers degrade to an empty
    display name (render falls back to @slug) and never raise."""
    bridge = FakeBridge(scripted=[{
        "type": "message", "envelope_id": "env_unnamed",
        "sender_slug": "bob-0001", "envelope_kind": "channel",
        "space_id": "sp_1", "channel_id": "ch_a",
        "sent_at": 1_700_000_000_400, "plaintext": "hi",
        # no sender_display_name on the frame
    }])
    client = _bridge_client(tmp_path, bridge, db="enrich_unnamed.db")

    surfaced: list = []
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        surfaced.append(batch)
        done.set()

    await _drive_listen_until(client, on_message=on_message, done=done)

    named = [m for m in surfaced[0] if m["envelope_id"] == "env_unnamed"][0]
    assert named["sender_display_name"] == ""  # degraded
    assert named["sender_slug"] == "bob-0001"


def test_preseed_frame_display_name_unit(tmp_path):
    """Focused: the pre-seed helper seeds a non-empty frame name for a
    known slug, and leaves the cache untouched when the name is
    absent/blank or the slug is missing (never pins a false miss)."""
    client = _bridge_client(tmp_path, FakeBridge(), db="preseed.db")

    from puffo_agent.crypto.message import MessagePayload as _MP

    def _payload(slug):
        return _MP(
            payload_type="message", version=1, envelope_id="e",
            envelope_kind="channel", sender_slug=slug, sender_subkey_id="",
            sent_at=1, message_nonce="", content_type="text/plain",
            content="x", is_visible_to_human=True, space_id="sp_1",
            channel_id="ch_a", recipient_slug=None,
        )

    # present → seeds
    client._preseed_frame_display_name(
        {"sender_display_name": "Alice Cooper", "avatar_url": "http://a/x.png"},
        _payload("alice-0001"),
    )
    assert client._profile_cache["alice-0001"][0] == "Alice Cooper"
    assert client._profile_cache["alice-0001"][1] == "http://a/x.png"

    # blank name → cache untouched
    client._preseed_frame_display_name(
        {"sender_display_name": "   "}, _payload("bob-0001"),
    )
    assert "bob-0001" not in client._profile_cache

    # absent name → cache untouched
    client._preseed_frame_display_name({}, _payload("dave-0001"))
    assert "dave-0001" not in client._profile_cache

    # missing slug → no crash, nothing seeded
    client._preseed_frame_display_name(
        {"sender_display_name": "Nobody"}, _payload(""),
    )
    assert "" not in client._profile_cache

    # fallback key `display_name` also works
    client._preseed_frame_display_name(
        {"display_name": "Eve X"}, _payload("eve-0001"),
    )
    assert client._profile_cache["eve-0001"][0] == "Eve X"


def test_phase25_gap_doc_names_all_routes():
    """c-2 (doc): the phase-2.5 server-gaps doc exists and names the
    inbound Message frame (thread ids + display name) plus the three
    token-read REST routes the keyless enrichment path needs."""
    from pathlib import Path

    doc = (
        Path(__file__).resolve().parents[1]
        / "roadmap" / "cloud-agent" / "PHASE25-SERVER-ROUTE-GAPS.md"
    )
    assert doc.is_file(), f"missing gap doc: {doc}"
    text = doc.read_text(encoding="utf-8")
    # Inbound Message frame + the fields the agent already reads/pre-seeds.
    assert "Message" in text
    assert "thread_root_id" in text and "reply_to_id" in text
    assert "sender_display_name" in text
    # The three token-auth REST read routes.
    assert "identities/profiles" in text
    assert "/spaces/{space_id}/channels" in text
    assert "/spaces/{space_id}/members" in text


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
# (e) attachments over the bridge: keyless blob upload/download, native
#     unchanged
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e_attachments_uploaded_keyless_and_reffed_on_bridge(
    tmp_path, monkeypatch,
):
    """OUT: a bridge DM attachment send uploads each file's PLAINTEXT
    bytes via ``upload_blob`` and rides the returned ``blob_id``(s) into
    the ``send`` frame's top-level ``attachments`` (with filename /
    mime_type / size_bytes) — no ``encrypt_*`` and no signed HTTP."""
    enc_calls: list[int] = []
    monkeypatch.setattr(
        pct_mod, "encrypt_message_with_content_key",
        lambda *a, **k: enc_calls.append(1),
    )
    monkeypatch.setattr(
        pct_mod, "encrypt_message", lambda *a, **k: enc_calls.append(1),
    )
    monkeypatch.setattr(
        pct_mod, "encrypt_attachment", lambda *a, **k: enc_calls.append(1),
    )

    ms = MessageStore(str(tmp_path / "e_out.db"))
    bridge = FakeBridge()
    http = FakeHttp()
    cfg = _tools_cfg(tmp_path, bridge=bridge, data_client=ms, http=http)
    tools = build_dispatch(cfg)

    body = b"hello attachment bytes"
    (tmp_path / "note.txt").write_bytes(body)
    result = await tools["send_message_with_attachments"](
        paths=["note.txt"], channel="@alice-0001", caption="see file",
    )

    # Uploaded exactly once, keyless, with the raw plaintext bytes.
    assert http.uploaded == [body]
    assert [p for m, p, _ in http.calls if m == "POST_BYTES_UNSIGNED"] == [
        "/v2/cloud-agents/blobs/upload",
    ]
    # One keyless send carrying the ref by blob_id; bridge untouched.
    assert bridge.uploaded == [] and bridge.sent == []
    sends = _http_sends(http)
    assert len(sends) == 1
    sent = sends[0]
    assert sent["plaintext"] == "see file"
    assert sent["recipient_slug"] == "alice-0001"
    assert "space_id" not in sent and "channel_id" not in sent
    refs = sent["attachments"]
    assert isinstance(refs, list) and len(refs) == 1
    ref = refs[0]
    assert ref["blob_id"] == "blob_0001"
    assert ref["filename"] == "note.txt"
    assert ref["mime_type"] == "text/plain"
    assert ref["size_bytes"] == len(body)
    # No signed-crypto: no encrypt, no signed /blobs/upload or /messages POST.
    assert enc_calls == []
    assert not any(
        p in ("/blobs/upload", "/messages") for _, p, _ in http.calls
    ), http.calls
    assert "msg_bridgeack" in result


@pytest.mark.asyncio
async def test_e_attachments_channel_route_on_bridge(tmp_path):
    """OUT (channel route): a channel attachment send resolves the
    space from the local cache and threads the ``send`` frame with the
    space/channel ids, carrying the blob ref."""
    ms = MessageStore(str(tmp_path / "e_out_ch.db"))
    await ms.mark_channel_space("ch_xyz", "sp_1")
    bridge = FakeBridge()
    cfg = _tools_cfg(tmp_path, bridge=bridge, data_client=ms)
    tools = build_dispatch(cfg)

    (tmp_path / "doc.txt").write_bytes(b"team doc")
    await tools["send_message_with_attachments"](
        paths=["doc.txt"], channel="ch_xyz", caption="team file",
    )
    assert len(cfg.http_client.uploaded) == 1
    assert bridge.uploaded == [] and bridge.sent == []
    sends = _http_sends(cfg.http_client)
    assert len(sends) == 1
    sent = sends[0]
    assert sent["space_id"] == "sp_1"
    assert sent["channel_id"] == "ch_xyz"
    assert "recipient_slug" not in sent
    assert sent["attachments"][0]["blob_id"] == "blob_0001"


@pytest.mark.asyncio
async def test_send_send_puts_attachments_top_level():
    """Frame-shape unit on the real ``CloudBridgeClient.send_send``:
    ``attachments`` lands at the frame top level when non-empty and is
    omitted entirely when ``None`` (a plain send stays shape-identical to
    the pre-attachment frame)."""
    from puffo_agent.agent.bridge_client import CloudBridgeClient

    client = CloudBridgeClient("http://127.0.0.1:1", "tok", "bot-0001")

    class _CapturingWs:
        def __init__(self, owner):
            self.owner = owner
            self.sent_frames: list[dict] = []

        async def send_json(self, frame):
            self.sent_frames.append(frame)
            # send_send set the ack future before calling send_json;
            # resolve it so the awaited call returns immediately.
            cref = frame.get("client_ref")
            fut = self.owner._send_acks.get(cref)
            if fut is not None and not fut.done():
                fut.set_result({"type": "ack", "envelope_id": "msg_cap"})

    ws = _CapturingWs(client)

    async def _require_ws():
        return ws

    client._require_ws = _require_ws  # type: ignore[method-assign]

    refs = [{"blob_id": "b1", "filename": "a.txt", "mime_type": "text/plain"}]
    await client.send_send(
        plaintext="cap", recipient_slug="alice-0001", attachments=refs,
    )
    frame_with = ws.sent_frames[-1]
    assert frame_with["attachments"] == refs
    assert frame_with["plaintext"] == "cap"
    assert frame_with["recipient_slug"] == "alice-0001"

    # No attachments → key absent entirely.
    await client.send_send(plaintext="hi", recipient_slug="alice-0001")
    frame_without = ws.sent_frames[-1]
    assert "attachments" not in frame_without


@pytest.mark.asyncio
async def test_inbound_attachments_surface_and_download_by_blob_id(tmp_path):
    """IN: an inbound ``message`` frame with top-level ``attachments``
    drives ``download_blob(blob_id)`` and writes the bytes into
    ``.puffo/inbox/<envelope_id>/<filename>``; the surfaced message
    event's ``attachments`` lists that path."""
    BLOB = "blob_in1"
    DATA = b"inbound report bytes"
    frame = {
        "type": "message", "envelope_id": "env_att_in",
        "sender_slug": "alice-0001", "envelope_kind": "channel",
        "space_id": "sp_1", "channel_id": "ch_a",
        "sent_at": 1_700_000_001_000, "plaintext": "see attached",
        "attachments": [{
            "blob_id": BLOB, "filename": "report.txt",
            "mime_type": "text/plain", "size_bytes": len(DATA),
        }],
    }
    bridge = FakeBridge(scripted=[frame], blobs={BLOB: DATA})
    client = _bridge_client(
        tmp_path, bridge, db="att_in.db", workspace=str(tmp_path),
    )
    surfaced: list = []
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        surfaced.append(batch)
        done.set()

    await _drive_listen_until(client, on_message=on_message, done=done)

    # Fetched by blob_id, no decrypt.
    assert bridge.downloaded == [BLOB]
    saved = tmp_path / ".puffo" / "inbox" / "env_att_in" / "report.txt"
    assert saved.is_file()
    assert saved.read_bytes() == DATA
    msg = [m for m in surfaced[0] if m["envelope_id"] == "env_att_in"][0]
    assert str(saved) in msg["attachments"]


@pytest.mark.asyncio
async def test_fetch_pending_backfill_carries_attachments(tmp_path):
    """IN (backfill): a message frame drained via the cold-start
    ``fetch_pending`` path (same ``frames()``/handle_inbound tail)
    surfaces + downloads attachments identically to a live frame."""
    BLOB = "blob_bf1"
    DATA = b"backfilled file"
    scripted = [
        {
            "type": "message", "envelope_id": "env_att_bf",
            "sender_slug": "alice-0001", "envelope_kind": "channel",
            "space_id": "sp_1", "channel_id": "ch_a",
            "sent_at": 1_700_000_001_500, "plaintext": "backfilled attach",
            "attachments": [{
                "blob_id": BLOB, "filename": "bf.txt",
                "mime_type": "text/plain", "size_bytes": len(DATA),
            }],
        },
        {"type": "pending_delivered", "count": 1},
    ]
    bridge = FakeBridge(scripted=scripted, blobs={BLOB: DATA})
    client = _bridge_client(
        tmp_path, bridge, db="att_bf.db", workspace=str(tmp_path),
    )
    surfaced: list = []
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        surfaced.append(batch)
        done.set()

    await _drive_listen_until(client, on_message=on_message, done=done)

    # The backfill drive fired exactly one fetch_pending.
    assert bridge.fetch_pending_count == 1
    assert bridge.downloaded == [BLOB]
    saved = tmp_path / ".puffo" / "inbox" / "env_att_bf" / "bf.txt"
    assert saved.is_file() and saved.read_bytes() == DATA
    msg = [m for m in surfaced[0] if m["envelope_id"] == "env_att_bf"][0]
    assert str(saved) in msg["attachments"]


@pytest.mark.asyncio
async def test_inbound_missing_blob_skipped_loop_survives(tmp_path):
    """Fail-soft: a ref whose blob ``download_blob`` returns ``None`` for
    (missing / oversized) is skipped — no exception, the message still
    delivers with empty ``attachments``, and the listen loop keeps
    running so a following frame is still processed."""
    scripted = [
        {
            "type": "message", "envelope_id": "env_missing_blob",
            "sender_slug": "alice-0001", "envelope_kind": "channel",
            "space_id": "sp_1", "channel_id": "ch_a",
            "sent_at": 1_700_000_002_000, "plaintext": "attached but gone",
            "attachments": [{"blob_id": "blob_missing", "filename": "x.bin"}],
        },
        {
            "type": "message", "envelope_id": "env_after_missing",
            "sender_slug": "alice-0001", "envelope_kind": "channel",
            "space_id": "sp_1", "channel_id": "ch_a",
            "sent_at": 1_700_000_002_100, "plaintext": "still flowing",
        },
    ]
    # Empty blob map → download_blob returns None for blob_missing.
    bridge = FakeBridge(scripted=scripted, blobs={})
    client = _bridge_client(
        tmp_path, bridge, db="missing_blob.db", workspace=str(tmp_path),
    )
    surfaced: dict[str, dict] = {}
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        for m in batch:
            surfaced[m["envelope_id"]] = m
        if "env_after_missing" in surfaced:
            done.set()

    await _drive_listen_until(client, on_message=on_message, done=done)

    # Download was attempted then skipped.
    assert bridge.downloaded == ["blob_missing"]
    # Both messages delivered — the loop survived the missing blob.
    assert await client.store.get_message_by_envelope("env_missing_blob") is not None
    assert await client.store.get_message_by_envelope("env_after_missing") is not None
    assert "env_missing_blob" in surfaced and "env_after_missing" in surfaced
    # The message with the missing blob carries no attachment path.
    assert surfaced["env_missing_blob"]["attachments"] == []
    # No blob dir/file was written for the skipped ref.
    assert not (tmp_path / ".puffo" / "inbox" / "env_missing_blob" / "x.bin").exists()


@pytest.mark.asyncio
async def test_e_native_attachments_still_encrypt_and_signed_http(
    tmp_path, monkeypatch,
):
    """Native (``bridge_client=None``) attachment send still encrypts the
    file (``encrypt_attachment``) + the envelope
    (``encrypt_message_with_content_key``) and uploads the ciphertext via
    signed ``http_client.post_bytes('/blobs/upload', ...)``, then POSTs
    ``/messages`` — the bridge blob surface is never touched."""
    enc_att_calls: list[int] = []
    enc_msg_calls: list[int] = []

    class _FakeMeta:
        def __init__(self):
            self.blob_id = ""

        def to_dict(self):
            return {"blob_id": self.blob_id, "filename": "note.txt"}

    def _enc_att_spy(*, plaintext, filename, mime_type, blob_id):
        enc_att_calls.append(1)
        return (b"CIPHERTEXT", _FakeMeta())

    def _enc_msg_spy(inp, signing_key, *, now_ms=None):
        enc_msg_calls.append(1)
        return ({"envelope_id": "msg_native_att", "type": "message_envelope"}, b"ck")

    monkeypatch.setattr(pct_mod, "encrypt_attachment", _enc_att_spy)
    monkeypatch.setattr(pct_mod, "encrypt_message_with_content_key", _enc_msg_spy)

    ks = _native_keystore(tmp_path)
    http = FakeHttp()
    http.responses["/blobs/upload"] = {"blob_id": "blob_native_att"}
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
    ms = MessageStore(str(tmp_path / "e_native.db"))
    cfg = PuffoCoreToolsConfig(
        slug="bot-0001",
        device_id="dev_test",
        keystore=ks,
        http_client=http,
        data_client=ms,
        space_id="sp_home",
        workspace=str(tmp_path),
        bridge_client=None,  # native
    )
    tools = build_dispatch(cfg)

    (tmp_path / "note.txt").write_bytes(b"native file bytes")
    result = await tools["send_message_with_attachments"](
        paths=["note.txt"], channel="@alice-0001", caption="native attach",
    )

    assert enc_att_calls, "native attachment send must encrypt the file"
    assert enc_msg_calls, "native attachment send must encrypt the envelope"
    assert any(
        m == "POST_BYTES" and p == "/blobs/upload" for m, p, _ in http.calls
    ), f"native must upload ciphertext via signed post_bytes; calls={http.calls}"
    assert any(
        m == "POST" and p == "/messages" for m, p, _ in http.calls
    ), f"native must POST the envelope; calls={http.calls}"
    assert "uploaded 1 file" in result


# --------------------------------------------------------------------------
# (F1) handled bridge messages are acked exactly once; the shared tail
#      (native's whole inbound path) never acks.
# --------------------------------------------------------------------------


async def _open_dispatch_client(tmp_path, bridge, *, db):
    """A bridge client with the minimal per-listen() state so
    ``_dispatch_bridge_frame`` / ``_handle_plaintext_payload`` can run
    without spinning up the full listen loop (mirrors the reference-path
    setup in test (a))."""
    client = _bridge_client(tmp_path, bridge, db=db)
    client._queue = asyncio.PriorityQueue()
    client._queue_seq = 0
    client._thread_state = {}
    await client.store.open()
    return client


def _bridge_message_frame(env_id: str) -> dict:
    return {
        "type": "message", "envelope_id": env_id,
        "sender_slug": "alice-0001", "envelope_kind": "channel",
        "space_id": "sp_1", "channel_id": "ch_a",
        "sent_at": 1_700_000_004_000, "plaintext": "ack me",
    }


@pytest.mark.asyncio
async def test_f1_handled_bridge_message_acked_exactly_once(tmp_path):
    """A handled inbound ``message`` frame schedules exactly one
    ``send_ack([envelope_id])`` off the dispatcher — the ack is async
    (never awaited inline, which would deadlock ``frames()``)."""
    ENV_ID = "env_ack_once"
    bridge = FakeBridge()
    client = await _open_dispatch_client(tmp_path, bridge, db="ack_once.db")

    await client._dispatch_bridge_frame(_bridge_message_frame(ENV_ID))
    # The ack is scheduled, not awaited inline: exactly one task is
    # in-flight right after dispatch returns.
    assert len(client._ack_tasks) == 1
    # Let the scheduled ack task run to completion.
    await asyncio.gather(*client._ack_tasks)

    assert bridge.acked == [[ENV_ID]]
    # The done-callback cleaned the task set up afterwards.
    assert client._ack_tasks == set()


@pytest.mark.asyncio
async def test_f1_ack_over_full_listen_loop(tmp_path):
    """End-to-end over ``_listen_bridge``: a live frame surfaces AND gets
    acked exactly once (proves the ack fires on the real loop, not just a
    direct dispatch call)."""
    ENV_ID = "env_ack_live"
    bridge = FakeBridge(scripted=[_bridge_message_frame(ENV_ID)])
    client = _bridge_client(tmp_path, bridge, db="ack_live.db")
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        done.set()

    await _drive_listen_until(client, on_message=on_message, done=done)
    # Drain any ack task still in flight after the loop was cancelled.
    if client._ack_tasks:
        await asyncio.gather(*client._ack_tasks, return_exceptions=True)

    assert bridge.acked == [[ENV_ID]]


@pytest.mark.asyncio
async def test_f1_failing_message_handling_skips_ack(tmp_path):
    """A frame whose handling raises is NOT acked — the ack sits inside
    the handling ``try`` after a clean ``_handle_plaintext_payload``, so a
    raise skips it and the server redelivers."""
    ENV_ID = "env_ack_fail"
    bridge = FakeBridge()
    client = await _open_dispatch_client(tmp_path, bridge, db="ack_fail.db")

    async def _boom(payload):
        raise RuntimeError("handling blew up")

    client._handle_plaintext_payload = _boom  # type: ignore[method-assign]

    # _dispatch_bridge_frame swallows the handling exception (logs it).
    await client._dispatch_bridge_frame(_bridge_message_frame(ENV_ID))
    await asyncio.sleep(0)

    assert client._ack_tasks == set()
    assert bridge.acked == []


@pytest.mark.asyncio
async def test_f1_shared_tail_does_not_ack_native_untouched(tmp_path):
    """The ack lives ONLY in ``_dispatch_bridge_frame``. The shared
    ``_handle_plaintext_payload`` tail — the entirety of native's inbound
    path — issues no ack. Proven with a bridge PRESENT: even then,
    driving the tail directly acks nothing."""
    bridge = FakeBridge()
    client = await _open_dispatch_client(tmp_path, bridge, db="tail_noack.db")

    payload = MessagePayload(
        payload_type="puffo.message", version=1,
        envelope_id="env_tail", envelope_kind="channel",
        sender_slug="alice-0001", sender_subkey_id="", sent_at=1,
        message_nonce="", content_type="text/plain", content="hi",
        is_visible_to_human=True, space_id="sp_1", channel_id="ch_a",
    )
    await client._handle_plaintext_payload(payload)
    await asyncio.sleep(0)

    assert client._ack_tasks == set()
    assert bridge.acked == []


@pytest.mark.asyncio
async def test_f1_native_config_client_never_acks(tmp_path):
    """A genuinely native client (``bridge_client=None``) has no bridge to
    ack against and never schedules an ack task when its inbound tail
    runs."""
    ks = _native_keystore(tmp_path)
    http = PuffoCoreHttpClient("http://127.0.0.1:1", ks, "bot-0001")
    store = MessageStore(str(tmp_path / "native_noack.db"))
    client = PuffoCoreMessageClient(
        slug="bot-0001", device_id="dev_test", space_id="sp_home",
        keystore=ks, http_client=http, message_store=store,
    )  # no bridge → native
    assert client._bridge is None
    client._queue = asyncio.PriorityQueue()
    client._queue_seq = 0
    client._thread_state = {}
    await client.store.open()

    async def _empty_get(path, *a, **k):
        return {}

    client.http.get = _empty_get  # type: ignore[method-assign]

    payload = MessagePayload(
        payload_type="puffo.message", version=1,
        envelope_id="env_native_tail", envelope_kind="channel",
        sender_slug="alice-0001", sender_subkey_id="", sent_at=1,
        message_nonce="", content_type="text/plain", content="hi",
        is_visible_to_human=True, space_id="sp_1", channel_id="ch_a",
    )
    await client._handle_plaintext_payload(payload)
    await asyncio.sleep(0)

    assert client._ack_tasks == set()


# --------------------------------------------------------------------------
# (F6) the inbound bridge blob_id filename fallback is basename-sanitised
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f6_blob_id_fallback_sanitised_stays_in_inbox(tmp_path):
    """A ref with NO filename and a ``blob_id`` carrying path separators
    (``../escape``) is written by its basename INSIDE the inbox dir, never
    a level above it."""
    DATA = b"escape-attempt-bytes"
    frame = {
        "type": "message", "envelope_id": "env_f6",
        "sender_slug": "alice-0001", "envelope_kind": "channel",
        "space_id": "sp_1", "channel_id": "ch_a",
        "sent_at": 1_700_000_005_000, "plaintext": "see attached",
        # No filename → fallback to the (malicious) blob_id.
        "attachments": [{"blob_id": "../escape"}],
    }
    bridge = FakeBridge(scripted=[frame], blobs={"../escape": DATA})
    client = _bridge_client(
        tmp_path, bridge, db="f6.db", workspace=str(tmp_path),
    )
    surfaced: list = []
    done = asyncio.Event()

    async def on_message(root_id, batch, channel_meta):
        surfaced.append(batch)
        done.set()

    await _drive_listen_until(client, on_message=on_message, done=done)

    inbox = (tmp_path / ".puffo" / "inbox" / "env_f6").resolve()
    saved = inbox / "escape"
    assert saved.is_file(), "sanitised file must land inside the inbox dir"
    assert saved.read_bytes() == DATA
    # The written path resolves inside the inbox — no ../ escape.
    assert str(saved.resolve()).startswith(str(inbox))
    # The pre-fix bug would have written one level up (a sibling of the
    # envelope dir); assert that escaped location does NOT exist.
    assert not (tmp_path / ".puffo" / "inbox" / "escape").exists()
    # Surfaced attachment path points at the sanitised in-inbox file.
    msg = [m for m in surfaced[0] if m["envelope_id"] == "env_f6"][0]
    assert str(saved) in msg["attachments"]


def test_message_payload_to_dict_omits_attachments():
    """Guard: the additive ``attachments`` field is NOT serialized by
    ``to_payload_dict()`` — native seal canonical bytes stay unchanged."""
    p = MessagePayload(
        payload_type="message_payload", version=1, envelope_id="msg_x",
        envelope_kind="dm", sender_slug="bot-0001", sender_subkey_id="sk",
        sent_at=1, message_nonce="n", content_type="text/plain",
        content="hi", is_visible_to_human=True,
        attachments=[{"blob_id": "b1"}],
    )
    d = p.to_payload_dict()
    assert "attachments" not in d
