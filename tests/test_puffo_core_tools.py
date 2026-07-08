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
from puffo_agent.agent._visibility import resolve_visibility
from puffo_agent.mcp.puffo_core_tools import (
    PuffoCoreToolsConfig,
    _resolve_root_id,
    _validate_root_same_channel,
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

    async def post_bytes(self, path, headers=None, data=None):
        """``send_message_with_attachments`` uploads each file via
        ``POST /blobs/upload`` before encrypting the message
        envelope; the integration tests below need this stub so the
        upload step doesn't AttributeError on the way to the
        envelope path. Return the canned response when set."""
        self.calls.append(("POST_BYTES", path, len(data) if data else 0))
        if path in self.responses:
            return self.responses[path]
        return {"blob_id": "blob_stub", "ok": True}

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


class KeylessFakeHttpClient:
    """Recording stub for the T23 keyless transport. ``keyless=True``
    flips ``PuffoCoreToolsConfig.keyless`` so the tools take the unsigned
    ``/v2/cloud-agents/*`` seam. Records every unsigned call and mints a
    fresh blob_id per upload. The signed ``get``/``post``/``post_bytes``
    methods are deliberately ABSENT so any accidental signed call fails
    loud (proving keyless tools never hit the signed path)."""

    def __init__(self, server_url: str = "http://sandbox.local"):
        self.keyless = True
        self.server_url = server_url
        self.calls: list[tuple[str, str, object]] = []
        self.responses: dict[str, dict] = {}
        self.uploaded: list[bytes] = []
        self._blob_seq = 0

    def _match(self, path: str) -> dict:
        if path in self.responses:
            return self.responses[path]
        base = path.split("?", 1)[0]
        return self.responses.get(base, {})

    async def get_unsigned(self, path):
        self.calls.append(("GET_UNSIGNED", path, None))
        return self._match(path)

    async def post_unsigned(self, path, body=None):
        self.calls.append(("POST_UNSIGNED", path, body))
        if path in self.responses:
            return self.responses[path]
        return {"envelope_id": "msg_keyless"}

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


class _SpyKeyStore:
    """Records any keystore load so a keyless tool that accidentally
    reaches the keystore is caught. Both loads raise, mirroring the
    ``_BridgeNoKeysStore`` dead-end."""

    def __init__(self):
        self.loads: list[tuple[str, str]] = []

    def load_identity(self, slug):
        self.loads.append(("identity", slug))
        raise AssertionError("keyless tool must not load identity")

    def load_session(self, slug):
        self.loads.append(("session", slug))
        raise AssertionError("keyless tool must not load session")


def _setup_keyless():
    """Keyless tools config: recording keyless http client + a real
    MessageStore, and NO keystore identity/session written to disk —
    proving the keyless tools never touch the keystore."""
    d = tempfile.mkdtemp()
    ks = KeyStore(os.path.join(d, "keys"))
    ms = MessageStore(os.path.join(d, "messages.db"))
    http = KeylessFakeHttpClient()
    cfg = PuffoCoreToolsConfig(
        slug="agent-0001",
        device_id="dev_test",
        keystore=ks,
        http_client=http,
        data_client=ms,
        space_id="sp_test",
    )
    return cfg, http, ms


def _keyless_sends(http):
    """The bodies of every keyless ``POST /v2/cloud-agents/messages``."""
    return [b for m, p, b in http.calls if m == "POST_UNSIGNED"]


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
async def test_whoami_includes_display_name():
    cfg, http, _ = _setup()
    http.responses["/identities/profiles?slugs=agent-0001"] = {
        "profiles": [{"slug": "agent-0001", "display_name": "Helper Bot"}],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "whoami")
    assert "display_name: Helper Bot" in result
    assert "agent-0001" in result


@pytest.mark.asyncio
async def test_send_message_channel():
    cfg, http, ms = _setup()
    recipient_kem = KemKeyPair.generate()
    # Channel reply: members from /spaces/<sp>/channels/<ch>/members,
    # device certs from /certs/sync.
    # Pre-cache the channel→space mapping the way an inbound message
    # would: send_message now resolves space via the local cache, then
    # via /spaces walking, and refuses to fall back to cfg.space_id.
    await ms.mark_channel_space("ch_abc", "sp_test")
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
    result = await _call(
        mcp,
        "send_message",
        {"channel": "ch_abc", "text": "hello world", "visibility_level": "human"},
    )
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
async def test_send_message_root_level_false_coerced():
    """A root-level send with visibility_level='default' still posts —
    the flag is coerced to visible and the tool response carries a
    note so the agent learns on the spot."""
    cfg, http, ms = _setup()
    recipient_kem = KemKeyPair.generate()
    # Pre-cache the channel→space mapping the way an inbound message
    # would: send_message now resolves space via the local cache, then
    # via /spaces walking, and refuses to fall back to cfg.space_id.
    await ms.mark_channel_space("ch_abc", "sp_test")
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
    result = await _call(
        mcp,
        "send_message",
        {"channel": "ch_abc", "text": "agent chatter", "visibility_level": "default"},
    )
    # Message still went out (warning, not error).
    assert "posted" in result
    assert len([1 for m, _, _ in http.calls if m == "POST"]) == 1
    # ...and the agent is told the flag was ignored.
    assert "hidden ignored" in result


@pytest.mark.asyncio
async def test_send_message_threaded_false_not_coerced():
    """A threaded reply with visibility_level='default' and no
    @-mention stays hidden — no coerce; the tool result carries
    the "be explicit" nudge note instead."""
    cfg, http, ms = _setup()
    recipient_kem = KemKeyPair.generate()
    # Pre-cache the channel→space mapping the way an inbound message
    # would: send_message now resolves space via the local cache, then
    # via /spaces walking, and refuses to fall back to cfg.space_id.
    await ms.mark_channel_space("ch_abc", "sp_test")
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
    result = await _call(
        mcp,
        "send_message",
        {
            "channel": "ch_abc",
            "text": "agent-to-agent reply",
            "visibility_level": "default",
            "root_id": "msg_root_abc",
        },
    )
    assert "posted" in result
    assert "ignored" not in result
    # Nudge note fires: level was default with no signal.
    assert "sent hidden" in result
    assert "'human'" in result and "'agent_only'" in result


@pytest.mark.asyncio
async def test_send_message_human_no_notes():
    """visibility_level='human' — visible send, no notes."""
    cfg, http, ms = _setup()
    recipient_kem = KemKeyPair.generate()
    await ms.mark_channel_space("ch_abc", "sp_test")
    http.responses["/spaces/sp_test/channels/ch_abc/members"] = {
        "members": [{"slug": "alice-0001", "role": "owner"}],
    }
    http.responses["/certs/sync?slugs=alice-0001"] = {
        "entries": [{
            "seq": 1, "kind": "device_cert", "slug": "alice-0001",
            "cert": {
                "device_id": "dev_recipient_1",
                "kem_public_key": base64url_encode(recipient_kem.public_key_bytes()),
            },
        }],
        "has_more": False,
    }
    mcp = _build_tools(cfg)
    result = await _call(
        mcp,
        "send_message",
        {
            "channel": "ch_abc",
            "text": "answer for the operator",
            "visibility_level": "human",
        },
    )
    assert "posted" in result
    # No visibility note appended (level was explicit).
    assert "sent visible" not in result
    assert "sent hidden" not in result
    assert "hidden ignored" not in result


@pytest.mark.asyncio
async def test_send_message_agent_only_dm_stays_hidden_with_warning():
    """visibility_level='agent_only' + DM: floor respects the opt-out
    (hidden) but the tool result warns that this looks human-targeted
    so the agent can reconsider without being overridden."""
    cfg, http, ms = _setup()
    recipient_kem = KemKeyPair.generate()
    http.responses["/certs/sync?slugs=agent-0001,alice-0001"] = {
        "entries": [
            {
                "seq": 1, "kind": "device_cert", "slug": "agent-0001",
                "cert": {
                    "device_id": "dev_self",
                    "kem_public_key": base64url_encode(recipient_kem.public_key_bytes()),
                },
            },
            {
                "seq": 2, "kind": "device_cert", "slug": "alice-0001",
                "cert": {
                    "device_id": "dev_recipient_1",
                    "kem_public_key": base64url_encode(recipient_kem.public_key_bytes()),
                },
            },
        ],
        "has_more": False,
    }
    mcp = _build_tools(cfg)
    result = await _call(
        mcp,
        "send_message",
        {
            "channel": "@alice-0001",
            "text": "internal ping",
            "visibility_level": "agent_only",
            "root_id": "msg_root_dm",
        },
    )
    # NB: msg_root_dm isn't in local cache, so validate_root wipes it
    # with its own warning note — assert the visibility warning is
    # present alongside, don't insist on it being alone.
    assert "posted" in result
    assert "sent hidden per" in result
    assert "DM" in result
    assert "Double-check" in result


@pytest.mark.asyncio
async def test_send_message_uses_cached_space_for_cross_space_channel():
    """send_message resolves channel→space from the local cache —
    which is filled by membership events as they arrive over the WS
    (see ``puffo_core_client._handle_event``). A channel that lives
    in a non-home space must still get its members call routed to
    the correct space, with no ``cfg.space_id`` fallback in sight.
    """
    cfg, http, ms = _setup()
    recipient_kem = KemKeyPair.generate()
    # Pre-cache the mapping the way an ``accept_channel_invite`` /
    # ``invite_to_channel`` / ``create_channel`` event would.
    await ms.mark_channel_space("ch_elsewhere", "sp_other")
    http.responses["/spaces/sp_other/channels/ch_elsewhere/members"] = {
        "members": [{"slug": "alice-0001", "role": "member"}],
    }
    http.responses["/certs/sync?slugs=alice-0001"] = {
        "entries": [{
            "seq": 1, "kind": "device_cert", "slug": "alice-0001",
            "cert": {
                "device_id": "dev_recipient_1",
                "kem_public_key": base64url_encode(recipient_kem.public_key_bytes()),
            },
        }],
        "has_more": False,
    }
    mcp = _build_tools(cfg)
    result = await _call(
        mcp, "send_message",
        {
            "channel": "ch_elsewhere",
            "text": "hello other space",
            "visibility_level": "human",
        },
    )
    assert "posted" in result, f"expected success, got: {result}"
    members_paths = [
        path for method, path, _ in http.calls
        if method == "GET" and "ch_elsewhere/members" in path
    ]
    assert any("/spaces/sp_other/" in p for p in members_paths), (
        f"members call must target sp_other, got: {members_paths}"
    )
    assert not any("/spaces/sp_test/" in p for p in members_paths), (
        f"must NOT hit sp_test (wrong-space fallback regression): {members_paths}"
    )
    # And critically: no /spaces walking — the cache should be the
    # only authority. (Pre-cache fix removed the FB-76-era resolver
    # that walked /spaces + /spaces/<sp>/channels.)
    assert not any(
        path == "/spaces" for method, path, _ in http.calls
        if method == "GET"
    ), "no /spaces walk should occur — cache lookup is the only path"


@pytest.mark.asyncio
async def test_send_message_fails_loud_on_cache_miss():
    """A channel the agent has no cached mapping for produces a
    clear MCP error — no walking ``/spaces`` as a guess, no falling
    back to ``cfg.space_id``. The agent's source of truth for
    channel→space is the event stream; if no event fed the cache,
    the agent isn't a member and shouldn't be sending."""
    cfg, http, _ = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(
            mcp, "send_message",
            {
                "channel": "ch_nowhere",
                "text": "should not send",
                "visibility_level": "human",
            },
        )
    assert "no record of channel" in str(excinfo.value), (
        f"expected a cache-miss error, got: {excinfo.value}"
    )
    # No members call, no /spaces walk — the resolver bailed before
    # any HTTP.
    assert not any(
        "ch_nowhere/members" in path or path == "/spaces"
        for method, path, _ in http.calls
        if method == "GET"
    ), f"must not issue HTTP on cache miss; calls={http.calls}"


@pytest.mark.asyncio
async def test_list_channel_members_fails_loud_on_cache_miss():
    """list_channel_members reads the cache too — miss = clear error,
    no fallback to ``cfg.space_id``."""
    cfg, http, _ = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(mcp, "list_channel_members", {"channel": "ch_unknown"})
    assert "no record of channel" in str(excinfo.value)
    assert not any(
        "ch_unknown/members" in path
        for method, path, _ in http.calls
        if method == "GET"
    ), "must not issue a members call when cache misses"


# ── bare user slug passed where a channel id belongs → distinct
# actionable error (not the generic membership cache-miss).


def _assert_dm_hint(exc: Exception, slug: str) -> None:
    msg = str(exc)
    assert "not a channel id" in msg, f"expected slug-hint error, got: {msg}"
    assert f"@{slug}" in msg
    assert "get_dm_history" in msg


@pytest.mark.asyncio
async def test_send_message_bare_slug_gets_dm_hint():
    cfg, http, _ = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(
            mcp, "send_message",
            {"channel": "alice-1234", "text": "hi", "visibility_level": "human"},
        )
    _assert_dm_hint(excinfo.value, "alice-1234")
    assert not http.calls, "must bail before any HTTP"


@pytest.mark.asyncio
async def test_list_channel_members_bare_slug_gets_dm_hint():
    cfg, _, _ = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(mcp, "list_channel_members", {"channel": "alice-1234"})
    _assert_dm_hint(excinfo.value, "alice-1234")


@pytest.mark.asyncio
async def test_leave_channel_bare_slug_gets_dm_hint():
    cfg, _, _ = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(mcp, "leave_channel", {"channel_id": "alice-1234"})
    _assert_dm_hint(excinfo.value, "alice-1234")


@pytest.mark.asyncio
async def test_send_message_with_attachments_bare_slug_gets_dm_hint():
    cfg, _, _ = _setup()
    d = tempfile.mkdtemp()
    cfg.workspace = d
    with open(os.path.join(d, "note.txt"), "w", encoding="utf-8") as f:
        f.write("hello")
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(
            mcp, "send_message_with_attachments",
            {"paths": ["note.txt"], "channel": "alice-1234"},
        )
    _assert_dm_hint(excinfo.value, "alice-1234")


@pytest.mark.asyncio
async def test_get_channel_history_bare_slug_gets_dm_hint():
    """The local-store read path would otherwise return an empty
    window for a slug ref — dark instead of diagnostic."""
    cfg, _, ms = _setup()
    await ms.open()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(mcp, "get_channel_history", {"channel": "alice-1234"})
    _assert_dm_hint(excinfo.value, "alice-1234")
    await ms.close()


@pytest.mark.asyncio
async def test_get_channel_history_non_ch_ref_known_to_cache_still_works():
    """The slug-hint guard only fires on refs the cache does NOT
    know — an exotic non-``ch_`` id with a cached space mapping keeps
    working."""
    cfg, _, ms = _setup()
    await ms.open()
    await ms.mark_channel_space("weird-legacy-id", "sp_test")
    await ms.store({
        "envelope_id": "env_legacy", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "weird-legacy-id",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "still reachable", "sent_at": _now_ms(),
    })
    mcp = _build_tools(cfg)
    result = await _call(
        mcp, "get_channel_history", {"channel": "weird-legacy-id"},
    )
    assert "still reachable" in result
    await ms.close()


@pytest.mark.asyncio
async def test_ch_prefixed_cache_miss_keeps_membership_error():
    """A genuine ``ch_`` id the cache misses keeps the original
    membership-flavoured error — the slug hint would mislead there."""
    cfg, _, _ = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(
            mcp, "send_message",
            {"channel": "ch_nowhere", "text": "x", "visibility_level": "human"},
        )
    assert "no record of channel" in str(excinfo.value)
    assert "not a channel id" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_message_dm():
    cfg, http, ms = _setup()
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
    result = await _call(
        mcp,
        "send_message",
        {"channel": "@alice-0001", "text": "hey", "visibility_level": "human"},
    )
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
        await _call(
            mcp,
            "send_message",
            {"channel": "#general", "text": "hi", "visibility_level": "human"},
        )
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
async def test_get_channel_history_unknown_channel():
    """Channel never seen → 'no such channel: …'. Distinct from
    the empty-window message so the agent doesn't conflate a
    bad channel id with a quiet one."""
    cfg, _, ms = _setup()
    await ms.open()
    mcp = _build_tools(cfg)
    result = await _call(mcp, "get_channel_history", {"channel": "ch_nonexistent"})
    assert "no such channel" in result
    assert "ch_nonexistent" in result
    await ms.close()


@pytest.mark.asyncio
async def test_get_dm_history_from_local():
    cfg, _, ms = _setup()
    await ms.open()
    base = _now_ms()
    await ms.store({
        "envelope_id": "dm_1", "envelope_kind": "dm",
        "sender_slug": "alice-0001", "recipient_slug": "me-0001",
        "content_type": "text/plain", "content": "hi from alice", "sent_at": base,
    })
    await ms.store({
        "envelope_id": "dm_2", "envelope_kind": "dm",
        "sender_slug": "me-0001", "recipient_slug": "alice-0001",
        "content_type": "text/plain", "content": "hi back", "sent_at": base + 1000,
    })
    await ms.store({
        "envelope_id": "dm_3", "envelope_kind": "dm",
        "sender_slug": "bob-0002", "recipient_slug": "me-0001",
        "content_type": "text/plain", "content": "bob here", "sent_at": base + 2000,
    })
    mcp = _build_tools(cfg)
    result = await _call(mcp, "get_dm_history", {"peer": "alice-0001", "limit": 10})
    assert "hi from alice" in result
    assert "hi back" in result
    assert "bob here" not in result   # a different peer is filtered out
    await ms.close()


@pytest.mark.asyncio
async def test_get_dm_history_empty():
    cfg, _, ms = _setup()
    await ms.open()
    mcp = _build_tools(cfg)
    result = await _call(mcp, "get_dm_history", {"peer": "nobody-9999"})
    assert "no direct messages" in result
    await ms.close()


@pytest.mark.asyncio
async def test_get_channel_history_empty_window():
    """Channel exists but the ``since`` filter pushes past every
    root → 'no root posts in the requested window'."""
    cfg, _, ms = _setup()
    await ms.open()
    base = _now_ms()
    await ms.store({
        "envelope_id": "env_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_seen",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "Hello", "sent_at": base,
    })
    mcp = _build_tools(cfg)
    result = await _call(
        mcp, "get_channel_history",
        {"channel": "ch_seen", "since": "env_root"},
    )
    assert "no root posts" in result
    await ms.close()


@pytest.mark.asyncio
async def test_get_thread_history_unknown_root():
    cfg, _, ms = _setup()
    await ms.open()
    mcp = _build_tools(cfg)
    result = await _call(mcp, "get_thread_history", {"root_id": "msg_nonexistent"})
    assert "no such thread" in result
    assert "msg_nonexistent" in result
    await ms.close()


@pytest.mark.asyncio
async def test_get_thread_history_empty_window():
    cfg, _, ms = _setup()
    await ms.open()
    base = _now_ms()
    await ms.store({
        "envelope_id": "env_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_t",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "Root", "sent_at": base,
    })
    mcp = _build_tools(cfg)
    # ``since=env_root`` filters out the root itself; thread has no
    # replies → empty window.
    result = await _call(
        mcp, "get_thread_history",
        {"root_id": "env_root", "since": "env_root"},
    )
    assert "no messages in this thread" in result
    await ms.close()


@pytest.mark.asyncio
async def test_list_spaces_returns_server_filtered_memberships():
    """``GET /spaces`` is server-filtered to memberships the agent
    actually has; the tool just formats the result. Server-side
    enforcement means "if it's in the list, the agent can write
    there" — pair with ``list_channels_in_space`` for the channel
    detail."""
    cfg, http, ms = _setup()
    http.responses["/spaces"] = {
        "spaces": [
            {"space_id": "sp_team", "name": "Team"},
            {"space_id": "sp_other", "name": "Other"},
        ],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_spaces")
    assert "sp_team" in result and "Team" in result
    assert "sp_other" in result and "Other" in result
    # No per-space round-trips — list_spaces stays cheap.
    per_space_calls = [c for c in http.calls if "/channels" in c[1]]
    assert per_space_calls == []


@pytest.mark.asyncio
async def test_list_spaces_returns_empty_marker_when_not_a_member():
    cfg, http, ms = _setup()
    http.responses["/spaces"] = {"spaces": []}
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_spaces")
    assert "not a member" in result


@pytest.mark.asyncio
async def test_list_channels_in_space_scopes_to_one_space():
    """``list_channels_in_space(space_id)`` round-trips exactly one
    ``GET /spaces/<sp>/channels`` and formats the result. No
    ``GET /spaces`` enumeration; no ``cfg.space_id`` consulting."""
    cfg, http, ms = _setup()
    cfg.space_id = "sp_legacy"  # must be irrelevant
    http.responses["/spaces/sp_target/channels"] = {
        "channels": [
            {"channel_id": "ch_g", "name": "general"},
            {"channel_id": "ch_r", "name": "random"},
        ],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels_in_space", {"space_id": "sp_target"})

    assert "ch_g" in result and "general" in result
    assert "ch_r" in result and "random" in result
    # Exactly one round-trip; never to cfg.space_id or /spaces.
    assert ("GET", "/spaces/sp_target/channels", None) in http.calls
    assert not any(c[1] == "/spaces" for c in http.calls)
    assert not any("sp_legacy" in c[1] for c in http.calls)


@pytest.mark.asyncio
async def test_list_channels_in_space_requires_space_id():
    """Missing ``space_id`` is a contract error — surface it as an
    MCP tool error rather than silently using ``cfg.space_id``."""
    cfg, http, ms = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception):
        await _call(mcp, "list_channels_in_space", {"space_id": ""})


@pytest.mark.asyncio
async def test_list_channels_in_space_tolerates_string_response():
    """Tight race after AcceptSpaceInvite: server briefly returns
    the SPA HTML stub (``str``). Treat as "no channels yet"."""
    cfg, http, ms = _setup()
    http.responses["/spaces/sp_racy/channels"] = ""
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels_in_space", {"space_id": "sp_racy"})
    assert "no channels" in result


@pytest.mark.asyncio
async def test_list_channels_in_all_spaces_enumerates_all_spaces():
    """One ``GET /spaces`` + one ``GET /spaces/<sp>/channels`` per
    space, grouped output. Convenience shortcut over ``list_spaces``
    + per-space calls."""
    cfg, http, ms = _setup()
    http.responses["/spaces"] = {
        "spaces": [
            {"space_id": "sp_team", "name": "Team"},
            {"space_id": "sp_other", "name": "Other"},
        ],
    }
    http.responses["/spaces/sp_team/channels"] = {
        "channels": [
            {"channel_id": "ch_g_team", "name": "General", "is_public": True},
            {"channel_id": "ch_rand", "name": "Random", "is_public": False},
        ],
    }
    http.responses["/spaces/sp_other/channels"] = {
        "channels": [
            {"channel_id": "ch_g_other", "name": "General", "is_public": True},
        ],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels_in_all_spaces")

    # Both spaces named, grouped, with all their channels.
    assert "sp_team" in result and "Team" in result
    assert "sp_other" in result and "Other" in result
    assert "ch_g_team" in result and "ch_rand" in result
    assert "ch_g_other" in result
    # One /spaces + one /spaces/<sp>/channels per space.
    assert ("GET", "/spaces", None) in http.calls
    assert ("GET", "/spaces/sp_team/channels", None) in http.calls
    assert ("GET", "/spaces/sp_other/channels", None) in http.calls


@pytest.mark.asyncio
async def test_list_channels_in_all_spaces_returns_empty_message_with_no_spaces():
    """Agent not in any space (new install, fully cascaded out) —
    no /spaces/<sp>/channels round-trips at all."""
    cfg, http, ms = _setup()
    http.responses["/spaces"] = {"spaces": []}
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels_in_all_spaces")

    assert "not a member" in result
    per_space_calls = [c for c in http.calls if "/channels" in c[1]]
    assert per_space_calls == []


@pytest.mark.asyncio
async def test_list_channels_in_all_spaces_ignores_cfg_space_id():
    """Req 3 anchor: ``cfg.space_id`` is legacy metadata and must
    not gate the LLM's view. An agent with ``cfg.space_id``
    pointing at a space it IS NOT in must still see channels in the
    spaces it IS in."""
    cfg, http, ms = _setup()
    cfg.space_id = "sp_legacy_not_a_member"  # explicit miss
    http.responses["/spaces"] = {
        "spaces": [{"space_id": "sp_real", "name": "Real"}],
    }
    http.responses["/spaces/sp_real/channels"] = {
        "channels": [
            {"channel_id": "ch_only", "name": "general"},
        ],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels_in_all_spaces")

    assert "ch_only" in result
    assert "sp_real" in result
    legacy_calls = [
        c for c in http.calls
        if "sp_legacy_not_a_member" in c[1]
    ]
    assert legacy_calls == [], (
        f"expected no calls into cfg.space_id, got {legacy_calls}"
    )


@pytest.mark.asyncio
async def test_list_channels_in_all_spaces_tolerates_per_space_string_response():
    """One space's ``/channels`` returns the SPA HTML stub (tight
    race); other spaces still enumerate cleanly."""
    cfg, http, ms = _setup()
    http.responses["/spaces"] = {
        "spaces": [
            {"space_id": "sp_a", "name": "A"},
            {"space_id": "sp_b", "name": "B"},
        ],
    }
    http.responses["/spaces/sp_a/channels"] = ""  # racy / unhealthy
    http.responses["/spaces/sp_b/channels"] = {
        "channels": [{"channel_id": "ch_x", "name": "general"}],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels_in_all_spaces")

    assert "sp_a" in result
    assert "(no channels)" in result
    assert "ch_x" in result


@pytest.mark.asyncio
async def test_list_channel_members():
    """Channel members come from
    ``/spaces/<sp>/channels/<ch>/members`` keyed by space_id."""
    cfg, http, ms = _setup()
    # Pre-cache the channel→space mapping the way an inbound message
    # would: send_message now resolves space via the local cache, then
    # via /spaces walking, and refuses to fall back to cfg.space_id.
    await ms.mark_channel_space("ch_abc", "sp_test")
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
    cfg, http, ms = _setup()
    http.responses["/identities/profiles?slugs=alice-0001"] = {
        "profiles": [{
            "slug": "alice-0001",
            # Server returns ``display_name`` (was previously
            # ``username`` in this fixture, mirroring a bug in
            # the production tool — both were fixed together).
            "display_name": "Alice",
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
async def test_send_message_with_attachments_requires_workspace():
    """Without ``cfg.workspace``, send_message_with_attachments refuses
    rather than silently dropping into a "no agent dir" hole. Real
    upload path is exercised end-to-end against a live daemon."""
    cfg, _, _ = _setup()
    # Fixture leaves cfg.workspace as None.
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc_info:
        await _call(
            mcp,
            "send_message_with_attachments",
            {"paths": ["test.txt"], "channel": "ch_1", "visibility_level": "human"},
        )
    assert "workspace" in str(exc_info.value).lower()


# PUF-200: _resolve_root_id


class _FakeDataClient:
    """Stand-in for ``DataClient`` — seed thread_root_id values and
    inject lookup failures without touching SQLite."""

    def __init__(self):
        self.messages: dict[str, object] = {}
        self.exc: Exception | None = None
        self.calls: list[str] = []

    def add(
        self,
        envelope_id: str,
        thread_root_id: str | None,
        *,
        channel_id: str | None = None,
        space_id: str | None = None,
    ) -> None:
        class _Msg:
            pass
        m = _Msg()
        m.envelope_id = envelope_id
        m.thread_root_id = thread_root_id
        m.channel_id = channel_id
        m.space_id = space_id
        self.messages[envelope_id] = m

    async def get_message_by_envelope(self, envelope_id: str):
        self.calls.append(envelope_id)
        if self.exc is not None:
            raise self.exc
        return self.messages.get(envelope_id)


@pytest.mark.asyncio
async def test_resolve_root_id_empty_skips_lookup():
    dc = _FakeDataClient()
    resolved, note = await _resolve_root_id("", dc)
    assert resolved is None
    assert note == ""
    assert dc.calls == []
    # Whitespace-only is also treated as empty.
    resolved, note = await _resolve_root_id("   ", dc)
    assert resolved is None and note == ""
    assert dc.calls == []


@pytest.mark.asyncio
async def test_resolve_root_id_true_root_unchanged():
    dc = _FakeDataClient()
    dc.add("msg_root", thread_root_id=None)
    resolved, note = await _resolve_root_id("msg_root", dc)
    assert resolved == "msg_root"
    assert note == ""
    assert dc.calls == ["msg_root"]


@pytest.mark.asyncio
async def test_resolve_root_id_reply_auto_corrected():
    dc = _FakeDataClient()
    dc.add("msg_root", thread_root_id=None)
    dc.add("msg_reply", thread_root_id="msg_root")
    resolved, note = await _resolve_root_id("msg_reply", dc)
    assert resolved == "msg_root"
    assert "auto-corrected" in note
    assert "msg_reply" in note and "msg_root" in note
    assert "thread_root_id" in note and "post_id" in note


@pytest.mark.asyncio
async def test_resolve_root_id_depth_two_chain_walks_to_root():
    dc = _FakeDataClient()
    dc.add("msg_root", thread_root_id=None)
    dc.add("msg_mid", thread_root_id="msg_root")
    dc.add("msg_leaf", thread_root_id="msg_mid")
    resolved, note = await _resolve_root_id("msg_leaf", dc)
    assert resolved == "msg_root"
    assert "auto-corrected" in note
    assert dc.calls == ["msg_leaf", "msg_mid", "msg_root"]


@pytest.mark.asyncio
async def test_resolve_root_id_lookup_miss_falls_through_with_warning():
    dc = _FakeDataClient()
    resolved, note = await _resolve_root_id("msg_unknown", dc)
    assert resolved == "msg_unknown"
    assert "could not verify" in note
    assert "not in local store" in note
    assert "thread_root_id" in note


@pytest.mark.asyncio
async def test_resolve_root_id_transport_error_falls_through_with_warning():
    dc = _FakeDataClient()
    dc.exc = RuntimeError("simulated transport blip")
    resolved, note = await _resolve_root_id("msg_anything", dc)
    assert resolved == "msg_anything"
    assert "could not verify" in note
    assert "lookup failed" in note


@pytest.mark.asyncio
async def test_resolve_root_id_data_not_found_treated_as_lookup_miss():
    """``DataClient.get_message_by_envelope`` raises ``DataNotFound``
    (rather than returning None) when the data service is reachable
    but the agent never recorded the envelope. The resolver should
    treat that the same as a None return — fall through with the
    "not in local store" warning, not the broader "lookup failed"
    one."""
    from puffo_agent.agent.message_store import DataNotFound
    dc = _FakeDataClient()
    dc.exc = DataNotFound("msg_only_on_server")
    resolved, note = await _resolve_root_id("msg_only_on_server", dc)
    assert resolved == "msg_only_on_server"
    assert "could not verify" in note
    assert "not in local store" in note
    assert "lookup failed" not in note


@pytest.mark.asyncio
async def test_resolve_root_id_cycle_preserves_root_id_with_warning():
    """Cycle is corrupt data — don't auto-correct to a node we
    can't trust. Preserve the original ``root_id`` and surface a
    loud warning so the operator can investigate."""
    dc = _FakeDataClient()
    dc.add("msg_a", thread_root_id="msg_b")
    dc.add("msg_b", thread_root_id="msg_a")
    resolved, note = await _resolve_root_id("msg_a", dc)
    assert resolved == "msg_a"
    assert "could not resolve" in note
    assert "cycle detected" in note


@pytest.mark.asyncio
async def test_resolve_root_id_depth_cap_preserves_root_id_with_warning():
    """Same corruption-defense path for a chain deeper than the
    cap — preserve ``root_id``, warn loudly, don't auto-correct."""
    dc = _FakeDataClient()
    # Chain deeper than _RESOLVE_ROOT_MAX_DEPTH (4): leaf → l4 → l3 → l2 → l1 → root
    dc.add("msg_leaf", thread_root_id="msg_l4")
    dc.add("msg_l4", thread_root_id="msg_l3")
    dc.add("msg_l3", thread_root_id="msg_l2")
    dc.add("msg_l2", thread_root_id="msg_l1")
    dc.add("msg_l1", thread_root_id="msg_root")
    dc.add("msg_root", thread_root_id=None)
    resolved, note = await _resolve_root_id("msg_leaf", dc)
    assert resolved == "msg_leaf"
    assert "could not resolve" in note
    assert "deeper than" in note


def _spy_encrypt_input(monkeypatch):
    """Capture the EncryptInput so tests can assert on the payload's
    thread_root_id. Patches both encrypt entrypoints — send paths
    use the with_content_key variant."""
    import puffo_agent.mcp.puffo_core_tools as pct
    captured: dict = {}
    real = pct.encrypt_message
    real_with_key = pct.encrypt_message_with_content_key

    def spy(inp, signing_key, **kw):
        captured["inp"] = inp
        return real(inp, signing_key, **kw)

    def spy_with_key(inp, signing_key, **kw):
        captured["inp"] = inp
        return real_with_key(inp, signing_key, **kw)

    monkeypatch.setattr(pct, "encrypt_message", spy)
    monkeypatch.setattr(pct, "encrypt_message_with_content_key", spy_with_key)
    return captured


def _seed_recipient(http, recipient_slug: str):
    recipient_kem = KemKeyPair.generate()
    http.responses[f"/certs/sync?slugs={recipient_slug}"] = {
        "entries": [{
            "seq": 1, "kind": "device_cert", "slug": recipient_slug,
            "cert": {
                "device_id": f"dev_{recipient_slug}",
                "kem_public_key": base64url_encode(
                    recipient_kem.public_key_bytes()
                ),
            },
        }],
        "has_more": False,
    }


async def _seed_channel(ms, http, channel_id: str, space_id: str,
                        recipient_slug: str):
    await ms.mark_channel_space(channel_id, space_id)
    http.responses[f"/spaces/{space_id}/channels/{channel_id}/members"] = {
        "members": [{"slug": recipient_slug, "role": "owner"}],
    }
    _seed_recipient(http, recipient_slug)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong_post_id, real_root_id, scenario",
    [
        # The two live failures we hit on 2026-05-18 with operator
        # mingvase-8795 — the clone-report send (post_id of the
        # operator's "please clone" message used as root_id) and
        # the build-test report send (post_id of the operator's
        # "ensure you can build/test" message used as root_id).
        ("msg_38364760-cd04-408a-9daf-aad66a2487fc",
         "msg_610fec10-122f-4fff-8dcb-498770809c84",
         "clone-report-live-failure"),
        ("msg_9e8f1a83-05ff-4775-8e07-b90999c61d53",
         "msg_610fec10-122f-4fff-8dcb-498770809c84",
         "build-test-report-live-failure"),
    ],
)
async def test_send_message_auto_corrects_real_live_failures(
    monkeypatch, wrong_post_id, real_root_id, scenario,
):
    """Each parameter is one of the two real failures we observed
    on 2026-05-18 — operator's post is the thread root, the message
    Calculation incorrectly passed as root_id is a reply in that
    same thread. After the fix the EncryptInput must carry the real
    root and the response must include the correction note."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    await ms.store({
        "envelope_id": real_root_id, "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "real root", "sent_at": _now_ms(),
        "thread_root_id": None,
    })
    await ms.store({
        "envelope_id": wrong_post_id, "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": f"reply in thread ({scenario})", "sent_at": _now_ms(),
        "thread_root_id": real_root_id,
    })
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc",
        "text": f"replaying {scenario}",
        "visibility_level": "default",
        "root_id": wrong_post_id,
    })

    assert "posted" in result
    assert "auto-corrected" in result
    assert wrong_post_id in result and real_root_id in result
    assert captured["inp"].thread_root_id == real_root_id


@pytest.mark.asyncio
async def test_send_message_keeps_real_root_id_unchanged(monkeypatch):
    """Happy path: agent passes a real root id; no correction note,
    EncryptInput's thread_root_id is the supplied id."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root post", "sent_at": _now_ms(),
        "thread_root_id": None,
    })
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc",
        "text": "correctly threaded reply",
        "visibility_level": "default",
        "root_id": "msg_root",
    })

    assert "posted" in result
    assert "auto-corrected" not in result
    assert "could not verify" not in result
    assert captured["inp"].thread_root_id == "msg_root"


@pytest.mark.asyncio
async def test_send_message_unknown_root_id_wiped_to_null_with_warning(monkeypatch):
    """PUF-227-A: strict cache-validation invariant. An unknown
    root_id (not in this agent's local store) gets WIPED to null
    before the envelope ships — the operator locked Q1(a) "client
    should only see thread_root_id that's in its local cache." The
    tool response carries a warning so the agent self-corrects on
    its next compose. Replaces PUF-200's "fall through with the
    original id" behavior, which was the permissive shape PUF-227-A
    explicitly overrides."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc",
        "text": "racing the inbound write",
        "visibility_level": "default",
        "root_id": "msg_never_seen",
    })

    assert "posted" in result
    assert "not in local cache" in result
    assert "wiped to null" in result or "sent as top-level" in result
    # PUF-227-A strict: invalid id wiped to None, NOT carried into
    # the payload.
    assert captured["inp"].thread_root_id is None


@pytest.mark.asyncio
async def test_send_message_root_level_send_skips_resolve(monkeypatch):
    """No root_id → no lookup attempted, EncryptInput's thread_root_id
    is None, no resolve-style note in the response."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc", "text": "top-level", "visibility_level": "human",
    })

    assert "posted" in result
    assert "auto-corrected" not in result
    assert "could not verify" not in result
    assert captured["inp"].thread_root_id is None


@pytest.mark.asyncio
async def test_send_message_with_attachments_auto_corrects_reply_as_root_id(
    monkeypatch, tmp_path,
):
    """Same auto-correction behaviour on the attachments path."""
    cfg, http, ms = _setup()
    cfg.workspace = tmp_path
    (tmp_path / "hello.txt").write_bytes(b"hello attachments")
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    http.responses["/blobs/upload"] = {"blob_id": "blob_xyz"}
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(),
        "thread_root_id": None,
    })
    await ms.store({
        "envelope_id": "msg_reply", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "reply", "sent_at": _now_ms(),
        "thread_root_id": "msg_root",
    })
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message_with_attachments", {
        "paths": ["hello.txt"],
        "channel": "ch_abc",
        "visibility_level": "default",
        "root_id": "msg_reply",
        "caption": "files",
    })

    assert "uploaded" in result
    assert "auto-corrected" in result
    # Display string reflects the *resolved* thread, not the wrong id.
    assert "in thread msg_root" in result
    assert captured["inp"].thread_root_id == "msg_root"


