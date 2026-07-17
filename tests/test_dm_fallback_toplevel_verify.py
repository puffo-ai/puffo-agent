"""PR #159 (+ completion) verification: DM replies must post TOP-LEVEL
(no thread_root_id) so they render inline in the linear DM view; CHANNEL
replies must KEEP threading (thread_root_id set).

Covers ALL FOUR reply code paths — {keyless, native} x {send_message tool,
send_fallback_message} — each asserting BOTH:
  - DM      -> top-level (no thread_root_id)
  - channel -> threaded  (thread_root_id set)

Touch points:
  keyless send_message tool      -> puffo_core_tools.py  (~608)
  native  send_message tool      -> puffo_core_tools.py  (~678)
  keyless send_fallback_message  -> puffo_core_client.py (~4058, bridge)
  native  send_fallback_message  -> puffo_core_client.py (~4187)

Mirrors the mocking style of tests/test_cloud_bridge_msgflow.py and
tests/test_puffo_core_tools.py.
"""

from __future__ import annotations

import pytest

import puffo_agent.agent.puffo_core_client as pcc_mod
import puffo_agent.agent._visibility as vis_mod
from puffo_agent.agent.message_store import MessageStore
from puffo_agent.crypto.http_client import PuffoCoreHttpClient

# Fakes/builders from the bridge msgflow suite (keyless send_message +
# bridge fallback).
from test_cloud_bridge_msgflow import (
    FakeBridge,
    _bridge_client,
    _http_sends,
    _native_keystore,
    _tools_cfg,
)
from puffo_agent.portal.ws_local.tool_dispatch import build_dispatch

# Native send_message helpers from the core-tools suite.
from test_puffo_core_tools import (
    _setup,
    _build_tools,
    _call,
    _spy_encrypt_input,
    _seed_channel,
    _now_ms,
)


# ===========================================================================
# PATH 1: keyless send_message MCP tool  (puffo_core_tools.py ~608)
# ===========================================================================

@pytest.mark.asyncio
async def test_keyless_send_message_DM_is_toplevel(tmp_path):
    """DM route: even with a valid, locally-cached root, the keyless
    send omits thread_root_id -> posts TOP-LEVEL (inline)."""
    ms = MessageStore(str(tmp_path / "dm_top.db"))
    await ms.store({
        "envelope_id": "dm_root", "envelope_kind": "dm",
        "sender_slug": "alice-0001", "channel_id": None,
        "space_id": None, "recipient_slug": "bot-0001",
        "content": "root dm", "sent_at": 1_700_000_000_002,
        "thread_root_id": None, "reply_to_id": None,
    })
    cfg = _tools_cfg(tmp_path, bridge=FakeBridge(), data_client=ms)
    tools = build_dispatch(cfg)

    await tools["send_message"](
        channel="@alice-0001", text="reply", root_id="dm_root",
    )
    body = _http_sends(cfg.http_client)[0]
    assert body["recipient_slug"] == "alice-0001"
    assert "thread_root_id" not in body   # top-level
    assert "reply_to_id" not in body


@pytest.mark.asyncio
async def test_keyless_send_message_CHANNEL_stays_threaded(tmp_path):
    """Channel route: threading is preserved -> thread_root_id set."""
    ms = MessageStore(str(tmp_path / "ch_thread.db"))
    await ms.mark_channel_space("ch_xyz", "sp_1")
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_xyz",
        "space_id": "sp_1", "content": "root",
        "sent_at": 1_700_000_000_000,
        "thread_root_id": None, "reply_to_id": None,
    })
    cfg = _tools_cfg(tmp_path, bridge=FakeBridge(), data_client=ms)
    tools = build_dispatch(cfg)

    await tools["send_message"](
        channel="ch_xyz", text="reply", root_id="msg_root",
    )
    body = _http_sends(cfg.http_client)[0]
    assert body["thread_root_id"] == "msg_root"   # threaded
    assert body["reply_to_id"] == "msg_root"


# ===========================================================================
# PATH 2: native send_message MCP tool  (puffo_core_tools.py ~678)
# ===========================================================================

