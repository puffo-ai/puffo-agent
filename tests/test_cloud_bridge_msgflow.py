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
from types import SimpleNamespace

import pytest

import puffo_agent.agent.puffo_core_client as pcc_mod
import puffo_agent.agent.send_coordinator as sc_mod
from puffo_agent.agent.message_store import MessageStore, ReceiptDisposition
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.agent.send_coordinator import SendCoordinator
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
        spaces: list[dict] | None = None,
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
        # added_to_space: send_list_spaces calls are counted so a test
        # can assert the re-list refresh fired; ``spaces`` is the canned
        # entry list the reply carries.
        self.list_spaces_count = 0
        self._spaces = list(spaces or [])
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

    async def send_list_spaces(self, *, timeout: float = 30.0) -> dict:
        self.list_spaces_count += 1
        return {"type": "spaces", "spaces": list(self._spaces)}

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
        if path == "/v2/cloud-agents/agent-runtime/messages:send":
            freshness = body["freshness"]
            request_baseline = freshness["context_baseline_seq"]
            return {
                "state": "sent",
                "envelope_id": "msg_bridgeack",
                "seq": freshness["seen_seq"] + 1,
                "replay": False,
                "missing_devices": [],
                "freshness": {
                    "mode": freshness["mode"],
                    "context_baseline_seq": (
                        request_baseline
                        if request_baseline is not None
                        else freshness["seen_seq"]
                    ),
                    "seen_seq": freshness["seen_seq"],
                    "latest_seq_before_send": freshness["seen_seq"],
                },
            }
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
        catchup_stale_hours=0,
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


def _install_native_send_coordinator(cfg: PuffoCoreToolsConfig) -> None:
    cfg.send_coordinator = SendCoordinator(
        slug=cfg.slug,
        keystore=cfg.keystore,
        http_client=cfg.http_client,
        data_client=cfg.data_client,
        workspace=cfg.workspace,
    )


def _http_sends(http):
    """Bodies of every keyless coordinated channel send."""
    return [b for m, p, b in http.calls if m == "POST_UNSIGNED"]


class _RuntimeWake:
    def __init__(self, done: asyncio.Event, expected: int):
        self.done = done
        self.expected = expected
        self.count = 0

    def notify(self) -> None:
        self.count += 1
        if self.count >= self.expected:
            self.done.set()

    def notify_delivery(self) -> None:
        return None


async def _drive_listen_until(
    client, *, done: asyncio.Event, notifications: int = 1, timeout=5.0,
):
    """Run ``_listen_bridge`` as a task until ``done`` fires, then cancel
    it cleanly. Returns nothing — assertions read the store / bridge."""
    previous_runtime = client.global_runtime
    client.global_runtime = _RuntimeWake(done, notifications)
    task = asyncio.ensure_future(client._listen_bridge())
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        client.global_runtime = previous_runtime


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
    # --- bridge path ---
    bridge = FakeBridge(scripted=[frame])
    client = _bridge_client(tmp_path, bridge, db="bridge.db")
    done = asyncio.Event()

    await _drive_listen_until(client, done=done)

    bridge_row = await client.store.get_message_by_envelope(ENV_ID)
    assert bridge_row is not None, "bridge inbound frame was not persisted"

    assert bridge_row.envelope_id == ENV_ID
    assert bridge_row.channel_id == CHANNEL
    assert bridge_row.space_id == SPACE
    assert bridge_row.recipient_slug is None
    assert bridge_row.sent_at == SENT_AT
    assert bridge_row.content["text"] == CONTENT
    assert bridge_row.sender_slug == SENDER
    assert bridge_row.envelope_kind == "channel"
    assert bridge_row.content_type == "text/plain"
    assert bridge_row.server_seq is None
    assert bridge_row.local_ordinal == 1

    # Decrypt never ran, and the production runtime wake fired.
    assert decrypt_calls == []


@pytest.mark.asyncio
async def test_sequenced_bridge_delivery_is_durable_server_lane_and_idempotent(
    tmp_path,
):
    bridge = FakeBridge()
    client = _bridge_client(tmp_path, bridge, db="sequenced.db")
    frame = {
        "type": "message",
        "seq": 17,
        "envelope_id": "env_sequenced",
        "sender_slug": "alice-0001",
        "envelope_kind": "channel",
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "sent_at": 1_700_000_000_000,
        "plaintext": "durable",
    }
    await client._dispatch_bridge_frame(frame)
    await asyncio.gather(*tuple(client._ack_tasks))
    await client._dispatch_bridge_frame(frame)
    await asyncio.gather(*tuple(client._ack_tasks))
    await client.store.close()
    await client.store.open()
    row = await client.store.get_message_by_envelope("env_sequenced")
    assert row is not None
    assert row.server_seq == 17
    assert row.receipt_disposition is ReceiptDisposition.ELIGIBLE
    assert row.local_ordinal is None
    assert MessageStore.target_projection(row) == "channel:sp_1:ch_1"
    assert [item.envelope_id for item in await client.store.get_pending()] == [
        "env_sequenced"
    ]
    assert bridge.acked == [["env_sequenced"], ["env_sequenced"]]
    await client.store.close()