@pytest.mark.asyncio
async def test_core_tools_registered():
    cfg, _, _ = _setup()
    mcp = _build_tools(cfg)
    tool_names = {t.name for t in await mcp.list_tools()}
    expected = {
        "whoami", "send_message", "get_channel_history",
        "list_spaces", "list_channels_in_all_spaces",
        "list_channels_in_space", "list_channel_members",
        "get_user_info", "get_post", "send_message_with_attachments",
    }
    assert expected.issubset(tool_names)


# PUF-227-A: _validate_root_same_channel — strict cache + channel-
# match validation, applied AFTER _resolve_root_id on the sender path.


@pytest.mark.asyncio
async def test_validate_root_passes_through_when_no_root_id():
    """``resolved_root=None`` is the top-level-post case; helper is a
    no-op and returns no warning."""
    dc = _FakeDataClient()
    out, note = await _validate_root_same_channel(None, "ch_x", "sp_1", dc)
    assert out is None
    assert note == ""


@pytest.mark.asyncio
async def test_validate_root_passes_through_when_parent_in_same_channel():
    """Parent envelope exists locally + matches outbound channel +
    space → pass through unchanged, no warning."""
    dc = _FakeDataClient()
    dc.add("msg_root", thread_root_id=None, channel_id="ch_x", space_id="sp_1")
    out, note = await _validate_root_same_channel("msg_root", "ch_x", "sp_1", dc)
    assert out == "msg_root"
    assert note == ""