@pytest.mark.asyncio
async def test_native_send_message_DM_is_toplevel(monkeypatch):
    """Native DM send: a cached DM root would survive resolution/validation
    (no channel to fail the same-channel check), yet the DM guard forces
    thread_root_id=None -> TOP-LEVEL."""
    cfg, http, ms = _setup()
    # DM fans to sender + recipient; seed both device certs.
    from puffo_agent.crypto.primitives import KemKeyPair
    from puffo_agent.crypto.encoding import base64url_encode
    a_kem, s_kem = KemKeyPair.generate(), KemKeyPair.generate()
    http.responses["/certs/sync?slugs=agent-0001,alice-0001"] = {
        "entries": [
            {"seq": 1, "kind": "device_cert", "slug": "agent-0001",
             "cert": {"device_id": "dev_test",
                      "kem_public_key": base64url_encode(s_kem.public_key_bytes())}},
            {"seq": 2, "kind": "device_cert", "slug": "alice-0001",
             "cert": {"device_id": "dev_alice",
                      "kem_public_key": base64url_encode(a_kem.public_key_bytes())}},
        ],
        "has_more": False,
    }
    # A real, cached DM root the agent is replying to.
    await ms.store({
        "envelope_id": "dm_root", "envelope_kind": "dm",
        "sender_slug": "alice-0001", "channel_id": None, "space_id": None,
        "recipient_slug": "agent-0001", "content_type": "text/plain",
        "content": "root dm", "sent_at": _now_ms(), "thread_root_id": None,
    })
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "@alice-0001", "text": "dm reply",
        "visibility_level": "human", "root_id": "dm_root",
    })
    assert "posted" in result
    assert captured["inp"].envelope_kind == "dm"
    assert captured["inp"].thread_root_id is None   # top-level