@pytest.mark.asyncio
async def test_sequenced_bridge_preserves_thread_reply_and_dm_projections(tmp_path):
    bridge = FakeBridge()
    client = _bridge_client(tmp_path, bridge, db="sequenced-routes.db")
    await client.store.store({
        "envelope_id": "thread-root",
        "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "content": "root",
        "sent_at": 1,
    })
    await client._dispatch_bridge_frame({
        "type": "message",
        "seq": 18,
        "envelope_id": "thread-reply",
        "sender_slug": "alice-0001",
        "envelope_kind": "channel",
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "thread_root_id": "thread-root",
        "reply_to_id": "thread-root",
        "plaintext": "reply",
    })
    await client._dispatch_bridge_frame({
        "type": "message",
        "seq": 19,
        "envelope_id": "dm-sequenced",
        "sender_slug": "alice-0001",
        "recipient_slug": "bot-0001",
        "envelope_kind": "dm",
        "plaintext": "dm",
    })
    reply = await client.store.get_message_by_envelope("thread-reply")
    dm = await client.store.get_message_by_envelope("dm-sequenced")
    assert reply is not None
    assert reply.thread_root_id == reply.reply_to_id == "thread-root"
    assert MessageStore.target_projection(reply) == (
        "channel:sp_1:ch_1:thread:thread-root"
    )
    assert dm is not None
    assert MessageStore.target_projection(dm) == "dm:alice-0001"
    await client.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_seq", [None, 0, -1, True, "7", 1.0])
async def test_present_invalid_bridge_sequence_is_rejected_without_ack(
    tmp_path, bad_seq,
):
    bridge = FakeBridge()
    client = _bridge_client(tmp_path, bridge, db=f"bad-{bad_seq!r}.db")
    wakes = SimpleNamespace(inbox=0, held=0)

    class RuntimeSpy:
        def notify(self):
            wakes.inbox += 1

        def notify_delivery(self):
            wakes.held += 1

    client.global_runtime = RuntimeSpy()
    await client._dispatch_bridge_frame({
        "type": "message",
        "seq": bad_seq,
        "envelope_id": "bad-seq",
        "sender_slug": "alice-0001",
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "plaintext": "must not persist",
    })
    assert await client.store.get_message_by_envelope("bad-seq") is None
    assert bridge.acked == []
    await client._dispatch_bridge_frame({
        "type": "message",
        "envelope_id": "legacy-ok",
        "sender_slug": "alice-0001",
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "plaintext": "legacy",
    })
    await client._dispatch_bridge_frame({
        "type": "message",
        "envelope_id": "legacy-ok",
        "sender_slug": "alice-0001",
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "plaintext": "legacy",
    })
    await asyncio.gather(*tuple(client._ack_tasks))
    legacy = await client.store.get_message_by_envelope("legacy-ok")
    assert legacy is not None
    assert legacy.server_seq is None and legacy.local_ordinal is not None
    assert [row.envelope_id for row in await client.store.get_pending()] == [
        "legacy-ok"
    ]
    assert wakes.inbox == 1
    assert wakes.held == 0
    assert bridge.acked == [["legacy-ok"], ["legacy-ok"]]
    await client.store.close()


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
    client.auto_accept_dm = True
    done = asyncio.Event()

    await _drive_listen_until(client, done=done)

    row = await client.store.get_message_by_envelope("env_dm_1")
    assert row is not None
    assert row.envelope_kind == "dm"
    assert row.recipient_slug == "bot-0001"
    # DM sender stashed so send_fallback_message("") can reply to them.
    assert client._last_dm_sender == "carol-0001"


def test_payload_from_bridge_frame_skips_missing_envelope_id(tmp_path):
    client = _bridge_client(tmp_path, FakeBridge(), db="skip.db")
    assert client._payload_from_bridge_frame({"plaintext": "x"}) is None