@pytest.mark.asyncio
async def test_validate_root_wipes_when_parent_in_different_channel():
    """Scout's PUF-227 symptom shape on the sender side. Parent
    exists in cache but its channel doesn't match outbound — wipe
    to None + emit warning."""
    dc = _FakeDataClient()
    dc.add(
        "msg_root",
        thread_root_id=None,
        channel_id="ch_general",
        space_id="sp_1",
    )
    out, note = await _validate_root_same_channel(
        "msg_root", "ch_gtm", "sp_1", dc,
    )
    assert out is None
    assert "different" in note.lower() or "belongs to" in note.lower()
    assert "ch_general" in note
    assert "ch_gtm" in note


@pytest.mark.asyncio
async def test_validate_root_wipes_when_parent_not_in_cache():
    """Strict per operator's Q1(a): parent-not-in-cache → wipe to
    None. No permissive fallback."""
    dc = _FakeDataClient()
    out, note = await _validate_root_same_channel(
        "msg_unknown", "ch_x", "sp_1", dc,
    )
    assert out is None
    assert "not in local cache" in note
    assert "msg_unknown" in note


@pytest.mark.asyncio
async def test_validate_root_wipes_when_parent_in_different_space():
    """Cross-space parent — same defense as cross-channel."""
    dc = _FakeDataClient()
    dc.add(
        "msg_root",
        thread_root_id=None,
        channel_id="ch_x",
        space_id="sp_OTHER",
    )
    out, note = await _validate_root_same_channel(
        "msg_root", "ch_x", "sp_1", dc,
    )
    assert out is None
    assert "different" in note.lower() or "belongs to space" in note.lower()
    assert "sp_OTHER" in note


