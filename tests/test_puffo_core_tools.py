import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.message_store import MessageStore, ReceiptDisposition
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.keystore import KeyStore, Session, StoredIdentity, encode_secret
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair
from puffo_agent.agent.send_coordinator import SendCoordinator
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
        self._seq = 100

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
        if path == "/v2/agent-runtime/messages:send":
            self._seq += 1
            freshness = body["freshness"]
            return {
                "state": "sent",
                "envelope_id": body["envelope"]["envelope_id"],
                "seq": self._seq,
                "replay": False,
                "missing_devices": [],
                "freshness": {
                    "mode": freshness["mode"],
                    "context_baseline_seq": (
                        freshness["context_baseline_seq"]
                    ),
                    "seen_seq": freshness["seen_seq"],
                    "latest_seq_before_send": freshness["seen_seq"],
                },
            }
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
    class _Freshness:
        async def get_context_baseline_seq(self, _space_id, _channel_id):
            return 0

        async def get_active_turn_through_seq(self, _space_id, _channel_id):
            return None

        async def advance_active_turn_through_seq(self, *_args):
            return None

    freshness = _Freshness()
    cfg.send_coordinator = SendCoordinator(
        slug=cfg.slug,
        keystore=cfg.keystore,
        http_client=cfg.http_client,
        data_client=cfg.data_client,
        workspace=cfg.workspace,
        baseline_source=freshness,
        active_turn_source=freshness,
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
        if path == "/v2/cloud-agents/agent-runtime/messages:send":
            freshness = body["freshness"]
            request_baseline = freshness["context_baseline_seq"]
            return {
                "state": "sent",
                "envelope_id": "msg_keyless",
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
    text = await _call_text(mcp, name, args)
    if "[send_result " in text and 'state="failed"' in text:
        raise RuntimeError(text)
    return text


async def _call_text(mcp, name, args=None):
    result = await mcp.call_tool(name, args or {})
    if (
        isinstance(result, tuple)
        and len(result) > 1
        and isinstance(result[1], dict)
        and result[1].get("state") == "failed"
    ):
        raise RuntimeError(json.dumps(result[1]))
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list):
        return "".join(
            getattr(item, "text", str(item)) for item in result
        )
    return str(result)


async def _call_structured(mcp, name, args=None):
    result = await mcp.call_tool(name, args or {})
    assert isinstance(result, tuple)
    assert isinstance(result[1], dict)
    return result[1]


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
    result = await _call_structured(mcp, "whoami")
    assert result["context_version"] == 1
    assert result["identity"]["display_name"] == "Helper Bot"
    assert result["identity"]["identity"] == "@agent-0001"


@pytest.mark.asyncio
async def test_hidden_schema_semantic_send_fields_only():
    cfg, _, _ = _setup()
    tools = {tool.name: tool for tool in await _build_tools(cfg).list_tools()}
    expected = {
        "send_message": {
            "channel", "text", "root_id", "visibility_level", "send_anyway",
        },
        "send_message_with_attachments": {
            "paths", "channel", "caption", "root_id",
            "visibility_level", "send_anyway",
        },
    }
    forbidden = {
        "freshness", "freshness_mode", "mode", "context_baseline_seq",
        "seen_seq", "synchronized", "transport", "provider_session_id",
        "session_ref", "turn_id", "turn_ref", "sequence", "seq",
        "through_seq", "latest_seq", "latest_envelope_id", "held_pair",
        "client_ref", "admission_receipt", "correlation_receipt",
        "tool_name", "tool_arguments",
    }
    for name, property_set in expected.items():
        properties = set(tools[name].inputSchema["properties"])
        assert properties == property_set
        assert properties.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_send_tool_descriptions_stay_at_mcp_boundary():
    cfg, _, _ = _setup()
    tools = {tool.name: tool for tool in await _build_tools(cfg).list_tools()}
    for name in ("send_message", "send_message_with_attachments"):
        description = " ".join(tools[name].description.lower().split())
        for phrase in ("channel", "root_id", "visibility_level", "send_anyway", "held", "error"):
            assert phrase in description, (name, phrase, description)
        for forbidden in (
            "same originating assignment", "not an automatic retry",
            "visible_draft_basis", "new_channel_context", "context_ready",
            "benchmark", "assignment-completion", "response quota", "counting",
        ):
            assert forbidden not in description


@pytest.mark.asyncio
async def test_send_tool_descriptions_point_to_managed_skill():
    cfg, _, _ = _setup()
    tools = {tool.name: tool for tool in await _build_tools(cfg).list_tools()}
    for name in ("send_message", "send_message_with_attachments"):
        description = " ".join(tools[name].description.lower().split())
        assert "managed" in description and "send-message" in description


@pytest.mark.asyncio
async def test_message_read_schemas_separate_inbox_work_from_history_context():
    cfg, _, _ = _setup()
    calls = []

    class Runtime:
        async def read_inbox(self, **kwargs):
            calls.append(kwargs)
            return {
                "messages": ["message"],
                "next_cursor": "cursor-2",
                "has_more": True,
                "remaining_count": 72,
                "snapshot_generation": 9,
            }

    cfg.inbox_runtime = Runtime()
    mcp = _build_tools(cfg)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    inbox_schema = tools["read_inbox"].inputSchema
    history_schema = tools["read_history"].inputSchema
    assert set(inbox_schema["properties"]) == {"target", "cursor", "limit"}
    assert set(history_schema["properties"]) == {
        "target", "cursor", "before_message_id", "after_message_id", "limit",
    }
    assert {
        "read_messages", "get_channel_history", "get_dm_history", "get_thread_history",
    }.isdisjoint(tools)
    assert set(inbox_schema["properties"] | history_schema["properties"]).isdisjoint({
        "freshness", "freshness_mode", "mode", "context_baseline_seq",
        "seen_seq", "synchronized", "transport", "provider_session_id",
        "session_ref", "turn_id", "turn_ref", "sequence", "seq",
        "through_seq", "latest_seq", "latest_envelope_id", "held_pair",
        "client_ref", "admission_receipt", "correlation_receipt",
        "message_ids", "tool_name", "tool_arguments",
    })
    result = await mcp.call_tool(
        "read_inbox",
        {"target": "channel:sp_1:ch_1", "cursor": "opaque", "limit": 17},
    )
    rendered = "".join(getattr(item, "text", str(item)) for item in result[0])
    assert '[window context_version=1 kind="inbox"' in rendered
    assert "message" in rendered
    assert "earlier_context" not in rendered
    assert calls == [{
        "target": "channel:sp_1:ch_1",
        "cursor": "opaque",
        "limit": 17,
        "tool_arguments": {
            "target": "channel:sp_1:ch_1",
            "cursor": "opaque",
            "limit": 17,
        },
    }]

    inbox_description = " ".join(tools["read_inbox"].description.lower().split())
    history_description = " ".join(tools["read_history"].description.lower().split())
    assert "pending" in inbox_description and "cursor" in inbox_description
    assert "earlier conversation" in history_description
    assert "managed" in inbox_description and "read-messages" in inbox_description
    assert "managed" in history_description and "read-messages" in history_description


@pytest.mark.asyncio
async def test_reminder_tools_have_exact_semantic_schemas_and_live_dispatch():
    cfg, _, _ = _setup()
    calls: list[tuple[str, dict]] = []
    reminder = {
        "reminder_id": "reminder-1",
        "occurrence_id": "occurrence-1",
        "state": "scheduled",
        "target": "channel:sp:ch",
        "content": "exact content",
        "intended_at": "2026-08-02T12:00:00.000Z",
        "actual_fire_at": None,
        "created_at": "2026-08-02T11:00:00.000Z",
        "cancelled_at": None,
        "delivered_at": None,
    }

    class Runtime:
        async def create_reminder(self, **kwargs):
            calls.append(("create", kwargs))
            return reminder

        async def list_reminders(self, **kwargs):
            calls.append(("list", kwargs))
            return {"reminders": [reminder]}

        async def cancel_reminder(self, **kwargs):
            calls.append(("cancel", kwargs))
            return {**reminder, "state": "cancelled", "cancelled_at": "2026-08-02T11:01:00.000Z"}

        async def replace_reminder(self, **kwargs):
            calls.append(("replace", kwargs))
            return {"cancelled": reminder, "replacement": {**reminder, "content": "new"}}

    cfg.inbox_runtime = Runtime()
    mcp = _build_tools(cfg)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert set(tools).issuperset({
        "create_reminder", "list_reminders", "cancel_reminder", "replace_reminder",
    })
    assert set(tools["create_reminder"].inputSchema["properties"]) == {
        "content", "target", "intended_at",
    }
    assert set(tools["list_reminders"].inputSchema["properties"]) == {
        "state", "limit",
    }
    assert set(tools["cancel_reminder"].inputSchema["properties"]) == {
        "reminder_id",
    }
    assert set(tools["replace_reminder"].inputSchema["properties"]) == {
        "reminder_id", "content", "target", "intended_at",
    }
    forbidden = {
        "recurrence", "provider", "server", "schedule_wake", "pause",
        "resume", "execute", "skip", "apologize", "reply", "silence",
        "state_machine", "actual_fire_at", "occurrence_id",
    }
    for name in ("create_reminder", "list_reminders", "cancel_reminder", "replace_reminder"):
        assert set(tools[name].inputSchema["properties"]).isdisjoint(forbidden)

    assert (await mcp.call_tool("create_reminder", {
        "content": "exact content", "target": "channel:sp:ch",
        "intended_at": "2026-08-02T12:00:00Z",
    }))[1] == reminder
    assert (await mcp.call_tool("list_reminders", {
        "state": "scheduled", "limit": 3,
    }))[1] == {"reminders": [reminder]}
    cancelled = (await mcp.call_tool("cancel_reminder", {
        "reminder_id": "reminder-1",
    }))[1]
    assert cancelled["state"] == "cancelled"
    replaced = (await mcp.call_tool("replace_reminder", {
        "reminder_id": "reminder-1", "content": "new",
    }))[1]
    assert replaced["replacement"]["content"] == "new"
    assert calls == [
        ("create", {
            "content": "exact content", "target": "channel:sp:ch",
            "intended_at": "2026-08-02T12:00:00Z",
        }),
        ("list", {"state": "scheduled", "limit": 3}),
        ("cancel", {"reminder_id": "reminder-1"}),
        ("replace", {
            "reminder_id": "reminder-1", "content": "new",
            "target": "", "intended_at": "",
        }),
    ]


@pytest.mark.asyncio
async def test_reminder_tools_fall_back_to_configured_loopback_rpc_client():
    """The subprocess MCP surface must use the same semantic objects."""
    cfg, _, _ = _setup()
    calls: list[tuple[str, dict]] = []
    scheduled = {
        "reminder_id": "reminder-1",
        "occurrence_id": "occurrence-1",
        "state": "scheduled",
        "target": "channel:sp:ch",
        "content": "exact content",
        "intended_at": "2026-08-02T12:00:00.000Z",
        "actual_fire_at": None,
        "created_at": "2026-08-02T11:00:00.000Z",
        "cancelled_at": None,
        "delivered_at": None,
    }
    cancelled = {
        **scheduled,
        "state": "cancelled",
        "cancelled_at": "2026-08-02T11:01:00.000Z",
    }

    class Rpc:
        async def create_reminder(self, **kwargs):
            calls.append(("create", kwargs))
            return scheduled

        async def list_reminders(self, **kwargs):
            calls.append(("list", kwargs))
            return {"reminders": [scheduled]}

        async def cancel_reminder(self, **kwargs):
            calls.append(("cancel", kwargs))
            return cancelled

        async def replace_reminder(self, **kwargs):
            calls.append(("replace", kwargs))
            return {"cancelled": cancelled, "replacement": scheduled}

    # No warm in-process runtime is available on the subprocess MCP path.
    cfg.inbox_runtime = None
    cfg.message_client = None
    cfg.rpc_client = Rpc()
    mcp = _build_tools(cfg)

    assert (await mcp.call_tool("create_reminder", {
        "content": "exact content", "target": "channel:sp:ch",
        "intended_at": "2026-08-02T12:00:00Z",
    }))[1] == scheduled
    assert (await mcp.call_tool("list_reminders", {
        "state": "scheduled", "limit": 3,
    }))[1] == {"reminders": [scheduled]}
    assert (await mcp.call_tool("cancel_reminder", {
        "reminder_id": "reminder-1",
    }))[1] == cancelled
    assert (await mcp.call_tool("replace_reminder", {
        "reminder_id": "reminder-1", "intended_at": "2026-08-03T12:00:00Z",
    }))[1] == {"cancelled": cancelled, "replacement": scheduled}
    assert calls == [
        ("create", {
            "content": "exact content", "target": "channel:sp:ch",
            "intended_at": "2026-08-02T12:00:00Z",
        }),
        ("list", {"state": "scheduled", "limit": 3}),
        ("cancel", {"reminder_id": "reminder-1"}),
        ("replace", {
            "reminder_id": "reminder-1", "content": "", "target": "",
            "intended_at": "2026-08-03T12:00:00Z",
        }),
    ]


@pytest.mark.asyncio
async def test_semantic_rpc_unavailable_returns_semantic_failure():
    cfg, http, _ = _setup()
    cfg.send_coordinator = None
    cfg.rpc_client = None
    result = await _call_text(
        _build_tools(cfg),
        "send_message", {"channel": "ch_a", "text": "do not post"},
    )
    assert 'state="failed"' in result
    assert "attempted=true" in result
    assert not [call for call in http.calls if call[0] == "POST"]


@pytest.mark.asyncio
async def test_semantic_in_process_uses_injected_persistent_coordinator():
    cfg, _, _ = _setup()
    calls = []

    class Stub:
        async def send(self, request):
            calls.append(request)
            return {"state": "held", "attempted": True}

    cfg.send_coordinator = Stub()
    result = await _call_text(
        _build_tools(cfg),
        "send_message",
        {"channel": "ch_a", "text": "x", "send_anyway": True},
    )
    assert 'state="held"' in result
    assert "attempted=true" in result
    assert calls[0].send_anyway is True


@pytest.mark.asyncio
async def test_semantic_out_of_process_uses_structured_rpc_client():
    cfg, _, _ = _setup()
    bodies = []

    class Rpc:
        async def send_message(self, **body):
            bodies.append(body)
            return {
                "state": "sent",
                "attempted": True,
                "envelope_id": "msg_sent",
                "seq": 3,
            }

    cfg.send_coordinator = None
    cfg.rpc_client = Rpc()
    result = await _call_text(
        _build_tools(cfg),
        "send_message", {"channel": "ch_a", "text": "x", "send_anyway": True},
    )
    assert 'state="sent"' in result
    assert 'message_id="msg_sent"' in result
    assert "envelope_id=" not in result
    assert bodies == [{
        "channel": "ch_a",
        "root_id": "",
        "visibility_level": "default",
        "send_anyway": True,
        "text": "x",
    }]


@pytest.mark.asyncio
async def test_keyless_configured_rpc_precedes_direct_unsigned_coordinator():
    cfg, http, _store = _setup_keyless()
    bodies = []

    class Rpc:
        async def send_message(self, **body):
            bodies.append(body)
            return {"state": "sent", "attempted": True, "seq": 3}

    cfg.send_coordinator = None
    cfg.rpc_client = Rpc()
    result = await _call_text(
        _build_tools(cfg),
        "send_message",
        {
            "channel": "ch_a",
            "text": "x",
            "visibility_level": "human",
            "send_anyway": True,
        },
    )
    assert 'state="sent"' in result
    assert len(bodies) == 1
    assert bodies[0]["visibility_level"] == "human"
    assert bodies[0]["send_anyway"] is True
    assert not [call for call in http.calls if call[0] == "POST_UNSIGNED"]


@pytest.mark.asyncio
async def test_keyless_configured_rpc_failure_does_not_fall_back_to_http():
    cfg, http, _store = _setup_keyless()

    class Rpc:
        async def send_message(self, **_body):
            raise ConnectionError("daemon unavailable")

    cfg.send_coordinator = None
    cfg.rpc_client = Rpc()
    result = await _call_text(
        _build_tools(cfg),
        "send_message", {"channel": "ch_a", "text": "x"},
    )
    assert 'state="failed"' in result
    assert 'error_kind="rpc_unavailable"' in result
    assert not [call for call in http.calls if call[0] == "POST_UNSIGNED"]


@pytest.mark.asyncio
async def test_send_message_encrypts_when_daemon_says_unencrypted():
    """Channel sends never downgrade to plaintext: even when the daemon-level
    send-mode decision reports unencrypted (turn bundle cleared, e.g. a
    turn-unbound background wakeup), the envelope still goes out E2EE."""
    cfg, http, ms = _setup()
    recipient_kem = KemKeyPair.generate()
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

    async def _no_encrypt(slug, root):
        return False

    ms.get_send_encryption = _no_encrypt
    mcp = _build_tools(cfg)
    result = await _call(
        mcp,
        "send_message",
        {"channel": "ch_abc", "text": "hello world", "visibility_level": "human"},
    )
    assert "posted" in result
    post_calls = [(p, b) for m, p, b in http.calls if m == "POST"]
    assert len(post_calls) == 1
    envelope = post_calls[0][1]["envelope"]
    assert envelope["type"] == "message_envelope"
    assert "content_ciphertext" in envelope


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
    assert path == "/v2/agent-runtime/messages:send"
    assert set(body) == {"envelope", "freshness"}
    envelope = body["envelope"]
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
    await ms.store({
        "envelope_id": "msg_root_abc", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(),
        "thread_root_id": None,
    })
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
    await ms.store({
        "envelope_id": "msg_root_dm", "envelope_kind": "dm",
        "sender_slug": "alice-0001", "channel_id": None,
        "space_id": None, "recipient_slug": "agent-0001",
        "content_type": "text/plain", "content": "root",
        "sent_at": _now_ms(), "thread_root_id": None,
    })
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
    assert "read_history" in msg


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
async def test_read_history_channel_from_local():
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
    result = await _call(
        mcp,
        "read_history",
        {"target": "channel:sp_test:ch_abc", "limit": 10},
    )
    assert "alice-0001" in result
    assert "bob-0001" in result
    assert "Hello from Alice" in result
    assert "Hello from Bob" in result
    await ms.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("content, expected", [
    ({"text": "structured text", "caption": "unused caption"}, "structured text"),
    ({"caption": "caption fallback"}, "caption fallback"),
])
async def test_structured_content_read_boundaries(content, expected):
    """Every local read surface renders structured content as text, never
    the Python representation of the dict."""
    cfg, _, ms = _setup()
    await ms.open()
    try:
        base = _now_ms()
        rows = [
            {"envelope_id": "root", "envelope_kind": "channel", "sender_slug": "alice",
             "channel_id": "ch_struct", "space_id": "sp_test", "content": content,
             "content_type": "puffo/message+attachments/v1", "sent_at": base},
            {"envelope_id": "reply", "envelope_kind": "channel", "sender_slug": "alice",
             "channel_id": "ch_struct", "space_id": "sp_test", "content": content,
             "content_type": "puffo/message+attachments/v1", "thread_root_id": "root", "sent_at": base + 1},
            {"envelope_id": "dm", "envelope_kind": "dm", "sender_slug": "alice",
             "recipient_slug": "agent-0001", "content": content,
             "content_type": "puffo/message+attachments/v1", "sent_at": base + 2},
        ]
        for row in rows:
            await ms.store(row)
        mcp = _build_tools(cfg)
        outputs = [
            await _call(mcp, "read_history", {
                "target": "channel:sp_test:ch_struct",
            }),
            await _call(mcp, "read_history", {
                "target": "dm:alice",
            }),
            await _call(mcp, "read_history", {
                "target": "channel:sp_test:ch_struct:thread:root",
            }),
            await _call(mcp, "get_post", {"post_ref": "root"}),
            await _call(mcp, "get_post_segment", {"envelope_id": "root", "segment": 0}),
        ]
        assert all(expected in output for output in outputs)
        assert all("{'text':" not in output and "{'caption':" not in output for output in outputs)
    finally:
        await ms.close()


@pytest.mark.asyncio
async def test_message_read_tools_stage_highest_model_visible_server_sequence():
    cfg, _, ms = _setup()
    base = _now_ms()
    root = {
        "envelope_id": "env_root",
        "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "channel_id": "ch_visible",
        "space_id": "sp_visible",
        "content_type": "text/plain",
        "content": "root body",
        "sent_at": base,
    }
    reply = {
        "envelope_id": "env_reply",
        "envelope_kind": "channel",
        "sender_slug": "bob-0001",
        "channel_id": "ch_visible",
        "space_id": "sp_visible",
        "content_type": "text/plain",
        "content": "reply body",
        "sent_at": base + 1,
        "thread_root_id": "env_root",
    }
    for seq, payload in ((41, root), (42, reply)):
        await ms.store_receipt(
            payload,
            server_seq=seq,
            disposition=ReceiptDisposition.ELIGIBLE,
            reason="test",
        )

    class RecordingRpc:
        def __init__(self):
            self.calls = []

        async def stage_model_visible_read(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "state": "staged",
                "correlation_receipt": f"receipt-{len(self.calls)}",
            }

    rpc = RecordingRpc()
    cfg.rpc_client = rpc
    mcp = _build_tools(cfg)
    channel_args = {
        "target": "channel:sp_visible:ch_visible",
    }
    thread_args = {
        "target": "channel:sp_visible:ch_visible:thread:env_root",
    }
    channel_result = await _call(mcp, "read_history", channel_args)
    clamped_result = await _call(
        mcp, "read_history", {**channel_args, "limit": 999},
    )
    await _call(mcp, "read_history", thread_args)
    await _call(mcp, "get_post", {"post_ref": "env_reply"})
    await _call(
        mcp,
        "get_post_segment",
        {"envelope_id": "env_reply", "segment": 0},
    )

    assert [
        (call["tool_name"], call["through_seq"], call["through_envelope_id"])
        for call in rpc.calls
    ] == [
        ("read_history", 41, "env_root"),
        ("read_history", 41, "env_root"),
        ("read_history", 42, "env_reply"),
        ("get_post", 42, "env_reply"),
        ("get_post_segment", 42, "env_reply"),
    ]
    assert [call["tool_arguments"] for call in rpc.calls] == [
        channel_args,
        {**channel_args, "limit": 999},
        thread_args,
        {"post_ref": "env_reply"},
        {"envelope_id": "env_reply", "segment": 0},
    ]
    assert [call["visible_message_ids"] for call in rpc.calls] == [
        ["env_root"], ["env_root"], ["env_root", "env_reply"],
        ["env_reply"], ["env_reply"],
    ]
    assert "[puffo:model-visible-read:receipt-1]" in channel_result
    assert "[puffo:model-visible-read:receipt-2]" in clamped_result
    await ms.close()


@pytest.mark.asyncio
async def test_history_reads_stage_through_the_in_process_inbox_runtime():
    """MAJOR 8: in-process (ws-local) tools have a runtime and no rpc_client.

    Returning early on ``rpc_client is None`` meant a ws-local agent's channel
    watermark never advanced, so already-read content was re-presented on the
    next planning cycle.
    """
    cfg, _, ms = _setup()
    await ms.store_receipt(
        {
            "envelope_id": "env_runtime",
            "envelope_kind": "channel",
            "sender_slug": "alice-0001",
            "channel_id": "ch_runtime",
            "space_id": "sp_runtime",
            "content_type": "text/plain",
            "content": "runtime body",
            "sent_at": _now_ms(),
        },
        server_seq=77,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )

    class RecordingRuntime:
        def __init__(self):
            self.calls = []

        async def stage_model_visible_read(self, **kwargs):
            self.calls.append(kwargs)
            return {"state": "staged", "correlation_receipt": "runtime-receipt"}

    runtime = RecordingRuntime()
    assert cfg.rpc_client is None
    cfg.inbox_runtime = runtime
    mcp = _build_tools(cfg)

    result = await _call(mcp, "read_history", {
        "target": "channel:sp_runtime:ch_runtime",
    })
    assert "[puffo:model-visible-read:runtime-receipt]" in result
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["through_seq"] == 77
    assert runtime.calls[0]["through_envelope_id"] == "env_runtime"
    assert runtime.calls[0]["visible_message_ids"] == ["env_runtime"]
    await ms.close()


@pytest.mark.asyncio
async def test_dm_and_unsequenced_reads_stage_rows_without_channel_freshness():
    cfg, _, ms = _setup()
    await ms.store({
        "envelope_id": "legacy",
        "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "channel_id": "ch_legacy",
        "space_id": "sp_legacy",
        "content_type": "text/plain",
        "content": "legacy body",
        "sent_at": _now_ms(),
    })
    await ms.store_receipt(
        {
            "envelope_id": "sequenced",
            "envelope_kind": "channel",
            "sender_slug": "bob-0001",
            "channel_id": "ch_legacy",
            "space_id": "sp_legacy",
            "content_type": "text/plain",
            "content": "sequenced body",
            "sent_at": _now_ms() + 1,
        },
        server_seq=42,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    await ms.store_receipt(
        {
            "envelope_id": "dm_9",
            "envelope_kind": "dm",
            "sender_slug": "alice-0001",
            "recipient_slug": "agent-0001",
            "content_type": "text/plain",
            "content": "private",
            "sent_at": _now_ms() + 2,
        },
        server_seq=43,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )

    class RecordingRpc:
        def __init__(self):
            self.calls = []

        async def stage_model_visible_read(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "state": "staged",
                "correlation_receipt": f"receipt-{len(self.calls)}",
            }

    rpc = RecordingRpc()
    cfg.rpc_client = rpc
    mcp = _build_tools(cfg)
    await _call(mcp, "read_history", {
        "target": "channel:sp_legacy:ch_legacy",
    })
    await _call(mcp, "get_post", {"post_ref": "dm_9"})
    assert [call["visible_message_ids"] for call in rpc.calls] == [
        ["legacy", "sequenced"], ["dm_9"],
    ]
    assert all(call["through_seq"] is None for call in rpc.calls)
    assert all(call["through_envelope_id"] is None for call in rpc.calls)
    await ms.close()


@pytest.mark.asyncio
async def test_read_history_unknown_channel():
    """Channel never seen → 'no such channel: …'. Distinct from
    the empty-window message so the agent doesn't conflate a
    bad channel id with a quiet one."""
    cfg, _, ms = _setup()
    await ms.open()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(mcp, "read_history", {
            "target": "channel:sp_test:ch_nonexistent",
        })
    assert "no such channel" in str(excinfo.value)
    assert "ch_nonexistent" in str(excinfo.value)
    await ms.close()


@pytest.mark.asyncio
async def test_read_history_dm_from_local():
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
        "sender_slug": "agent-0001", "recipient_slug": "alice-0001",
        "content_type": "text/plain", "content": "hi back", "sent_at": base + 1000,
    })
    await ms.store({
        "envelope_id": "dm_3", "envelope_kind": "dm",
        "sender_slug": "bob-0002", "recipient_slug": "me-0001",
        "content_type": "text/plain", "content": "bob here", "sent_at": base + 2000,
    })
    mcp = _build_tools(cfg)
    result = await _call(mcp, "read_history", {
        "target": "dm:alice-0001", "limit": 10,
    })
    assert "hi from alice" in result
    assert "hi back" in result
    assert "bob here" not in result   # a different peer is filtered out
    assert 'message_id="dm_2"' in result
    assert "self=true" in result
    await ms.close()


