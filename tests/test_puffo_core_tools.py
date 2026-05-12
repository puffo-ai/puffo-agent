import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.keystore import KeyStore, Session, StoredIdentity, encode_secret
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair
from puffo_agent.mcp.puffo_core_tools import (
    PuffoCoreToolsConfig,
    register_core_tools,
)


def _now_ms():
    return int(time.time() * 1000)


class FakeHttpClient:
    """Test stub. Match priority: exact path, then path-without-query,
    then query params modulo the ``since`` cursor (so a test can
    register one canonical key and match the variants the real client
    sends).
    """
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses: dict[str, dict] = {}

    def _match(self, path: str) -> dict:
        if path in self.responses:
            return self.responses[path]
        base = path.split("?", 1)[0]
        if base in self.responses:
            return self.responses[base]
        if "?" in path:
            from urllib.parse import parse_qsl
            actual_qs = sorted(
                (k, v) for k, v in parse_qsl(path.split("?", 1)[1], keep_blank_values=True)
                if k != "since"
            )
            for key in self.responses:
                if "?" not in key:
                    continue
                key_base, key_qs = key.split("?", 1)
                if key_base != base:
                    continue
                if sorted(parse_qsl(key_qs, keep_blank_values=True)) == actual_qs:
                    return self.responses[key]
        return {}

    async def get(self, path):
        self.calls.append(("GET", path, None))
        return self._match(path)

    async def post(self, path, body=None):
        self.calls.append(("POST", path, body))
        if path in self.responses:
            return self.responses[path]
        return {"ok": True}

    async def _ensure_subkey(self):
        pass


def _setup():
    d = tempfile.mkdtemp()
    ks = KeyStore(os.path.join(d, "keys"))
    device_key = Ed25519KeyPair.generate()
    subkey = Ed25519KeyPair.generate()
    identity = StoredIdentity(
        slug="agent-0001",
        device_id="dev_test",
        root_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
        device_signing_secret_key=encode_secret(device_key.secret_bytes()),
        kem_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
        server_url="http://localhost:3000",
    )
    ks.save_identity(identity)
    session = Session(
        slug="agent-0001",
        subkey_id="sk_test",
        subkey_secret_key=encode_secret(subkey.secret_bytes()),
        expires_at=_now_ms() + 3_600_000,
    )
    ks.save_session(session)

    ms = MessageStore(os.path.join(d, "messages.db"))
    http = FakeHttpClient()

    cfg = PuffoCoreToolsConfig(
        slug="agent-0001",
        device_id="dev_test",
        keystore=ks,
        http_client=http,
        # MessageStore is duck-compatible with DataClient (same three
        # methods + return shapes), so tests skip the loopback HTTP
        # round-trip and read SQLite directly.
        data_client=ms,
        space_id="sp_test",
    )
    return cfg, http, ms


def _build_tools(cfg):
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test")
    register_core_tools(mcp, cfg)
    return mcp


async def _call(mcp, name, args=None):
    result = await mcp.call_tool(name, args or {})
    if isinstance(result, list):
        return "".join(
            getattr(item, "text", str(item)) for item in result
        )
    return str(result)


@pytest.mark.asyncio
async def test_whoami():
    cfg, _, _ = _setup()
    mcp = _build_tools(cfg)
    result = await _call(mcp, "whoami")
    assert "agent-0001" in result
    assert "dev_test" in result
    assert "sk_test" in result


@pytest.mark.asyncio
async def test_send_message_channel():
    cfg, http, _ = _setup()
    recipient_kem = KemKeyPair.generate()
    # Channel reply: members from /spaces/<sp>/channels/<ch>/members,
    # device certs from /certs/sync.
    http.responses["/spaces/sp_test/channels/ch_abc/members"] = {
        "members": [{"slug": "alice-0001", "role": "owner"}],
    }
    http.responses["/certs/sync?slugs=alice-0001"] = {
        "entries": [{
            "seq": 1,
            "kind": "device_cert",
            "slug": "alice-0001",
            "cert": {
                "device_id": "dev_recipient_1",
                "kem_public_key": base64url_encode(recipient_kem.public_key_bytes()),
            },
        }],
        "has_more": False,
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {"channel": "ch_abc", "text": "hello world"})
    assert "posted" in result
    assert "ch_abc" in result

    paths = [p for m, p, _ in http.calls if m == "GET"]
    assert any(p == "/spaces/sp_test/channels/ch_abc/members" for p in paths)
    assert any(p.startswith("/certs/sync") for p in paths)

    post_calls = [(p, b) for m, p, b in http.calls if m == "POST"]
    assert len(post_calls) == 1
    path, body = post_calls[0]
    assert path == "/messages"
    # Body IS the envelope; no ``{"envelope": ...}`` wrapper.
    envelope = body
    assert envelope["type"] == "message_envelope"
    assert envelope["version"] == 1
    assert envelope["envelope_kind"] == "channel"
    assert envelope["sender_slug"] == "agent-0001"
    assert envelope["channel_id"] == "ch_abc"
    assert envelope["space_id"] == "sp_test"
    assert "content_ciphertext" in envelope
    assert "content_nonce" in envelope
    assert len(envelope["recipients"]) == 1
    r = envelope["recipients"][0]
    assert r["device_id"] == "dev_recipient_1"
    assert "hpke_enc" in r
    assert "wrapped_content_key" in r