@pytest.mark.asyncio
async def test_validate_root_wipes_on_lookup_transport_error():
    """Strict mode: if the local-cache lookup itself errors out
    (sqlite hiccup, DataClient transport blip), treat as 'not
    verified' and wipe — don't ship an unverifiable id."""
    dc = _FakeDataClient()
    dc.exc = RuntimeError("simulated lookup failure")
    out, note = await _validate_root_same_channel(
        "msg_any", "ch_x", "sp_1", dc,
    )
    assert out is None
    assert "could not be verified" in note
    assert "lookup failed" in note


@pytest.mark.asyncio
async def test_validate_root_dm_envelope_no_channel_id_passes_through():
    """DM context: no channel_id to compare against; helper still
    enforces cache presence but skips the channel-match check.
    (Cross-DM-thread validation is out of scope for this ticket per
    the build plan.)"""
    dc = _FakeDataClient()
    dc.add(
        "msg_dm_root",
        thread_root_id=None,
        channel_id=None,
        space_id=None,
    )
    out, note = await _validate_root_same_channel(
        "msg_dm_root", None, None, dc,
    )
    assert out == "msg_dm_root"
    assert note == ""


# resolve_visibility — one entry point that combines level parsing,
# root-level coerce, DM/@-mention detection, and the per-level note
# wording.


