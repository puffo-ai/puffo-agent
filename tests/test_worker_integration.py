"""Tests for worker/config integration.

Covers: PuffoCoreConfig, PuffoCoreMessageClient, puffo_core_server,
and config builders.
"""

import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.crypto.keystore import KeyStore, Session, StoredIdentity, encode_secret
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair
from puffo_agent.mcp.config import (
    PUFFO_CORE_TOOL_NAMES,
    PUFFO_CORE_TOOL_FQNS,
    puffo_core_mcp_env,
    puffo_core_stdio_sdk_config,
)
from puffo_agent.portal.state import AgentConfig, PuffoCoreConfig


def _now_ms():
    return int(time.time() * 1000)


def _make_keystore():
    d = tempfile.mkdtemp()
    ks_dir = os.path.join(d, "keys")
    ks = KeyStore(ks_dir)
    device_key = Ed25519KeyPair.generate()
    kem_kp = KemKeyPair.generate()
    identity = StoredIdentity(
        slug="bot-0001",
        device_id="dev_test",
        root_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
        device_signing_secret_key=encode_secret(device_key.secret_bytes()),
        kem_secret_key=encode_secret(kem_kp.secret_bytes()),
        server_url="http://localhost:3000",
    )
    ks.save_identity(identity)
    subkey = Ed25519KeyPair.generate()
    session = Session(
        slug="bot-0001",
        subkey_id="sk_test",
        subkey_secret_key=encode_secret(subkey.secret_bytes()),
        expires_at=_now_ms() + 3_600_000,
    )
    ks.save_session(session)
    return ks, ks_dir, d, kem_kp


# ── PuffoCoreConfig tests ──────────────────────────────────────────


def test_puffo_core_config_is_configured():
    cfg = PuffoCoreConfig()
    assert not cfg.is_configured()

    cfg = PuffoCoreConfig(server_url="http://localhost", slug="bot-0001", device_id="dev_1", space_id="sp_1")
    assert cfg.is_configured()

    cfg = PuffoCoreConfig(server_url="http://localhost", slug="", device_id="dev_1", space_id="sp_1")
    assert not cfg.is_configured()

    cfg = PuffoCoreConfig(server_url="http://localhost", slug="bot-0001", device_id="dev_1", space_id="")
    assert not cfg.is_configured()


def test_puffo_core_config_in_agent_config():
    cfg = AgentConfig(
        id="test-agent",
        puffo_core=PuffoCoreConfig(
            server_url="http://localhost:3000",
            slug="bot-0001",
            device_id="dev_1",
            space_id="sp_test",
        ),
    )
    assert cfg.puffo_core.is_configured()
    assert cfg.puffo_core.space_id == "sp_test"


def test_agent_config_default_puffo_core():
    cfg = AgentConfig(id="test-agent")
    assert not cfg.puffo_core.is_configured()


# ── Config builder tests ───────────────────────────────────────────


def test_puffo_core_tool_names():
    assert "send_message" in PUFFO_CORE_TOOL_NAMES
    assert "whoami" in PUFFO_CORE_TOOL_NAMES
    assert "reload_system_prompt" in PUFFO_CORE_TOOL_NAMES
    assert "approve_permission" not in PUFFO_CORE_TOOL_NAMES
    assert len(PUFFO_CORE_TOOL_FQNS) == len(PUFFO_CORE_TOOL_NAMES)
    assert all(t.startswith("mcp__puffo__") for t in PUFFO_CORE_TOOL_FQNS)


def test_puffo_core_mcp_env():
    env = puffo_core_mcp_env(
        slug="bot-0001",
        device_id="dev_1",
        server_url="http://localhost:3000",
        space_id="sp_test",
        keystore_dir="/tmp/keys",
        workspace="/workspace",
        agent_id="bot-0001",
        runtime_kind="cli-local",
        harness="claude-code",
    )
    assert env["PUFFO_CORE_SLUG"] == "bot-0001"
    assert env["PUFFO_CORE_DEVICE_ID"] == "dev_1"
    assert env["PUFFO_CORE_SERVER_URL"] == "http://localhost:3000"
    assert env["PUFFO_CORE_SPACE_ID"] == "sp_test"
    assert env["PUFFO_CORE_KEYSTORE_DIR"] == "/tmp/keys"
    # MCP reads SQLite via the daemon's data service at
    # PUFFO_DATA_SERVICE_URL, not by opening the DB directly.
    assert "PUFFO_CORE_DB_PATH" not in env
    assert env["PUFFO_DATA_SERVICE_URL"] == "http://127.0.0.1:63386"
    assert env["PUFFO_AGENT_ID"] == "bot-0001"
    assert env["PUFFO_WORKSPACE"] == "/workspace"
    assert env["PUFFO_RUNTIME_KIND"] == "cli-local"
    assert env["PUFFO_HARNESS"] == "claude-code"