@pytest.mark.asyncio
async def test_send_message_dm():
    cfg, http, _ = _setup()
    recipient_kem = KemKeyPair.generate()
    sender_kem = KemKeyPair.generate()
    # DM fans to recipient + sender's own devices via /certs/sync.
    http.responses["/certs/sync?slugs=agent-0001,alice-0001"] = {
        "entries": [
            {
                "seq": 1, "kind": "device_cert", "slug": "agent-0001",
                "cert": {
                    "device_id": "dev_test",
                    "kem_public_key": base64url_encode(sender_kem.public_key_bytes()),
                },
            },
            {
                "seq": 2, "kind": "device_cert", "slug": "alice-0001",
                "cert": {
                    "device_id": "dev_alice",
                    "kem_public_key": base64url_encode(recipient_kem.public_key_bytes()),
                },
            },
        ],
        "has_more": False,
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {"channel": "@alice-0001", "text": "hey"})
    assert "posted" in result

    post_calls = [(p, b) for m, p, b in http.calls if m == "POST"]
    assert len(post_calls) == 1
    _, body = post_calls[0]
    envelope = body
    assert envelope["envelope_kind"] == "dm"
    assert envelope["recipient_slug"] == "alice-0001"
    # Both devices land in the envelope so other clients of the same
    # sender see the DM too.
    device_ids = {r["device_id"] for r in envelope["recipients"]}
    assert device_ids == {"dev_test", "dev_alice"}


@pytest.mark.asyncio
async def test_send_message_rejects_named_channel():
    """``#name`` addressing isn't supported; the LLM gets a clear error
    pointing at ``list_channels`` instead of a 404 spiral."""
    cfg, _, _ = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(mcp, "send_message", {"channel": "#general", "text": "hi"})
    assert "isn't supported" in str(excinfo.value) or "not supported" in str(excinfo.value)


@pytest.mark.asyncio
async def test_get_channel_history_from_local():
    cfg, http, ms = _setup()
    await ms.open()

    base = _now_ms()
    await ms.store({
        "envelope_id": "env_1", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "Hello from Alice", "sent_at": base,
    })
    await ms.store({
        "envelope_id": "env_2", "envelope_kind": "channel",
        "sender_slug": "bob-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "Hello from Bob", "sent_at": base + 1000,
    })

    mcp = _build_tools(cfg)
    result = await _call(mcp, "get_channel_history", {"channel": "ch_abc", "limit": 10})
    assert "alice-0001" in result
    assert "bob-0001" in result
    assert "Hello from Alice" in result
    assert "Hello from Bob" in result
    await ms.close()


@pytest.mark.asyncio
async def test_get_channel_history_empty():
    cfg, _, ms = _setup()
    await ms.open()
    mcp = _build_tools(cfg)
    result = await _call(mcp, "get_channel_history", {"channel": "ch_nonexistent"})
    assert "no root posts" in result
    await ms.close()