class _VisHttp:
    """Stub for ``/identities/profiles?slugs=<csv>``."""

    def __init__(
        self,
        types: dict[str, str] | None = None,
        *,
        raise_error: bool = False,
    ):
        self.types = types or {}
        self.raise_error = raise_error
        self.calls: list[str] = []

    async def get(self, path: str):
        self.calls.append(path)
        if self.raise_error:
            raise RuntimeError("simulated transport failure")
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(path).query)
        slugs = (qs.get("slugs", [""])[0]).split(",") if qs.get("slugs") else []
        profiles = [
            {"slug": s, "identity_type": self.types.get(s, "human")}
            for s in slugs if s
        ]
        return {"profiles": profiles}


# ── level="human" ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_human_returns_visible_no_note_no_lookup():
    http = _VisHttp({"alice-1234": "human"})
    visible, note = await resolve_visibility(
        "human", "@alice-1234", "@alice-1234 hi", "msg_root", http,
    )
    assert visible is True
    assert note == ""
    assert http.calls == []


# ── level="default" ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_default_dm_coerces_and_nudges_human():
    http = _VisHttp()
    visible, note = await resolve_visibility(
        "default", "@alice-1234", "hi", "msg_root", http,
    )
    assert visible is True
    assert "sent visible" in note
    assert "DM" in note
    assert "'human'" in note
    assert http.calls == []