@pytest.mark.asyncio
async def test_read_history_dm_empty():
    cfg, _, ms = _setup()
    await ms.open()
    mcp = _build_tools(cfg)
    result = await _call(mcp, "read_history", {
        "target": "dm:nobody-9999",
    })
    assert 'returned_count=0' in result
    assert 'target_ref="dm:nobody-9999"' in result
    await ms.close()


@pytest.mark.asyncio
async def test_read_history_channel_empty_window():
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
        mcp,
        "read_history",
        {
            "target": "channel:sp_test:ch_seen",
            "after_message_id": "env_root",
        },
    )
    assert 'returned_count=0' in result
    await ms.close()


@pytest.mark.asyncio
async def test_read_history_cursor_keeps_equal_timestamp_rows_distinct():
    cfg, _, ms = _setup()
    await ms.open()
    base = _now_ms()
    for seq, env_id in ((11, "env_a"), (12, "env_b"), (13, "env_c")):
        await ms.store_receipt(
            {
                "envelope_id": env_id,
                "envelope_kind": "channel",
                "sender_slug": "alice-0001",
                "channel_id": "ch_bounds",
                "space_id": "sp_test",
                "content_type": "text/plain",
                "content": env_id,
                "sent_at": base,
            },
            server_seq=seq,
            disposition=ReceiptDisposition.ELIGIBLE,
            reason="test",
        )
    mcp = _build_tools(cfg)
    first = await _call(mcp, "read_history", {
        "target": "channel:sp_test:ch_bounds", "limit": 1,
    })
    older = first.split('older_cursor="', 1)[1].split('"', 1)[0]
    second = await _call(mcp, "read_history", {"cursor": older, "limit": 1})
    older = second.split('older_cursor="', 1)[1].split('"', 1)[0]
    third = await _call(mcp, "read_history", {"cursor": older, "limit": 1})

    assert ['message_id="env_c"', 'message_id="env_b"', 'message_id="env_a"'] == [
        next(part for part in page.split() if part.startswith('message_id='))
        for page in (first, second, third)
    ]
    assert "older_cursor=" not in third
    assert 'older_boundary="local_start"' in third
    await ms.close()