def test_puffo_core_mcp_env_optional_fields():
    env = puffo_core_mcp_env(
        slug="bot-0001",
        device_id="dev_1",
        server_url="http://localhost:3000",
        keystore_dir="/tmp/keys",
        workspace="/workspace",
    )
    assert "PUFFO_CORE_SPACE_ID" not in env
    assert "PUFFO_RUNTIME_KIND" not in env
    assert "PUFFO_HARNESS" not in env
    assert "PUFFO_AGENT_ID" not in env


def test_puffo_core_mcp_env_pins_python_user_base():
    """cli-local rewrites HOME on the claude subprocess, which
    would move Python's user-site to an empty per-agent path and
    hide ``mcp`` from the MCP subprocess. ``PYTHONUSERBASE`` pins
    user-site to the daemon's real base regardless of HOME."""
    import site
    env = puffo_core_mcp_env(
        slug="bot-0001",
        device_id="dev_1",
        server_url="http://localhost:3000",
        keystore_dir="/tmp/keys",
        workspace="/workspace",
        runtime_kind="cli-local",
    )
    assert env["PYTHONUSERBASE"] == site.getuserbase()


def test_puffo_core_mcp_env_skips_python_user_base_for_docker():
    """The container has its own Python install with baked-in deps,
    so the host's user-base path is meaningless inside it. We
    deliberately don't forward ``PYTHONUSERBASE`` into the docker
    env block to keep the contract semantically clean."""
    env = puffo_core_mcp_env(
        slug="bot-0001",
        device_id="dev_1",
        server_url="http://localhost:3000",
        keystore_dir="/tmp/keys",
        workspace="/workspace",
        runtime_kind="cli-docker",
    )
    assert "PYTHONUSERBASE" not in env


def test_puffo_core_stdio_sdk_config():
    cfg = puffo_core_stdio_sdk_config(
        python="/usr/bin/python3",
        slug="bot-0001",
        device_id="dev_1",
        server_url="http://localhost:3000",
        space_id="sp_test",
        keystore_dir="/tmp/keys",
        workspace="/workspace",
        agent_id="bot-0001",
    )
    assert "puffo" in cfg
    server = cfg["puffo"]
    assert server["type"] == "stdio"
    assert server["command"] == "/usr/bin/python3"
    assert server["args"] == ["-m", "puffo_agent.mcp.puffo_core_server"]
    assert server["env"]["PUFFO_CORE_SLUG"] == "bot-0001"
    assert server["env"]["PUFFO_AGENT_ID"] == "bot-0001"


# ── MCP server build test ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_puffo_core_server_builds():
    """The puffo-core MCP server should register both API and local tools."""
    ks, ks_dir, d, _ = _make_keystore()
    db_path = os.path.join(d, "messages.db")

    from puffo_agent.mcp.puffo_core_server import build_server

    mcp = build_server(
        slug="bot-0001",
        device_id="dev_test",
        server_url="http://localhost:3000",
        space_id="sp_test",
        keystore_dir=ks_dir,
        workspace=d,
        agent_id="bot-0001",
        # Test never makes a real call; value just needs to parse.
        data_service_url="http://127.0.0.1:0",
    )
    tool_names = {t.name for t in await mcp.list_tools()}
    assert "whoami" in tool_names
    assert "send_message" in tool_names
    assert "get_channel_history" in tool_names
    assert "reload_system_prompt" in tool_names
    assert "refresh" in tool_names
    assert "install_skill" in tool_names
    assert "list_skills" in tool_names
    assert "install_mcp_server" in tool_names
    assert "list_mcp_servers" in tool_names