@pytest.mark.asyncio
async def test_resolve_default_mention_human_coerces():
    http = _VisHttp({"alice-1234": "human"})
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "@alice-1234 here's the answer", "msg_root", http,
    )
    assert visible is True
    assert "@-mentions a human" in note
    assert "'human'" in note
    assert http.calls and "alice-1234" in http.calls[0]


@pytest.mark.asyncio
async def test_resolve_default_mention_agent_only_stays_hidden_but_nudges():
    http = _VisHttp({"scout-5678": "agent"})
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "@scout-5678 pipeline done", "msg_root", http,
    )
    assert visible is False
    assert "sent hidden" in note
    assert "'human'" in note and "'agent_only'" in note


@pytest.mark.asyncio
async def test_resolve_default_no_signal_nudges_explicit():
    http = _VisHttp()
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "internal retry", "msg_root", http,
    )
    assert visible is False
    assert "sent hidden" in note
    assert "'human'" in note and "'agent_only'" in note


@pytest.mark.asyncio
async def test_resolve_default_root_level_always_coerces():
    """No root_id → can't fold → always sent visible regardless of
    DM / @-mention signals."""
    http = _VisHttp()
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "top-level chatter", "", http,
    )
    assert visible is True
    assert "root-level messages can't fold" in note
    assert http.calls == []


@pytest.mark.asyncio
async def test_resolve_default_mixed_mentions_any_human_wins():
    http = _VisHttp({"alice-1234": "human", "scout-5678": "agent"})
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "@scout-5678 @alice-1234 status", "msg_root", http,
    )
    assert visible is True
    assert "@-mentions a human" in note


@pytest.mark.asyncio
async def test_resolve_default_profile_error_soft_fails_to_hidden():
    """Transport error on profile fetch can't flip an intentional
    hidden send — nudge fires, no coerce."""
    http = _VisHttp({"alice-1234": "human"}, raise_error=True)
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "@alice-1234 hi", "msg_root", http,
    )
    assert visible is False
    assert "sent hidden" in note


@pytest.mark.asyncio
async def test_resolve_default_email_not_mistaken_for_mention():
    http = _VisHttp({"alice-1234": "human"})
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "see contact@alice-1234 for details",
        "msg_root", http,
    )
    assert visible is False
    assert "sent hidden" in note
    assert http.calls == []


# ── level="agent_only" ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_agent_only_dm_stays_hidden_but_warns():
    http = _VisHttp()
    visible, note = await resolve_visibility(
        "agent_only", "@alice-1234", "hi", "msg_root", http,
    )
    assert visible is False
    assert "sent hidden per" in note
    assert "DM" in note
    assert "Double-check" in note


@pytest.mark.asyncio
async def test_resolve_agent_only_mention_human_stays_hidden_but_warns():
    http = _VisHttp({"alice-1234": "human"})
    visible, note = await resolve_visibility(
        "agent_only", "ch_abcd", "@alice-1234 fyi", "msg_root", http,
    )
    assert visible is False
    assert "@-mentions a human" in note
    assert "Double-check" in note


@pytest.mark.asyncio
async def test_resolve_agent_only_mention_agent_no_note():
    http = _VisHttp({"scout-5678": "agent"})
    visible, note = await resolve_visibility(
        "agent_only", "ch_abcd", "@scout-5678 done", "msg_root", http,
    )
    assert visible is False
    assert note == ""


@pytest.mark.asyncio
async def test_resolve_agent_only_root_level_still_coerces():
    """agent_only doesn't override the root-level constraint — the UI
    can't fold root-level so it goes out visible."""
    http = _VisHttp()
    visible, note = await resolve_visibility(
        "agent_only", "ch_abcd", "top-level", "", http,
    )
    assert visible is True
    assert "root-level messages can't fold" in note


# ── validation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_rejects_unknown_level():
    http = _VisHttp()
    with pytest.raises(RuntimeError, match="visibility_level"):
        await resolve_visibility("visible", "ch_x", "hi", "msg_root", http)
    with pytest.raises(RuntimeError):
        await resolve_visibility("", "ch_x", "hi", "msg_root", http)


# ── F4 / F5: keyless-transport send preconditions ───────────────────
#
# Under the T23 keyless transport the two send tools POST plaintext to
# ``/v2/cloud-agents/messages`` (blobs to ``/v2/cloud-agents/blobs/
# upload``) instead of driving the bridge WS. F4/F5 semantics are
# preserved on that HTTP seam:
#
# F4: reply_to_id must be dropped when _validate_root_same_channel wipes
#     the resolved root (no dangling parent ref).
# F5: send_message_with_attachments must run EVERY precondition
#     (destination resolve, root validate, all per-file size checks)
#     before the first blob upload, so a rejected route or an oversized
#     later file raises with no orphaned blobs.


class _RecordingBridge:
    """A bridge stub used only to PROVE the keyless send path bypasses
    the WS: if it's ever touched (``sent``/``uploaded`` non-empty) the
    keyless branch wrongly fell back to the bridge."""

    def __init__(self):
        self.sent: list[dict] = []
        self.uploaded: list[bytes] = []
        self._seq = 0

    async def upload_blob(self, data: bytes) -> dict:
        self._seq += 1
        self.uploaded.append(data)
        return {"blob_id": f"blob_{self._seq:04d}", "size_bytes": len(data)}

    async def send_send(self, *, plaintext, recipient_slug=None,
                        space_id=None, channel_id=None, reply_to_id=None,
                        thread_root_id=None, attachments=None,
                        timeout: float = 30.0) -> dict:
        self.sent.append({
            "plaintext": plaintext, "recipient_slug": recipient_slug,
            "space_id": space_id, "channel_id": channel_id,
            "reply_to_id": reply_to_id, "thread_root_id": thread_root_id,
            "attachments": attachments,
        })
        return {"type": "ack", "envelope_id": "msg_rec"}