def test_bridge_additive_fields_preserve_json_false_visibility_and_sender(tmp_path):
    """Canonical bridge fields are additive: old plaintext defaults remain."""
    client = _bridge_client(tmp_path, FakeBridge(), db="additive.db")
    payload = client._payload_from_bridge_frame({
        "type": "message", "envelope_id": "env_additive",
        "sender_slug": "build-bot", "sender_owner_slug": "alice",
        "sender_type": "agent", "space_id": "sp_1", "channel_id": "ch_1",
        "content": {"text": "caption", "attachments": [{"blob_id": "b1"}]},
        "plaintext": "must not win", "content_type": "puffo/message+attachments/v1",
        "is_visible_to_human": False,
    })
    assert payload is not None
    assert payload.content == {"text": "caption", "attachments": [{"blob_id": "b1"}]}
    assert payload.content_type == "puffo/message+attachments/v1"
    assert payload.is_visible_to_human is False
    assert payload.sender_owner_slug == "alice"
    assert payload.sender_type == "agent"


@pytest.mark.asyncio
async def test_pending_pages_continue_without_progress_loop(tmp_path):
    bridge = FakeBridge()
    client = _bridge_client(tmp_path, bridge, db="pages.db")
    await client._dispatch_bridge_frame({"type": "pending_delivered", "count": 50, "more": True})
    await asyncio.sleep(0)
    await client._dispatch_bridge_frame({"type": "pending_delivered", "count": 0, "more": True})
    await asyncio.sleep(0)
    await client._dispatch_bridge_frame({"type": "pending_delivered", "count": 0, "more": True})
    await asyncio.sleep(0)
    assert bridge.fetch_pending_count == 1


@pytest.mark.asyncio
async def test_canonical_bridge_contract_and_malformed_frame_followed_by_valid(tmp_path):
    """The wire parser, dispatcher and durable history agree on the
    canonical additive fields; one bad frame cannot poison the next one."""
    bridge = FakeBridge()
    client = await _open_dispatch_client(tmp_path, bridge, db="canonical.db")
    bad = {"type": "message", "envelope_id": "bad", "sender_slug": "alice", "seq": 0}
    canonical = {
        "type": "message", "envelope_id": "canonical", "sender_slug": "bot",
        "sender_owner_slug": "owner", "sender_type": "agent",
        "envelope_kind": "channel", "space_id": "sp_1", "channel_id": "ch_1",
        "sent_at": 100, "content_type": "puffo/message+attachments/v1",
        "content": {"caption": "caption fallback", "attachments": []},
        "is_visible_to_human": False, "future_additive_field": {"ignored": True},
    }
    await client._dispatch_bridge_frame(bad)
    await client._dispatch_bridge_frame(canonical)
    await client._dispatch_bridge_frame({
        "type": "message", "envelope_id": "legacy", "sender_slug": "alice",
        "space_id": "sp_1", "channel_id": "ch_1", "plaintext": "legacy text",
    })
    await asyncio.gather(*tuple(client._ack_tasks))
    row = await client.store.get_message_by_envelope("canonical")
    legacy = await client.store.get_message_by_envelope("legacy")
    history = await client.store.get_channel_history("ch_1")
    assert row is not None and legacy is not None
    assert row.content["original_content"] == canonical["content"]
    assert row.content["text"] == "caption fallback"
    assert row.content_type == canonical["content_type"]
    assert row.content["is_visible_to_human"] is False
    assert row.content["sender_owner_slug"] == "owner"
    assert row.content["sender_type"] == "agent"
    assert legacy.content["original_content"] == "legacy text"
    assert legacy.content_type == "text/plain"
    assert [value.envelope_id for value in history] == ["canonical", "legacy"]
    projected = history[0]
    assert projected.content["text"] == "caption fallback"
    assert projected.content["original_content"] == canonical["content"]
    assert projected.content_type == canonical["content_type"]
    assert projected.content["is_visible_to_human"] is False
    assert projected.content["sender_owner_slug"] == "owner"
    assert projected.content["sender_type"] == "agent"
    assert bridge.acked == [["canonical"], ["legacy"]]
    await client.store.close()