@pytest.mark.asyncio
async def test_read_history_unknown_thread():
    cfg, _, ms = _setup()
    await ms.open()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(mcp, "read_history", {
            "target": "channel:sp_test:ch_test:thread:msg_nonexistent",
        })
    assert "no such thread" in str(excinfo.value)
    assert "msg_nonexistent" in str(excinfo.value)
    await ms.close()


@pytest.mark.asyncio
async def test_read_history_thread_empty_window():
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
    result = await _call(
        mcp,
        "read_history",
        {
            "target": "channel:sp_test:ch_t:thread:env_root",
            "after_message_id": "env_root",
        },
    )
    assert 'returned_count=0' in result
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
            {
                "space_id": "sp_team",
                "name": "Team",
                "description": "Core team",
                "role": "member",
                "joined_at": 1700000000000,
            },
            {"space_id": "sp_other", "name": "Other"},
        ],
    }
    mcp = _build_tools(cfg)
    result = await _call_structured(mcp, "list_spaces")
    assert result["context_version"] == 1
    assert result["count"] == 2
    spaces = {space["space_id"]: space for space in result["spaces"]}
    assert spaces["sp_team"]["name"] == "Team"
    assert spaces["sp_team"]["description"] == "Core team"
    assert spaces["sp_team"]["role"] == "member"
    assert spaces["sp_other"]["name"] == "Other"
    # No per-space round-trips — list_spaces stays cheap.
    per_space_calls = [c for c in http.calls if "/channels" in c[1]]
    assert per_space_calls == []