def _keyless_ws_setup(bridge=None):
    """A keyless tools config with a workspace dir so the send tools'
    attachments path runs. An optional ``bridge`` is attached only to
    prove the keyless branch never touches it. Returns
    ``(cfg, http, ms, workspace_dir)``."""
    cfg, http, ms = _setup_keyless()
    ws = tempfile.mkdtemp()
    cfg.workspace = ws
    if bridge is not None:
        cfg.bridge_client = bridge
    return cfg, http, ms, ws


def _write_ws_file(ws: str, name: str, data: bytes = b"x") -> str:
    from pathlib import Path
    (Path(ws) / name).write_bytes(data)
    return name


@pytest.mark.asyncio
async def test_f4_keyless_reply_to_dropped_when_root_wiped():
    """An unknown root_id gets wiped by _validate_root_same_channel; the
    keyless send body must carry NEITHER thread_root_id NOR reply_to_id
    (F4: no dangling parent ref)."""
    cfg, http, ms, _ = _keyless_ws_setup()
    await ms.mark_channel_space("ch_abc", "sp_test")
    mcp = _build_tools(cfg)

    result = await _call(mcp, "send_message", {
        "channel": "ch_abc", "text": "reply", "root_id": "msg_never_seen",
    })

    sends = _keyless_sends(http)
    assert len(sends) == 1
    body = sends[0]
    assert "thread_root_id" not in body
    assert "reply_to_id" not in body  # F4: dropped alongside the wiped root
    assert "posted" in result


@pytest.mark.asyncio
async def test_f4_keyless_reply_to_kept_when_root_valid():
    """A valid same-channel root is preserved: both thread_root_id and
    reply_to_id ride the keyless send body."""
    cfg, http, ms, _ = _keyless_ws_setup()
    await ms.mark_channel_space("ch_abc", "sp_test")
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root post", "sent_at": _now_ms(),
        "thread_root_id": None,
    })
    mcp = _build_tools(cfg)

    result = await _call(mcp, "send_message", {
        "channel": "ch_abc", "text": "reply", "root_id": "msg_root",
    })

    body = _keyless_sends(http)[0]
    assert body["thread_root_id"] == "msg_root"
    assert body["reply_to_id"] == "msg_root"
    assert "posted" in result


@pytest.mark.asyncio
async def test_f4_keyless_attachments_reply_to_dropped_when_root_wiped():
    """The same F4 gate applies to the keyless attachments send path."""
    cfg, http, ms, ws = _keyless_ws_setup()
    await ms.mark_channel_space("ch_abc", "sp_test")
    _write_ws_file(ws, "a.txt", b"aaa")
    mcp = _build_tools(cfg)

    await _call(mcp, "send_message_with_attachments", {
        "paths": ["a.txt"], "channel": "ch_abc", "caption": "cap",
        "root_id": "msg_never_seen",
    })

    body = _keyless_sends(http)[0]
    assert "thread_root_id" not in body
    assert "reply_to_id" not in body


@pytest.mark.asyncio
async def test_f5_keyless_attachments_bare_at_dm_raises_before_upload():
    """A bare ``@`` destination is rejected before any blob is uploaded."""
    cfg, http, ms, ws = _keyless_ws_setup()
    _write_ws_file(ws, "a.txt", b"aaa")
    mcp = _build_tools(cfg)

    with pytest.raises(Exception) as exc:
        await _call(mcp, "send_message_with_attachments", {
            "paths": ["a.txt"], "channel": "@", "caption": "x",
        })
    assert "DM recipient" in str(exc.value)
    assert http.uploaded == []  # F5: no orphaned blobs


@pytest.mark.asyncio
async def test_f5_keyless_attachments_stale_channel_raises_before_upload():
    """A stale/unknown ``ch_`` id (not in the cache) raises via
    _resolve_channel_space before any upload."""
    cfg, http, ms, ws = _keyless_ws_setup()
    _write_ws_file(ws, "a.txt", b"aaa")
    mcp = _build_tools(cfg)

    with pytest.raises(Exception) as exc:
        await _call(mcp, "send_message_with_attachments", {
            "paths": ["a.txt"], "channel": "ch_stale", "caption": "x",
        })
    assert "no record of channel" in str(exc.value)
    assert http.uploaded == []
    await ms.close()


@pytest.mark.asyncio
async def test_f5_keyless_attachments_oversized_later_file_orphans_nothing():
    """An oversized SECOND file makes the whole send raise before ANY
    upload — the earlier valid file must not be orphaned on the server."""
    cfg, http, ms, ws = _keyless_ws_setup()
    await ms.mark_channel_space("ch_abc", "sp_test")
    _write_ws_file(ws, "small.txt", b"small")
    _write_ws_file(ws, "big.bin", b"x" * (8 * 1024 * 1024 + 1))
    mcp = _build_tools(cfg)

    with pytest.raises(Exception) as exc:
        await _call(mcp, "send_message_with_attachments", {
            "paths": ["small.txt", "big.bin"], "channel": "ch_abc",
            "caption": "x",
        })
    assert "8 MiB" in str(exc.value)
    assert http.uploaded == []  # F5: the earlier small file wasn't uploaded
    await ms.close()


@pytest.mark.asyncio
async def test_f5_keyless_attachments_happy_path_uploads_all_and_sends_once():
    """The valid multi-file keyless path uploads every file via
    ``post_bytes_unsigned`` then issues exactly one
    ``post_unsigned`` carrying all blob refs."""
    cfg, http, ms, ws = _keyless_ws_setup()
    await ms.mark_channel_space("ch_abc", "sp_test")
    _write_ws_file(ws, "a.txt", b"aaa")
    _write_ws_file(ws, "b.txt", b"bbbb")
    mcp = _build_tools(cfg)

    result = await _call(mcp, "send_message_with_attachments", {
        "paths": ["a.txt", "b.txt"], "channel": "ch_abc", "caption": "hi",
    })

    # Two unsigned blob uploads, in order, then one unsigned message send.
    upload_paths = [p for m, p, _ in http.calls if m == "POST_BYTES_UNSIGNED"]
    assert upload_paths == [
        "/v2/cloud-agents/blobs/upload", "/v2/cloud-agents/blobs/upload",
    ]
    assert http.uploaded == [b"aaa", b"bbbb"]
    sends = _keyless_sends(http)
    assert len(sends) == 1
    body = sends[0]
    assert body["space_id"] == "sp_test"
    assert body["channel_id"] == "ch_abc"
    assert body["plaintext"] == "hi"
    assert [r["filename"] for r in body["attachments"]] == ["a.txt", "b.txt"]
    assert [r["blob_id"] for r in body["attachments"]] == ["blob_0001", "blob_0002"]
    assert "uploaded 2 file" in result


# ── keyless reads → /v2/cloud-agents/* (unsigned) ───────────────────