@pytest.mark.asyncio
async def test_pending_stream_101_drains_same_connection_stores_once_and_acks(tmp_path):
    ids = [f"pending-{index:03d}" for index in range(101)]
    frames = [_bridge_message_frame(envelope_id) for envelope_id in ids[:50]]
    frames += [{"type": "pending_delivered", "count": 50, "more": True}]
    frames += [_bridge_message_frame(envelope_id) for envelope_id in ids[50:100]]
    frames += [{"type": "pending_delivered", "count": 50, "more": True}]
    frames += [_bridge_message_frame(ids[-1]), {"type": "pending_delivered", "count": 1, "more": False}]
    bridge = FakeBridge(scripted=frames)
    client = _bridge_client(tmp_path, bridge, db="pending-101.db")
    done = asyncio.Event()

    await _drive_listen_until(client, done=done, notifications=101)
    if client._ack_tasks:
        await asyncio.gather(*tuple(client._ack_tasks))
    stored = await client.store.get_channel_history("ch_a", limit=200)
    assert {row.envelope_id for row in stored} == set(ids)
    assert len(stored) == 101
    assert {item for batch in bridge.acked for item in batch} == set(ids)
    assert len(bridge.acked) == 101
    assert bridge.connect_count == 1 and bridge.fetch_pending_count >= 3
    await client.store.close()


# --------------------------------------------------------------------------
# (b) bridge send, no encrypt
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_send_message_dm_uses_bridge_no_encrypt(tmp_path, monkeypatch):
    enc_calls: list[int] = []
    monkeypatch.setattr(
        sc_mod, "encrypt_message_with_content_key",
        lambda *a, **k: enc_calls.append(1),
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
    assert result["envelope_id"] == "msg_bridgeack"
    assert result["state"] == "sent"
    assert enc_calls == []


@pytest.mark.asyncio
async def test_b_send_message_channel_uses_bridge_no_encrypt(tmp_path, monkeypatch):
    enc_calls: list[int] = []
    monkeypatch.setattr(
        sc_mod, "encrypt_message_with_content_key",
        lambda *a, **k: enc_calls.append(1),
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
    assert result["envelope_id"] == "msg_bridgeack"
    assert result["state"] == "sent"
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
    assert "top-level" not in result.get("note", "").lower()
    assert "not wired" not in result.get("note", "").lower()
    assert result["envelope_id"] == "msg_bridgeack"


@pytest.mark.asyncio
async def test_b_send_message_dm_threads_on_bridge(tmp_path):
    """DM route of ``send_message`` also threads. With the DM root seeded
    locally, resolution + same-channel validation keep it, so both
    ``thread_root_id`` and ``reply_to_id`` carry it — proving the DM
    branch is wired, not silently dropping the ids."""
    ms = MessageStore(str(tmp_path / "b_dm_thread.db"))
    # Seed a real DM root so thread_root_id survives validation.
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
    assert body["thread_root_id"] == "dm_root"
    assert body["reply_to_id"] == "dm_root"
    assert bridge.sent == []
    assert result["envelope_id"] == "msg_bridgeack"


@pytest.mark.asyncio
async def test_send_fallback_channel_fails_closed_but_dm_keeps_bridge(tmp_path):
    bridge = FakeBridge()
    client = _bridge_client(tmp_path, bridge, db="fallback_thread.db")
    await client.store.mark_channel_space("ch_a", "sp_1")

    # channel route
    failed = await client.send_fallback_message(
        "ch_a", "chan reply", root_id="msg_root"
    )
    # DM route: stash a DM sender so empty channel_id routes to them.
    client._last_dm_sender = "carol-0001"
    await client.send_fallback_message("", "dm reply", root_id="msg_root2")

    assert failed["state"] == "failed"
    assert failed["error_kind"] == "coordinator_unavailable"
    assert len(bridge.sent) == 1
    dm = bridge.sent[0]
    assert dm["recipient_slug"] == "carol-0001"
    assert dm["thread_root_id"] == "msg_root2"
    assert dm["reply_to_id"] == "msg_root2"


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

    await _drive_listen_until(client, done=done, notifications=2)

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

    done = asyncio.Event()

    await _drive_listen_until(client, done=done)

    named = await client.store.get_message_by_envelope("env_named")
    assert named is not None
    assert named.content["sender_display_name"] == "Alice Cooper"
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

    done = asyncio.Event()

    await _drive_listen_until(client, done=done)

    named = await client.store.get_message_by_envelope("env_unnamed")
    assert named is not None
    assert named.content["sender_display_name"] == ""  # degraded
    assert named.sender_slug == "bob-0001"


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
            "type": "message", "seq": 1, "envelope_id": "env_backfill",
            "sender_slug": "alice-0001", "envelope_kind": "channel",
            "space_id": "sp_1", "channel_id": "ch_a",
            "sent_at": 1_700_000_000_010, "plaintext": "backfilled",
        },
        {"type": "pending_delivered", "count": 1},
        {
            "type": "message", "seq": 2, "envelope_id": "env_live",
            "sender_slug": "alice-0001", "envelope_kind": "channel",
            "space_id": "sp_1", "channel_id": "ch_a",
            "sent_at": 1_700_000_000_020, "plaintext": "live one",
        },
    ]
    bridge = FakeBridge(scripted=scripted)
    client = _bridge_client(tmp_path, bridge, db="c.db")

    done = asyncio.Event()

    with caplog.at_level(logging.INFO, logger="puffo_agent.agent.puffo_core_client"):
        await _drive_listen_until(client, done=done, notifications=2)

    # Exactly one connect + one fetch_pending drove the cold-start drain.
    assert bridge.connect_count == 1
    assert bridge.fetch_pending_count == 1
    # pending_delivered was recognised as backfill completion.
    assert "backfill complete" in caplog.text
    # Both the backfill and the post-terminator live message landed.
    backfill = await client.store.get_message_by_envelope("env_backfill")
    live = await client.store.get_message_by_envelope("env_live")
    assert backfill is not None and backfill.server_seq == 1
    assert live is not None and live.server_seq == 2