@pytest.mark.asyncio
async def test_native_send_message_CHANNEL_stays_threaded(monkeypatch):
    """Native channel send keeps threading -> thread_root_id set."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(), "thread_root_id": None,
    })
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc", "text": "chan reply",
        "visibility_level": "default", "root_id": "msg_root",
    })
    assert "posted" in result
    assert captured["inp"].envelope_kind == "channel"
    assert captured["inp"].thread_root_id == "msg_root"   # threaded


# ===========================================================================
# PATH 3: keyless / bridge send_fallback_message  (puffo_core_client.py ~4058)
#         (the path cloud/E2B agents actually run)
# ===========================================================================

@pytest.mark.asyncio
async def test_keyless_fallback_DM_is_toplevel(tmp_path):
    """Bridge DM fallback: even with a root_id, the reply drops both
    thread keys -> TOP-LEVEL (this is the primary cloud-agent fix)."""
    bridge = FakeBridge()
    client = _bridge_client(tmp_path, bridge, db="kf_dm.db")
    client._last_dm_sender = "carol-0001"

    await client.send_fallback_message("", "dm reply", root_id="msg_root2")

    assert len(bridge.sent) == 1
    dm = bridge.sent[0]
    assert dm["recipient_slug"] == "carol-0001"
    assert dm["thread_root_id"] is None    # top-level
    assert dm["reply_to_id"] is None


@pytest.mark.asyncio
async def test_keyless_fallback_CHANNEL_stays_threaded(tmp_path):
    """Bridge channel fallback keeps threading -> thread_root_id set."""
    bridge = FakeBridge()
    client = _bridge_client(tmp_path, bridge, db="kf_ch.db")
    await client.store.mark_channel_space("ch_a", "sp_1")

    await client.send_fallback_message("ch_a", "chan reply", root_id="msg_root")

    assert len(bridge.sent) == 1
    chan = bridge.sent[0]
    assert chan["channel_id"] == "ch_a" and chan["space_id"] == "sp_1"
    assert chan["thread_root_id"] == "msg_root"   # threaded
    assert chan["reply_to_id"] == "msg_root"


# ===========================================================================
# PATH 4: native send_fallback_message  (puffo_core_client.py ~4187)
# ===========================================================================

def _capture_native_fallback_encrypt(monkeypatch):
    """Capture the EncryptInput handed to encrypt_message on the native
    fallback path; also short-circuit visibility (no network)."""
    captured: list = []

    def _fake_encrypt(inp, signing_key):
        captured.append(inp)
        return {"envelope_id": "msg_captured"}

    monkeypatch.setattr(pcc_mod, "encrypt_message", _fake_encrypt)

    async def _fake_vis(level, channel_ref, text, root_id, http):
        return True, ""

    monkeypatch.setattr(vis_mod, "resolve_visibility", _fake_vis)
    return captured


def _native_client(tmp_path, db):
    ks = _native_keystore(tmp_path)
    http = PuffoCoreHttpClient("http://127.0.0.1:1", ks, "bot-0001")
    return pcc_mod.PuffoCoreMessageClient(
        slug="bot-0001", device_id="dev_test", space_id="sp_home",
        keystore=ks, http_client=http,
        message_store=MessageStore(str(tmp_path / db)),
        workspace="", bridge_client=None,   # native (no bridge)
    )


@pytest.mark.asyncio
async def test_native_fallback_DM_is_toplevel(tmp_path, monkeypatch):
    """Native DM fallback: thread_root_id on the outgoing envelope is
    None -> TOP-LEVEL even though a root_id was supplied."""
    captured = _capture_native_fallback_encrypt(monkeypatch)
    client = _native_client(tmp_path, "nfb_dm.db")

    async def _devices(_slugs):
        return [{"slug": "alice-0001", "device_id": "d1"}]

    async def _post(path, body=None):
        return {}

    client._fetch_device_keys = _devices          # type: ignore[assignment]
    client.http.post = _post                       # type: ignore[assignment]
    client._last_dm_sender = "alice-0001"

    await client.send_fallback_message("", "dm reply", root_id="msg_root2")

    assert len(captured) == 1
    assert captured[0].envelope_kind == "dm"
    assert captured[0].thread_root_id is None   # top-level


@pytest.mark.asyncio
async def test_native_fallback_CHANNEL_stays_threaded(tmp_path, monkeypatch):
    """Native channel fallback: thread_root_id preserved -> threaded."""
    captured = _capture_native_fallback_encrypt(monkeypatch)
    client = _native_client(tmp_path, "nfb_ch.db")
    await client.store.mark_channel_space("ch_a", "sp_1")
    client._channel_space["ch_a"] = "sp_1"

    async def _members(path, *a, **k):
        return {"members": [{"slug": "alice-0001"}, {"slug": "bot-0001"}]}

    async def _devices(_slugs):
        return [{"slug": "alice-0001", "device_id": "d1"}]

    async def _post(path, body=None):
        return {}

    client.http.get = _members                     # type: ignore[assignment]
    client._fetch_device_keys = _devices           # type: ignore[assignment]
    client.http.post = _post                        # type: ignore[assignment]

    await client.send_fallback_message("ch_a", "chan reply", root_id="msg_root")

    assert len(captured) == 1
    assert captured[0].envelope_kind == "channel"
    assert captured[0].thread_root_id == "msg_root"   # threaded


# ===========================================================================
# PATH 5: native send_message_with_attachments  (puffo_core_tools.py ~1411)
# ===========================================================================

@pytest.mark.asyncio
async def test_native_attachments_DM_is_toplevel(monkeypatch, tmp_path):
    """Native attachment DM send: a cached DM root would survive validation
    (no channel to fail the same-channel check), yet the DM guard forces
    thread_root_id=None -> TOP-LEVEL."""
    cfg, http, ms = _setup()
    cfg.workspace = tmp_path
    (tmp_path / "hello.txt").write_bytes(b"hello attachments")
    http.responses["/blobs/upload"] = {"blob_id": "blob_xyz"}
    # DM fans to sender + recipient; seed both device certs.
    from puffo_agent.crypto.primitives import KemKeyPair
    from puffo_agent.crypto.encoding import base64url_encode
    a_kem, s_kem = KemKeyPair.generate(), KemKeyPair.generate()
    http.responses["/certs/sync?slugs=agent-0001,alice-0001"] = {
        "entries": [
            {"seq": 1, "kind": "device_cert", "slug": "agent-0001",
             "cert": {"device_id": "dev_test",
                      "kem_public_key": base64url_encode(s_kem.public_key_bytes())}},
            {"seq": 2, "kind": "device_cert", "slug": "alice-0001",
             "cert": {"device_id": "dev_alice",
                      "kem_public_key": base64url_encode(a_kem.public_key_bytes())}},
        ],
        "has_more": False,
    }
    await ms.store({
        "envelope_id": "dm_root", "envelope_kind": "dm",
        "sender_slug": "alice-0001", "channel_id": None, "space_id": None,
        "recipient_slug": "agent-0001", "content_type": "text/plain",
        "content": "root dm", "sent_at": _now_ms(), "thread_root_id": None,
    })
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message_with_attachments", {
        "paths": ["hello.txt"],
        "channel": "@alice-0001",
        "visibility_level": "human",
        "root_id": "dm_root",
        "caption": "dm files",
    })
    assert "uploaded" in result
    assert captured["inp"].envelope_kind == "dm"
    assert captured["inp"].thread_root_id is None   # top-level


@pytest.mark.asyncio
async def test_native_attachments_CHANNEL_stays_threaded(monkeypatch, tmp_path):
    """Native attachment channel send keeps threading -> thread_root_id set."""
    cfg, http, ms = _setup()
    cfg.workspace = tmp_path
    (tmp_path / "hello.txt").write_bytes(b"hello attachments")
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    http.responses["/blobs/upload"] = {"blob_id": "blob_xyz"}
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(), "thread_root_id": None,
    })
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message_with_attachments", {
        "paths": ["hello.txt"],
        "channel": "ch_abc",
        "visibility_level": "default",
        "root_id": "msg_root",
        "caption": "files",
    })
    assert "uploaded" in result
    assert captured["inp"].envelope_kind == "channel"
    assert captured["inp"].thread_root_id == "msg_root"   # threaded
