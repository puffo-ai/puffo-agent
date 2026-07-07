"""T23 phase 1: transport seam. An injected ``bridge_client`` flips
``PuffoCoreMessageClient.listen()`` to the keyless bridge lifecycle
(no ``PuffoCoreWsClient``, no ``keystore.load_identity``); default
construction keeps the native signed path. ``build_server`` under
``transport="bridge"`` builds with zero key files on disk.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

import puffo_agent.agent.puffo_core_client as pcc_mod
from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.http_client import PuffoCoreHttpClient
from puffo_agent.crypto.keystore import (
    KeyStore,
    Session,
    StoredIdentity,
    encode_secret,
)
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair


class _SpyKeyStore(KeyStore):
    """Records load_identity calls; bridge mode must never make one."""

    def __init__(self) -> None:
        super().__init__(tempfile.mkdtemp(prefix="spy-keys-"))
        self.load_identity_calls: list[str] = []

    def load_identity(self, slug: str) -> StoredIdentity:
        self.load_identity_calls.append(slug)
        return super().load_identity(slug)


class _StubBridge:
    """Bridge stub: records the lifecycle calls listen() makes."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.connected = asyncio.Event()

    async def connect(self) -> None:
        self.calls.append("connect")
        self.connected.set()

    async def frames(self):
        self.calls.append("frames")
        await asyncio.Event().wait()  # block until the test cancels
        yield {}  # pragma: no cover — makes this an async generator

    async def close(self) -> None:
        self.calls.append("close")


class _RecordingWs:
    """PuffoCoreWsClient stand-in — records construction, run() is a
    no-op so listen() returns instead of blocking."""

    instances: list["_RecordingWs"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.on_message = None
        self.on_event = None
        _RecordingWs.instances.append(self)

    async def run(self) -> None:
        return


def _make_client(keystore: KeyStore, tmp_path, **kwargs) -> PuffoCoreMessageClient:
    http = PuffoCoreHttpClient("http://127.0.0.1:1", keystore, "bot-0001")
    store = MessageStore(str(tmp_path / "messages.db"))
    return PuffoCoreMessageClient(
        slug="bot-0001",
        device_id="dev_test",
        space_id="sp_test",
        keystore=keystore,
        http_client=http,
        message_store=store,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_injected_bridge_skips_ws_client_and_identity_load(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(_RecordingWs, "instances", [])
    monkeypatch.setattr(pcc_mod, "PuffoCoreWsClient", _RecordingWs)
    spy_ks = _SpyKeyStore()
    stub = _StubBridge()
    client = _make_client(spy_ks, tmp_path, bridge_client=stub)

    async def on_message(*args) -> None:
        return

    task = asyncio.create_task(client.listen(on_message))
    await asyncio.wait_for(stub.connected.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stub.calls[:2] == ["connect", "frames"]
    assert "close" in stub.calls
    assert _RecordingWs.instances == []
    assert spy_ks.load_identity_calls == []


def test_default_construction_has_no_bridge(tmp_path):
    client = _make_client(KeyStore(str(tmp_path / "keys")), tmp_path)
    assert client._bridge is None


@pytest.mark.asyncio
async def test_native_path_reaches_ws_client_construction(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(_RecordingWs, "instances", [])
    monkeypatch.setattr(pcc_mod, "PuffoCoreWsClient", _RecordingWs)
    ks = KeyStore(str(tmp_path / "keys"))
    ks.save_identity(StoredIdentity(
        slug="bot-0001",
        device_id="dev_test",
        root_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
        device_signing_secret_key=encode_secret(
            Ed25519KeyPair.generate().secret_bytes()
        ),
        kem_secret_key=encode_secret(KemKeyPair.generate().secret_bytes()),
        server_url="http://127.0.0.1:1",
    ))
    ks.save_session(Session(
        slug="bot-0001",
        subkey_id="sk_test",
        subkey_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
        expires_at=32_503_680_000_000,
    ))
    client = _make_client(ks, tmp_path)

    async def on_message(*args) -> None:
        return

    await client.listen(on_message)
    assert len(_RecordingWs.instances) == 1
    assert _RecordingWs.instances[0].kwargs["slug"] == "bot-0001"


def test_build_server_bridge_transport_needs_no_key_files(tmp_path):
    from mcp.server.fastmcp import FastMCP
    from puffo_agent.mcp.puffo_core_server import build_server

    server = build_server(
        slug="bot-0001",
        device_id="dev_test",
        server_url="http://127.0.0.1:1",
        space_id="",
        keystore_dir="",  # zero key files anywhere
        workspace=str(tmp_path),
        agent_id="bot-0001",
        data_service_url="http://127.0.0.1:1",
        transport="bridge",
    )
    assert isinstance(server, FastMCP)


def test_bridge_inert_keystore_raises_clear_error():
    from puffo_agent.mcp.puffo_core_server import _BridgeNoKeysStore

    ks = _BridgeNoKeysStore("")
    with pytest.raises(RuntimeError, match="no local keys"):
        ks.load_identity("bot-0001")
    with pytest.raises(RuntimeError, match="no local keys"):
        ks.load_session("bot-0001")


def test_cfg_from_env_picks_up_transport(monkeypatch):
    from puffo_agent.mcp.puffo_core_server import _cfg_from_env

    monkeypatch.setenv("PUFFO_CORE_SLUG", "bot-0001")
    monkeypatch.setenv("PUFFO_CORE_DEVICE_ID", "dev_test")
    monkeypatch.setenv("PUFFO_CORE_SERVER_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("PUFFO_CORE_TRANSPORT", "bridge")
    cfg = _cfg_from_env()
    assert cfg["transport"] == "bridge"

    monkeypatch.delenv("PUFFO_CORE_TRANSPORT")
    assert _cfg_from_env()["transport"] == ""


def test_puffo_core_mcp_env_transport_is_additive():
    from puffo_agent.mcp.config import puffo_core_mcp_env

    base = dict(
        slug="bot-0001",
        device_id="dev_test",
        server_url="http://127.0.0.1:1",
        keystore_dir="/keys",
        workspace="/ws",
    )
    default_env = puffo_core_mcp_env(**base)
    assert "PUFFO_CORE_TRANSPORT" not in default_env
    bridge_env = puffo_core_mcp_env(**base, transport="bridge")
    assert bridge_env["PUFFO_CORE_TRANSPORT"] == "bridge"
    # Only the transport var differs — default output stays identical.
    bridge_env.pop("PUFFO_CORE_TRANSPORT")
    assert bridge_env == default_env