@pytest.mark.asyncio
async def test_puffo_core_server_whoami():
    ks, ks_dir, d, _ = _make_keystore()
    db_path = os.path.join(d, "messages.db")

    from puffo_agent.mcp.puffo_core_server import build_server

    mcp = build_server(
        slug="bot-0001",
        device_id="dev_test",
        server_url="http://localhost:3000",
        space_id="sp_test",
        keystore_dir=ks_dir,
        workspace=d,
        agent_id="bot-0001",
        # Test never makes a real call; value just needs to parse.
        data_service_url="http://127.0.0.1:0",
    )
    result = await mcp.call_tool("whoami", {})
    text = "".join(getattr(item, "text", str(item)) for item in result)
    assert "bot-0001" in text
    assert "dev_test" in text


# ── MessageStore WAL test ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_store_wal_mode():
    d = tempfile.mkdtemp()
    store = MessageStore(os.path.join(d, "messages.db"))
    await store.open()
    db = await store._ensure_db()
    async with db.execute("PRAGMA journal_mode") as cursor:
        row = await cursor.fetchone()
        assert row[0] == "wal"
    await store.close()


# ── PuffoCoreMessageClient unit tests ──────────────────────────────


@pytest.mark.asyncio
async def test_puffo_core_client_send_fallback_message_encrypts():
    """Smoke test: send_fallback_message resolves channel members and their
    device certs, encrypts the reply, and posts the bare envelope.

    Mocked endpoints reflect the real puffo-core wire shape:
      * ``/spaces/{space}/channels/{ch}/members`` -> ``{"members": [...]}``
      * ``/certs/sync?slugs=...`` -> entries with ``kind=device_cert``
      * ``POST /messages`` accepts the envelope at the top level
        (``Json<MessageEnvelope>``), not wrapped in ``{"envelope": ...}``.
    """
    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient

    ks, ks_dir, d, kem_kp = _make_keystore()
    db_path = os.path.join(d, "messages.db")
    ms = MessageStore(db_path)
    await ms.open()

    from puffo_agent.crypto.encoding import base64url_encode

    recipient_kem_pk_b64 = base64url_encode(
        KemKeyPair.generate().public_key_bytes()
    )

    class FakeHttp:
        def __init__(self):
            self.calls = []
            self.post_bodies = []

        async def get(self, path):
            self.calls.append(("GET", path))
            # Channel members: slugs only; client follows up with
            # /certs/sync to translate to device certs.
            if path.startswith("/spaces/") and "/channels/" in path and path.endswith("/members"):
                return {"members": [{"slug": "alice", "role": "member"}]}
            if path.startswith("/certs/sync"):
                return {
                    "entries": [{
                        "seq": 1,
                        "kind": "device_cert",
                        "slug": "alice",
                        "cert": {
                            "device_id": "dev_recipient",
                            "kem_public_key": recipient_kem_pk_b64,
                        },
                    }],
                    "has_more": False,
                }
            return {}

        async def post(self, path, body=None):
            self.calls.append(("POST", path))
            self.post_bodies.append(body)
            return {"ok": True, "envelope_id": body.get("envelope_id"), "devices_queued": 1}

        async def _ensure_subkey(self):
            pass

    http = FakeHttp()
    client = PuffoCoreMessageClient(
        slug="bot-0001",
        device_id="dev_test",
        space_id="sp_test",
        keystore=ks,
        http_client=http,
        message_store=ms,
    )
    await client.send_fallback_message("ch_abc", "hello world", root_id="")

    # Channel resolution: members endpoint -> /certs/sync.
    assert any(
        m == "GET" and p == "/spaces/sp_test/channels/ch_abc/members"
        for m, p in http.calls
    )
    assert any(m == "GET" and p.startswith("/certs/sync") for m, p in http.calls)
    assert any(("POST", "/messages") == (m, p) for m, p in http.calls)

    assert len(http.post_bodies) == 1
    # Body IS the envelope; no ``{"envelope": ...}`` wrapper.
    envelope = http.post_bodies[0]
    assert envelope["type"] == "message_envelope"
    assert envelope["version"] == 1
    assert envelope["envelope_kind"] == "channel"
    assert envelope["sender_slug"] == "bot-0001"
    assert envelope["channel_id"] == "ch_abc"
    assert envelope["space_id"] == "sp_test"
    assert "content_ciphertext" in envelope
    assert "content_nonce" in envelope
    assert len(envelope["recipients"]) == 1
    r = envelope["recipients"][0]
    assert r["device_id"] == "dev_recipient"
    assert "hpke_enc" in r
    assert "wrapped_content_key" in r
    await ms.close()