@pytest.mark.asyncio
async def test_list_channels():
    """Channels come from ``/spaces/<sp>/events`` replay — there's no
    standalone channels endpoint on puffo-server."""
    cfg, http, _ = _setup()
    http.responses["/spaces/sp_test/events"] = {
        "events": [
            {"kind": "create_space", "payload": {"space_id": "sp_test", "name": "Test"}},
            {"kind": "create_channel", "payload": {"channel_id": "ch_1", "name": "General"}},
            {"kind": "create_channel", "payload": {"channel_id": "ch_2", "name": "Random"}},
            {"kind": "invite_to_space", "payload": {"invitee_slug": "alice-0001"}},
        ],
        "has_more": False,
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels")
    assert "General" in result
    assert "Random" in result
    assert "ch_1" in result
    assert "ch_2" in result


@pytest.mark.asyncio
async def test_list_channels_paginates_via_since():
    """``list_channels`` walks ``/spaces/<sp>/events`` page-by-page
    using ``?since=<next_cursor>``. Guards against a regression of
    the ``?cursor=`` bug — the server's axum extractor silently
    ignored the wrong-named key, so paginated calls re-fetched the
    first page forever and the tool never returned."""
    cfg, http, _ = _setup()
    # Page 1: one channel + cursor pointing at page 2.
    http.responses["/spaces/sp_test/events"] = {
        "events": [
            {"kind": "create_channel", "payload": {"channel_id": "ch_1", "name": "General"}},
        ],
        "has_more": True,
        "next_cursor": "cursor_page2",
    }
    # Page 2: second channel + end of stream. Registered against the
    # exact ``?since=`` URL so a regression to ``?cursor=`` would miss
    # this entry and fall back to page 1 (loop forever).
    http.responses["/spaces/sp_test/events?since=cursor_page2"] = {
        "events": [
            {"kind": "create_channel", "payload": {"channel_id": "ch_2", "name": "Random"}},
        ],
        "has_more": False,
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels")

    assert "General" in result
    assert "ch_1" in result
    assert "Random" in result
    assert "ch_2" in result

    events_calls = [c for c in http.calls if c[1].startswith("/spaces/sp_test/events")]
    # Exactly two requests: page 1 (no query) + page 2 (?since=).
    assert len(events_calls) == 2, events_calls
    assert events_calls[0][1] == "/spaces/sp_test/events"
    assert "?since=cursor_page2" in events_calls[1][1]


@pytest.mark.asyncio
async def test_list_channels_bails_on_stuck_cursor():
    """If the server ever regresses and returns the same cursor it
    was just sent, the tool must stop instead of spinning. Mirrors
    the strict-advance guard in ``fetchChannelsFromEvents``
    (web) and ``_resolve_channel_name`` (this package)."""
    cfg, http, _ = _setup()
    # Both the initial and the ``?since=stuck`` request hand back
    # the same ``next_cursor`` — a real regression would loop, the
    # guarded loop bails after the second fetch.
    page = {
        "events": [
            {"kind": "create_channel", "payload": {"channel_id": "ch_1", "name": "General"}},
        ],
        "has_more": True,
        "next_cursor": "stuck",
    }
    http.responses["/spaces/sp_test/events"] = page
    http.responses["/spaces/sp_test/events?since=stuck"] = page
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels")

    assert "General" in result
    events_calls = [c for c in http.calls if c[1].startswith("/spaces/sp_test/events")]
    # Two calls: initial, then one with ``?since=stuck`` whose
    # ``next_cursor`` is also ``stuck`` — the guard breaks the loop.
    assert len(events_calls) == 2, events_calls


@pytest.mark.asyncio
async def test_list_channel_members():
    """Channel members come from
    ``/spaces/<sp>/channels/<ch>/members`` keyed by space_id."""
    cfg, http, _ = _setup()
    http.responses["/spaces/sp_test/channels/ch_abc/members"] = {
        "members": [
            {"slug": "alice-0001", "role": "owner"},
            {"slug": "agent-0001", "role": "member"},
        ]
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channel_members", {"channel": "ch_abc"})
    assert "alice-0001" in result
    assert "agent-0001" in result
    # Roles render as ``(owner)`` / ``(member)``.
    assert "(owner)" in result
    assert "(member)" in result


@pytest.mark.asyncio
async def test_get_user_info():
    """Profile lookups go through ``/identities/profiles?slugs=<slug>``.
    """
    cfg, http, _ = _setup()
    http.responses["/identities/profiles?slugs=alice-0001"] = {
        "profiles": [{
            "slug": "alice-0001",
            "username": "Alice",
            "bio": "A test user",
            "avatar_url": None,
            "profile_updated_at": 1700000000000,
        }],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "get_user_info", {"username": "@alice-0001"})
    assert "alice-0001" in result
    assert "Alice" in result
    assert "A test user" in result


@pytest.mark.asyncio
async def test_get_post_from_local():
    cfg, _, ms = _setup()
    await ms.open()
    await ms.store({
        "envelope_id": "env_lookup", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_1",
        "space_id": "sp_1", "content_type": "text/plain",
        "content": "find this message", "sent_at": _now_ms(),
    })
    mcp = _build_tools(cfg)
    result = await _call(mcp, "get_post", {"post_ref": "env_lookup"})
    assert "env_lookup" in result
    assert "alice-0001" in result
    assert "find this message" in result
    await ms.close()


@pytest.mark.asyncio
async def test_get_post_not_found():
    cfg, _, ms = _setup()
    await ms.open()
    mcp = _build_tools(cfg)
    result = await _call(mcp, "get_post", {"post_ref": "env_nonexistent"})
    assert "not found" in result.lower() or "error" in result.lower()
    await ms.close()


@pytest.mark.asyncio
async def test_upload_file_requires_workspace():
    """Without ``cfg.workspace``, upload_file refuses rather than
    silently dropping into a "no agent dir" hole. Real upload path
    is exercised end-to-end against a live daemon."""
    cfg, _, _ = _setup()
    # Fixture leaves cfg.workspace as None.
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc_info:
        await _call(
            mcp, "upload_file", {"paths": ["test.txt"], "channel": "ch_1"},
        )
    assert "workspace" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_fetch_channel_files_stub():
    cfg, _, _ = _setup()
    mcp = _build_tools(cfg)
    result = await _call(mcp, "fetch_channel_files", {"channel": "ch_1"})
    assert "not yet implemented" in result


@pytest.mark.asyncio
async def test_all_9_tools_registered():
    cfg, _, _ = _setup()
    mcp = _build_tools(cfg)
    tool_names = {t.name for t in await mcp.list_tools()}
    expected = {
        "whoami", "send_message", "get_channel_history",
        "list_channels", "list_channel_members", "get_user_info",
        "get_post", "upload_file", "fetch_channel_files",
    }
    assert expected.issubset(tool_names)