@pytest.mark.asyncio
async def test_keyless_list_spaces_hits_cloud_agents_route():
    cfg, http, ms = _setup_keyless()
    http.responses["/v2/cloud-agents/spaces"] = {
        "spaces": [{"space_id": "sp_team", "name": "Team"}],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_spaces")
    assert "sp_team" in result and "Team" in result
    assert ("GET_UNSIGNED", "/v2/cloud-agents/spaces", None) in http.calls


@pytest.mark.asyncio
async def test_keyless_list_channels_in_space_hits_cloud_agents_route():
    cfg, http, ms = _setup_keyless()
    http.responses["/v2/cloud-agents/spaces/sp_target/channels"] = {
        "channels": [{"channel_id": "ch_g", "name": "general"}],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels_in_space", {"space_id": "sp_target"})
    assert "ch_g" in result and "general" in result
    assert (
        "GET_UNSIGNED", "/v2/cloud-agents/spaces/sp_target/channels", None,
    ) in http.calls


@pytest.mark.asyncio
async def test_keyless_list_channels_in_all_spaces_hits_cloud_agents_routes():
    cfg, http, ms = _setup_keyless()
    http.responses["/v2/cloud-agents/spaces"] = {
        "spaces": [{"space_id": "sp_a", "name": "A"}],
    }
    http.responses["/v2/cloud-agents/spaces/sp_a/channels"] = {
        "channels": [{"channel_id": "ch_x", "name": "general"}],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels_in_all_spaces")
    assert "sp_a" in result and "ch_x" in result
    assert ("GET_UNSIGNED", "/v2/cloud-agents/spaces", None) in http.calls
    assert (
        "GET_UNSIGNED", "/v2/cloud-agents/spaces/sp_a/channels", None,
    ) in http.calls


@pytest.mark.asyncio
async def test_keyless_list_channel_members_degrades_to_space_roster():
    """No keyless channel-members route exists; the keyless tool reads
    the space roster ``/v2/cloud-agents/spaces/<sp>/members`` after
    resolving channel→space from the cache."""
    cfg, http, ms = _setup_keyless()
    await ms.mark_channel_space("ch_abc", "sp_test")
    http.responses["/v2/cloud-agents/spaces/sp_test/members"] = {
        "members": [
            {"slug": "alice-0001", "role": "owner"},
            {"slug": "agent-0001", "role": "member"},
        ],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channel_members", {"channel": "ch_abc"})
    assert "alice-0001" in result and "(owner)" in result
    assert "agent-0001" in result and "(member)" in result
    assert (
        "GET_UNSIGNED", "/v2/cloud-agents/spaces/sp_test/members", None,
    ) in http.calls
    # NEVER the native channel-scoped route.
    assert not any("channels/ch_abc/members" in p for _, p, _ in http.calls)


@pytest.mark.asyncio
async def test_keyless_get_user_info_hits_cloud_agents_route():
    cfg, http, ms = _setup_keyless()
    http.responses["/v2/cloud-agents/identities/profiles?slugs=alice-0001"] = {
        "profiles": [{
            "slug": "alice-0001", "display_name": "Alice", "bio": "A user",
        }],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "get_user_info", {"username": "@alice-0001"})
    assert "alice-0001" in result and "Alice" in result and "A user" in result
    assert (
        "GET_UNSIGNED",
        "/v2/cloud-agents/identities/profiles?slugs=alice-0001",
        None,
    ) in http.calls


# ── keyless whoami: no keystore ─────────────────────────────────────


@pytest.mark.asyncio
async def test_keyless_whoami_needs_no_keystore():
    """Keyless whoami builds identity from cfg + resolves display_name
    over the unsigned profiles route, never loading the keystore."""
    cfg, http, ms = _setup_keyless()
    spy = _SpyKeyStore()
    cfg.keystore = spy
    http.responses["/v2/cloud-agents/identities/profiles?slugs=agent-0001"] = {
        "profiles": [{"slug": "agent-0001", "display_name": "Cloud Bot"}],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "whoami")
    assert "Cloud Bot" in result
    assert "agent-0001" in result
    assert "dev_test" in result
    assert "sandbox.local" in result          # from http_client.server_url
    assert "managed server-side" in result    # keyless subkey line
    assert spy.loads == []                     # keystore never touched


# ── keyless send_message: unsigned POST, no bridge ──────────────────


@pytest.mark.asyncio
async def test_keyless_send_message_dm_posts_unsigned():
    cfg, http, ms = _setup_keyless()
    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "@alice-0001", "text": "hi there",
    })
    assert "posted" in result
    sends = [(p, b) for m, p, b in http.calls if m == "POST_UNSIGNED"]
    assert len(sends) == 1
    path, body = sends[0]
    assert path == "/v2/cloud-agents/messages"
    assert body == {"plaintext": "hi there", "recipient_slug": "alice-0001"}


@pytest.mark.asyncio
async def test_keyless_send_message_channel_posts_unsigned():
    cfg, http, ms = _setup_keyless()
    await ms.mark_channel_space("ch_abc", "sp_test")
    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc", "text": "hello channel",
    })
    assert "posted" in result
    sends = [(p, b) for m, p, b in http.calls if m == "POST_UNSIGNED"]
    assert len(sends) == 1
    path, body = sends[0]
    assert path == "/v2/cloud-agents/messages"
    assert body == {
        "plaintext": "hello channel",
        "space_id": "sp_test",
        "channel_id": "ch_abc",
    }


@pytest.mark.asyncio
async def test_keyless_send_message_channel_threaded_carries_ids():
    cfg, http, ms = _setup_keyless()
    await ms.mark_channel_space("ch_abc", "sp_test")
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(), "thread_root_id": None,
    })
    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc", "text": "reply", "root_id": "msg_root",
    })
    assert "posted" in result
    body = _keyless_sends(http)[0]
    assert body["space_id"] == "sp_test"
    assert body["channel_id"] == "ch_abc"
    assert body["thread_root_id"] == "msg_root"
    assert body["reply_to_id"] == "msg_root"


@pytest.mark.asyncio
async def test_keyless_send_message_returns_ack_envelope_id():
    cfg, http, ms = _setup_keyless()
    http.responses["/v2/cloud-agents/messages"] = {"envelope_id": "msg_ack99"}
    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "@alice-0001", "text": "hi",
    })
    assert "msg_ack99" in result


@pytest.mark.asyncio
async def test_keyless_send_message_bypasses_bridge():
    """A bridge is present but the keyless branch must POST over HTTP and
    make ZERO bridge send_send calls."""
    bridge = _RecordingBridge()
    cfg, http, ms, _ = _keyless_ws_setup(bridge)
    mcp = _build_tools(cfg)
    await _call(mcp, "send_message", {"channel": "@bob-0001", "text": "yo"})
    assert bridge.sent == []
    assert bridge.uploaded == []
    assert _keyless_sends(http) == [
        {"plaintext": "yo", "recipient_slug": "bob-0001"},
    ]


@pytest.mark.asyncio
async def test_keyless_attachments_bypasses_bridge():
    """Keyless attachments upload via post_bytes_unsigned and never touch
    the bridge's upload_blob/send_send."""
    bridge = _RecordingBridge()
    cfg, http, ms, ws = _keyless_ws_setup(bridge)
    await ms.mark_channel_space("ch_abc", "sp_test")
    _write_ws_file(ws, "a.txt", b"aaa")
    mcp = _build_tools(cfg)
    await _call(mcp, "send_message_with_attachments", {
        "paths": ["a.txt"], "channel": "ch_abc", "caption": "cap",
    })
    assert bridge.sent == []
    assert bridge.uploaded == []
    assert http.uploaded == [b"aaa"]
    assert len(_keyless_sends(http)) == 1


# ── build_server(transport="bridge") is keyless-self-sufficient ─────


def test_build_server_bridge_transport_is_keyless(tmp_path, monkeypatch):
    """The subprocess server built with ``transport="bridge"`` gives its
    ``PuffoCoreHttpClient`` ``keyless=True`` and keeps ``bridge_client``
    None (outbound is HTTP, not WS)."""
    import puffo_agent.mcp.puffo_core_server as pcs

    captured = {}
    real = pcs.PuffoCoreHttpClient

    def spy(server_url, ks, slug, keyless=False):
        client = real(server_url, ks, slug, keyless=keyless)
        captured["client"] = client
        return client

    monkeypatch.setattr(pcs, "PuffoCoreHttpClient", spy)
    server = pcs.build_server(
        slug="bot-0001", device_id="dev_test", server_url="http://127.0.0.1:1",
        space_id="", keystore_dir="", workspace=str(tmp_path),
        agent_id="bot-0001", data_service_url="http://127.0.0.1:1",
        transport="bridge",
    )
    from mcp.server.fastmcp import FastMCP
    assert isinstance(server, FastMCP)
    assert captured["client"].keyless is True


def test_build_server_native_transport_is_not_keyless(tmp_path, monkeypatch):
    """A non-bridge build keeps the signed path — ``keyless`` is False."""
    import puffo_agent.mcp.puffo_core_server as pcs

    captured = {}
    real = pcs.PuffoCoreHttpClient

    def spy(server_url, ks, slug, keyless=False):
        client = real(server_url, ks, slug, keyless=keyless)
        captured["client"] = client
        return client

    monkeypatch.setattr(pcs, "PuffoCoreHttpClient", spy)
    pcs.build_server(
        slug="bot-0001", device_id="dev_test", server_url="http://127.0.0.1:1",
        space_id="", keystore_dir=str(tmp_path / "keys"),
        workspace=str(tmp_path), agent_id="bot-0001",
        data_service_url="http://127.0.0.1:1",
    )
    assert captured["client"].keyless is False