@pytest.mark.asyncio
async def test_c_uncorrelated_error_frame_does_not_crash_loop(tmp_path, caplog):
    sentinels = [
        "MESSAGE_SENTINEL",
        "RAW_PROVIDER_FRAME_SENTINEL",
        "CIPHERTEXT_SENTINEL",
        "REASONING_SENTINEL",
        "TOOL_ARGUMENT_SENTINEL",
        "TOOL_RESULT_SENTINEL",
        "CREDENTIAL_SENTINEL",
    ]
    scripted = [
        {
            "type": "error",
            "code": "INTERNAL",
            "message": sentinels[0],
            "raw_frame": sentinels[1],
            "ciphertext": sentinels[2],
            "reasoning": sentinels[3],
            "tool_arguments": sentinels[4],
            "tool_result": sentinels[5],
            "credential": sentinels[6],
        },
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

    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.puffo_core_client"):
        await _drive_listen_until(client, done=done)

    assert "bridge error category=INTERNAL" in caplog.text
    assert all(sentinel not in caplog.text for sentinel in sentinels)
    # The loop survived the error frame and delivered the next message.
    assert await client.store.get_message_by_envelope("env_after_err") is not None
    await client.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("NO_SUBKEY", "NO_SUBKEY"),
        ("NOT_AUTHORIZED", "NOT_AUTHORIZED"),
        ("DECRYPT_FAILED", "DECRYPT_FAILED"),
        ("BAD_FRAME", "BAD_FRAME"),
        ("INTERNAL", "INTERNAL"),
        ("RAW_UNKNOWN_CODE_SENTINEL", "UNKNOWN_BRIDGE_ERROR"),
        (17, "UNKNOWN_BRIDGE_ERROR"),
        (None, "UNKNOWN_BRIDGE_ERROR"),
    ],
)
async def test_bridge_error_codes_are_closed_content_free_categories(
    tmp_path, caplog, code, category,
):
    client = _bridge_client(tmp_path, FakeBridge(), db=f"bridge_{category}.db")
    with caplog.at_level(
        logging.WARNING, logger="puffo_agent.agent.puffo_core_client"
    ):
        await client._dispatch_bridge_frame({
            "type": "error",
            "code": code,
            "message": "MESSAGE_PLAINTEXT_SENTINEL",
            "raw_frame": "RAW_PROVIDER_FRAME_SENTINEL",
            "ciphertext": "CIPHERTEXT_SENTINEL",
            "reasoning": "REASONING_SENTINEL",
            "tool_arguments": "TOOL_ARGUMENT_SENTINEL",
            "tool_result": "TOOL_RESULT_SENTINEL",
            "credential": "CREDENTIAL_SENTINEL",
        })
    assert f"bridge error category={category}" in caplog.text
    for sentinel in (
        "MESSAGE_PLAINTEXT_SENTINEL",
        "RAW_PROVIDER_FRAME_SENTINEL",
        "CIPHERTEXT_SENTINEL",
        "REASONING_SENTINEL",
        "TOOL_ARGUMENT_SENTINEL",
        "TOOL_RESULT_SENTINEL",
        "CREDENTIAL_SENTINEL",
        "RAW_UNKNOWN_CODE_SENTINEL",
    ):
        assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_bridge_reconnect_logs_fixed_diagnostics_and_doubles_backoff(
    tmp_path, monkeypatch, caplog,
):
    sentinel = "BRIDGE_EXCEPTION_TEXT_SENTINEL"
    third_attempt = asyncio.Event()

    class FailingBridge(FakeBridge):
        async def connect(self):
            self.connect_count += 1
            if self.connect_count <= 2:
                raise RuntimeError(sentinel)
            third_attempt.set()

    bridge = FailingBridge()
    client = _bridge_client(tmp_path, bridge, db="bridge_reconnect.db")
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(pcc_mod.asyncio, "sleep", fake_sleep)
    with caplog.at_level(
        logging.WARNING, logger="puffo_agent.agent.puffo_core_client"
    ):
        task = asyncio.create_task(client._listen_bridge())
        await asyncio.wait_for(third_attempt.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert delays == [pcc_mod.INITIAL_BACKOFF, pcc_mod.INITIAL_BACKOFF * 2]
    assert bridge.connect_count >= 3
    assert bridge.close_count == bridge.connect_count
    assert "bot-0001" in caplog.text
    assert "category=bridge_transport" in caplog.text
    assert "exception=RuntimeError" in caplog.text
    assert "retry_delay=1s" in caplog.text
    assert "retry_delay=2s" in caplog.text
    assert sentinel not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    await client.store.close()


# --------------------------------------------------------------------------
# (d) native untouched
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d_native_send_message_still_encrypts_and_posts(tmp_path, monkeypatch):
    enc_calls: list[int] = []

    def _enc_spy(inp, signing_key, *, now_ms=None):
        enc_calls.append(1)
        return ({"envelope_id": "msg_native", "type": "message_envelope"}, b"ck")

    monkeypatch.setattr(sc_mod, "encrypt_message_with_content_key", _enc_spy)

    ks = _native_keystore(tmp_path)
    http = FakeHttp()
    http.responses["/certs/sync?slugs=alice-0001"] = {
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
    _install_native_send_coordinator(cfg)
    tools = build_dispatch(cfg)

    result = await tools["send_message"](channel="@alice-0001", text="hi native")

    assert enc_calls, "native send_message must still encrypt"
    assert any(p == "/messages" for m, p, _ in http.calls if m == "POST"), (
        f"native send must POST via http_client; calls={http.calls}"
    )
    assert result["state"] == "sent"
    assert "posted" in result["note"]


class _RecordingWs:
    """PuffoCoreWsClient stand-in: captures ``on_message`` (the native
    ``handle_envelope`` closure) and no-ops ``run()`` so ``listen()``
    returns instead of blocking."""

    instances: list[_RecordingWs] = []

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
        catchup_stale_hours=0,
    )  # no bridge → native

    async def _empty_get(path, *a, **k):
        return {}

    client.http.get = _empty_get  # type: ignore[method-assign]

    async def _fake_signing_keys(slug):
        return [object()]  # one non-empty pubkey so the decrypt loop runs

    client._key_cache.get_signing_keys = _fake_signing_keys  # type: ignore[method-assign]

    # Native listen() wires handle_envelope onto the (recording) WS and
    # returns because run() is a no-op.
    await client.listen()
    assert len(_RecordingWs.instances) == 1
    handle_envelope = _RecordingWs.instances[0].on_message
    assert handle_envelope is not None

    await handle_envelope({
        "seq": 1,
        "envelope": {
            "envelope_id": "env_native_in",
            "sender_slug": "alice-0001",
        },
    })

    assert decrypt_calls, "native inbound must call decrypt_message"
    row = await client.store.get_message_by_envelope("env_native_in")
    assert row is not None
    assert row.content["text"] == "decrypted body"


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
        sc_mod, "encrypt_message_with_content_key",
        lambda *a, **k: enc_calls.append(1),
    )
    monkeypatch.setattr(
        sc_mod, "encrypt_attachment", lambda *a, **k: enc_calls.append(1),
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
    assert result["state"] == "sent"
    assert result["envelope_id"] == "msg_bridgeack"


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
    done = asyncio.Event()

    await _drive_listen_until(client, done=done)

    # Fetched by blob_id, no decrypt.
    assert bridge.downloaded == [BLOB]
    saved = tmp_path / ".puffo" / "inbox" / "env_att_in" / "report.txt"
    assert saved.is_file()
    assert saved.read_bytes() == DATA
    msg = await client.store.get_message_by_envelope("env_att_in")
    assert msg is not None
    assert str(saved) in msg.content["attachment_paths"]


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
    done = asyncio.Event()

    await _drive_listen_until(client, done=done)

    # The backfill drive fired exactly one fetch_pending.
    assert bridge.fetch_pending_count == 1
    assert bridge.downloaded == [BLOB]
    saved = tmp_path / ".puffo" / "inbox" / "env_att_bf" / "bf.txt"
    assert saved.is_file() and saved.read_bytes() == DATA
    msg = await client.store.get_message_by_envelope("env_att_bf")
    assert msg is not None
    assert str(saved) in msg.content["attachment_paths"]


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
    done = asyncio.Event()

    await _drive_listen_until(client, done=done, notifications=2)

    # Download was attempted then skipped.
    assert bridge.downloaded == ["blob_missing"]
    # Both messages delivered — the loop survived the missing blob.
    missing = await client.store.get_message_by_envelope("env_missing_blob")
    assert missing is not None
    assert await client.store.get_message_by_envelope("env_after_missing") is not None
    # The message with the missing blob carries no attachment path.
    assert missing.content["attachment_paths"] == []
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

    monkeypatch.setattr(sc_mod, "encrypt_attachment", _enc_att_spy)
    monkeypatch.setattr(sc_mod, "encrypt_message_with_content_key", _enc_msg_spy)

    ks = _native_keystore(tmp_path)
    http = FakeHttp()
    http.responses["/blobs/upload"] = {"blob_id": "blob_native_att"}
    http.responses["/certs/sync?slugs=alice-0001"] = {
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
    _install_native_send_coordinator(cfg)
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
    assert result["state"] == "sent"
    assert "uploaded" in result["note"]


# --------------------------------------------------------------------------
# (F1) handled bridge messages are acked exactly once.
# --------------------------------------------------------------------------


async def _open_dispatch_client(tmp_path, bridge, *, db):
    """Open a bridge client without spinning up the full listen loop."""
    client = _bridge_client(tmp_path, bridge, db=db)
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

    await _drive_listen_until(client, done=done)
    # Drain any ack task still in flight after the loop was cancelled.
    if client._ack_tasks:
        await asyncio.gather(*client._ack_tasks, return_exceptions=True)

    assert bridge.acked == [[ENV_ID]]


@pytest.mark.asyncio
async def test_f1_failing_message_handling_skips_ack(tmp_path):
    """A frame whose handling raises is NOT acked — the ack sits inside
    the handling ``try`` after a clean ``_store_bridge_payload``, so a
    raise skips it and the server redelivers."""
    ENV_ID = "env_ack_fail"
    bridge = FakeBridge()
    client = await _open_dispatch_client(tmp_path, bridge, db="ack_fail.db")

    async def _boom(payload, **_kwargs):
        raise RuntimeError("handling blew up")

    client._store_bridge_payload = _boom  # type: ignore[method-assign]

    # _dispatch_bridge_frame swallows the handling exception (logs it).
    await client._dispatch_bridge_frame(_bridge_message_frame(ENV_ID))
    await asyncio.sleep(0)

    assert client._ack_tasks == set()
    assert bridge.acked == []


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
    done = asyncio.Event()

    await _drive_listen_until(client, done=done)

    inbox = (tmp_path / ".puffo" / "inbox" / "env_f6").resolve()
    saved = inbox / "escape"
    assert saved.is_file(), "sanitised file must land inside the inbox dir"
    assert saved.read_bytes() == DATA
    # The written path resolves inside the inbox — no ../ escape.
    assert str(saved.resolve()).startswith(str(inbox))
    # The pre-fix bug would have written one level up (a sibling of the
    # envelope dir); assert that escaped location does NOT exist.
    assert not (tmp_path / ".puffo" / "inbox" / "escape").exists()
    # Durable model-facing content points at the sanitised in-inbox file.
    msg = await client.store.get_message_by_envelope("env_f6")
    assert msg is not None
    assert str(saved) in msg.content["attachment_paths"]


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


# --------------------------------------------------------------------------
# added_to_space: server push on Space add triggers an eager spaces refresh
# --------------------------------------------------------------------------


def _persist_space_spy(monkeypatch):
    """Divert ``disk_cache.persist_space`` into a recorder so the tests
    neither write into the real ``home_dir()`` cache nor depend on it."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pcc_mod.disk_cache, "persist_space",
        lambda sid, name: calls.append((sid, name)),
    )
    return calls


@pytest.mark.asyncio
async def test_added_to_space_triggers_list_spaces_refresh(
    tmp_path, monkeypatch,
):
    """An inbound ``{"type": "added_to_space", "space_id": ...}`` frame
    (server ``AgentServerMsg::AddedToSpace``) schedules exactly one
    ``list_spaces`` re-issue off the dispatcher — async, never awaited
    inline (which would deadlock ``frames()``) — and the reply warms the
    space-name + disk caches so the new space is in the known set."""
    persisted = _persist_space_spy(monkeypatch)
    bridge = FakeBridge(spaces=[{"id": "sp_new", "name": "New Space"}])
    client = await _open_dispatch_client(tmp_path, bridge, db="ats.db")

    await client._dispatch_bridge_frame(
        {"type": "added_to_space", "space_id": "sp_new"},
    )
    # Scheduled, not awaited inline: one task in flight after dispatch.
    assert len(client._ack_tasks) == 1
    await asyncio.gather(*client._ack_tasks)

    assert bridge.list_spaces_count == 1
    assert client._space_name_cache["sp_new"] == "New Space"
    assert ("sp_new", "New Space") in persisted
    # The done-callback cleaned the task set up afterwards.
    assert client._ack_tasks == set()


@pytest.mark.asyncio
async def test_added_to_space_duplicate_is_harmless_relist(
    tmp_path, monkeypatch,
):
    """A duplicate ``added_to_space`` for an already-known space is a
    harmless re-list: the refresh runs again but ``setdefault`` keeps the
    existing cache entry, and nothing raises."""
    _persist_space_spy(monkeypatch)
    bridge = FakeBridge(spaces=[{"id": "sp_known", "name": "Server Name"}])
    client = await _open_dispatch_client(tmp_path, bridge, db="ats_dup.db")
    client._space_name_cache["sp_known"] = "Cached Name"

    for _ in range(2):
        await client._dispatch_bridge_frame(
            {"type": "added_to_space", "space_id": "sp_known"},
        )
        await asyncio.gather(*client._ack_tasks)

    assert bridge.list_spaces_count == 2
    # setdefault: the pre-existing entry survives the duplicate push.
    assert client._space_name_cache["sp_known"] == "Cached Name"
    assert client._ack_tasks == set()


@pytest.mark.asyncio
async def test_added_to_space_missing_space_id_and_failed_refresh_survive(
    tmp_path, monkeypatch, caplog,
):
    """Malformed push (no ``space_id``) still refreshes without raising,
    and a refresh whose ``list_spaces`` blows up logs + drops instead of
    crashing the dispatch loop (fail-soft, like the F1 ack)."""
    _persist_space_spy(monkeypatch)
    bridge = FakeBridge(spaces=[{"id": "sp_1", "name": "One"}])
    client = await _open_dispatch_client(tmp_path, bridge, db="ats_bad.db")

    # Missing space_id → refresh still runs, nothing raises.
    await client._dispatch_bridge_frame({"type": "added_to_space"})
    await asyncio.gather(*client._ack_tasks)
    assert bridge.list_spaces_count == 1
    assert client._space_name_cache.get("sp_1") == "One"

    # list_spaces failure → warning, loop survives.
    async def _boom(*, timeout=30.0):
        raise RuntimeError("bridge fell over")

    bridge.send_list_spaces = _boom  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        await client._dispatch_bridge_frame(
            {"type": "added_to_space", "space_id": "sp_2"},
        )
        await asyncio.gather(*client._ack_tasks)
    assert "bridge spaces refresh failed" in caplog.text
    assert client._ack_tasks == set()


@pytest.mark.asyncio
async def test_added_to_space_over_full_listen_loop(tmp_path, monkeypatch):
    """End-to-end over ``_listen_bridge``: a live ``added_to_space``
    frame drives the ``list_spaces`` refresh on the real loop (not just a
    direct dispatch call)."""
    persisted = _persist_space_spy(monkeypatch)

    class _RefreshBridge(FakeBridge):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.refreshed = asyncio.Event()

        async def send_list_spaces(self, *, timeout: float = 30.0) -> dict:
            resp = await super().send_list_spaces(timeout=timeout)
            self.refreshed.set()
            return resp

    bridge = _RefreshBridge(
        scripted=[{"type": "added_to_space", "space_id": "sp_live"}],
        spaces=[{"id": "sp_live", "name": "Live Space"}],
    )
    client = _bridge_client(tmp_path, bridge, db="ats_live.db")

    await _drive_listen_until(client, done=bridge.refreshed)
    if client._ack_tasks:
        await asyncio.gather(*client._ack_tasks, return_exceptions=True)

    assert bridge.list_spaces_count == 1
    assert client._space_name_cache.get("sp_live") == "Live Space"
    assert ("sp_live", "Live Space") in persisted