@pytest.mark.asyncio
async def test_list_spaces_returns_empty_marker_when_not_a_member():
    cfg, http, ms = _setup()
    http.responses["/spaces"] = {"spaces": []}
    mcp = _build_tools(cfg)
    result = await _call_structured(mcp, "list_spaces")
    assert result == {"context_version": 1, "count": 0, "spaces": []}


@pytest.mark.asyncio
async def test_list_channels_in_space_scopes_to_one_space():
    """``list_channels_in_space(space_id)`` round-trips exactly one
    ``GET /spaces/<sp>/channels`` and formats the result. No
    ``GET /spaces`` enumeration; no ``cfg.space_id`` consulting."""
    cfg, http, ms = _setup()
    cfg.space_id = "sp_legacy"  # must be irrelevant
    http.responses["/spaces/sp_target/channels"] = {
        "channels": [
            {
                "channel_id": "ch_g",
                "name": "general",
                "description": "Team discussion",
                "is_public": True,
                "owner_slug": "alice-0001",
            },
            {"channel_id": "ch_r", "name": "random"},
        ],
    }
    mcp = _build_tools(cfg)
    result = await _call_structured(
        mcp, "list_channels_in_space", {"space_id": "sp_target"},
    )

    channels = {channel["channel_id"]: channel for channel in result["channels"]}
    assert channels["ch_g"]["name"] == "general"
    assert channels["ch_g"]["description"] == "Team discussion"
    assert channels["ch_g"]["visibility"] == "public"
    assert channels["ch_g"]["owner_identity"] == "@alice-0001"
    assert channels["ch_r"]["name"] == "random"
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
    result = await _call_structured(
        mcp, "list_channels_in_space", {"space_id": "sp_racy"},
    )
    assert result["space_id"] == "sp_racy"
    assert result["count"] == 0
    assert result["channels"] == []


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
    result = await _call_structured(mcp, "list_channels_in_all_spaces")

    assert result["space_count"] == 0
    assert result["channel_count"] == 0
    assert result["spaces"] == []
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
    result = await _call_structured(mcp, "list_channels_in_all_spaces")

    by_space = {space["space_id"]: space for space in result["spaces"]}
    assert by_space["sp_a"]["channels"] == []
    assert by_space["sp_b"]["channels"][0]["channel_id"] == "ch_x"


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
            {
                "slug": "alice-0001",
                "role": "owner",
                "identity_type": "human",
                "owner_slug": None,
            },
            {
                "slug": "agent-0001",
                "role": "member",
                "identity_type": "agent",
                "owner_slug": "alice-0001",
                "online": True,
            },
        ]
    }
    http.responses[
        "/identities/profiles?slugs=alice-0001,agent-0001"
    ] = {
        "profiles": [
            {"slug": "alice-0001", "display_name": "Alice"},
            {"slug": "agent-0001", "display_name": "Helper"},
        ],
    }
    mcp = _build_tools(cfg)
    result = await _call_structured(
        mcp, "list_channel_members", {"channel": "ch_abc"},
    )
    assert result["context_version"] == 1
    assert result["target"]["target_ref"] == "channel:sp_test:ch_abc"
    members = {member["identity"]: member for member in result["members"]}
    assert members["@alice-0001"]["role"] == "owner"
    assert members["@alice-0001"]["display_name"] == "Alice"
    assert members["@alice-0001"]["identity_type"] == "human"
    assert members["@alice-0001"]["owner_identity"] is None
    assert members["@agent-0001"]["role"] == "member"
    assert members["@agent-0001"]["display_name"] == "Helper"
    assert members["@agent-0001"]["identity_type"] == "agent"
    assert members["@agent-0001"]["owner_identity"] == "@alice-0001"
    assert members["@agent-0001"]["self"] is True
    assert members["@agent-0001"]["online"] is True


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
    result = await _call_structured(
        mcp, "get_user_info", {"username": "@alice-0001"},
    )
    assert result["context_version"] == 1
    assert result["found"] is True
    assert result["identity"]["identity"] == "@alice-0001"
    assert result["identity"]["display_name"] == "Alice"
    assert result["identity"]["identity_type"] == "unknown"
    assert result["identity"]["bio"] == "A test user"


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
