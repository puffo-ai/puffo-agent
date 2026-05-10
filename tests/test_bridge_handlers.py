"""Bridge handler tests: info no-auth, agents list/detail redaction,
file endpoint path safety + caps.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from _bridge_support import (
    isolated_home, make_user, pair_request_body, signed_headers,
    write_test_agent,
)
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.portal.api.server import build_app
from puffo_agent.portal.state import DaemonConfig

pytestmark = pytest.mark.asyncio

_HOST = {"Host": "127.0.0.1:63387"}


@pytest_asyncio.fixture
async def client():
    isolated_home()
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


async def _pair(client, user):
    body = pair_request_body(user)
    h = signed_headers(user, "POST", "/v1/pair", body); h.update(_HOST)
    r = await client.post("/v1/pair", data=body, headers=h)
    assert r.status == 200, await r.text()


# ────────────────────────────────────────────────────────────────────
# /v1/info — no auth
# ────────────────────────────────────────────────────────────────────


async def test_info_no_auth(client):
    r = await client.get("/v1/info", headers=_HOST)
    assert r.status == 200
    j = await r.json()
    assert j["service"] == "puffo-agent-bridge"
    assert j["paired"] is False


async def test_info_reflects_pairing_state(client):
    user = make_user()
    await _pair(client, user)
    r = await client.get("/v1/info", headers=_HOST)
    j = await r.json()
    assert j["paired"] is True
    assert j["paired_slug"] == user.slug


# ────────────────────────────────────────────────────────────────────
# /v1/agents
# ────────────────────────────────────────────────────────────────────


async def test_list_agents_empty(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "GET", "/v1/agents"); h.update(_HOST)
    r = await client.get("/v1/agents", headers=h)
    j = await r.json()
    assert j["agents"] == []


async def test_list_marks_owned_correctly(client):
    user = make_user()
    home = isolated_home()  # fresh home so we control which agents exist
    user_root_pk = base64url_encode(user.root_key.public_key_bytes())
    other_root_pk = base64url_encode(b"\x99" * 32)
    write_test_agent(home, "owned-bot", owner_root_pubkey=user_root_pk)
    write_test_agent(home, "stranger-bot", owner_root_pubkey=other_root_pk)
    write_test_agent(home, "orphan-bot", owner_root_pubkey=None)

    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents"); h.update(_HOST)
        r = await c.get("/v1/agents", headers=h)
        j = await r.json()
    by_id = {a["id"]: a for a in j["agents"]}
    assert by_id["owned-bot"]["owned"] is True
    assert by_id["stranger-bot"]["owned"] is False
    assert by_id["orphan-bot"]["owned"] is False


async def test_get_agent_redacts_secrets_for_non_owner():
    user = make_user()
    home = isolated_home()
    other_root_pk = base64url_encode(b"\x99" * 32)
    write_test_agent(home, "stranger-bot", owner_root_pubkey=other_root_pk)
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/stranger-bot"); h.update(_HOST)
        r = await c.get("/v1/agents/stranger-bot", headers=h)
        j = await r.json()
    assert j["owned"] is False
    assert j["runtime"]["api_key"] is None
    # Boolean stays exposed so UI can still render "(set)".
    assert j["runtime"]["api_key_set"] is True


async def test_get_agent_exposes_secrets_for_owner():
    user = make_user()
    home = isolated_home()
    user_root_pk = base64url_encode(user.root_key.public_key_bytes())
    write_test_agent(home, "owned-bot", owner_root_pubkey=user_root_pk)
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/owned-bot"); h.update(_HOST)
        r = await c.get("/v1/agents/owned-bot", headers=h)
        j = await r.json()
    assert j["owned"] is True
    assert j["runtime"]["api_key"] == "sk-ant-test-secret"


async def test_get_agent_404(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "GET", "/v1/agents/nope"); h.update(_HOST)
    r = await client.get("/v1/agents/nope", headers=h)
    assert r.status == 404


# ────────────────────────────────────────────────────────────────────
# /v1/agents/{id}/files
# ────────────────────────────────────────────────────────────────────


async def test_list_files_returns_workspace_root():
    user = make_user()
    home = isolated_home()
    workspace = write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
        workspace_files={"README.md": "hi", "src/main.py": "print('x')\n"},
    )
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files", headers=h)
        j = await r.json()
    names = {e["name"]: e for e in j["entries"]}
    assert names["README.md"]["kind"] == "file"
    assert names["README.md"]["size"] == 2
    assert names["src"]["kind"] == "dir"


async def test_list_files_subdir():
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
        workspace_files={"src/main.py": "x"},
    )
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files?path=src"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files?path=src", headers=h)
        j = await r.json()
    assert [e["name"] for e in j["entries"]] == ["main.py"]


async def test_list_files_rejects_traversal():
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files?path=../../etc"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files?path=../../etc", headers=h)
        assert r.status == 400


async def test_list_files_rejects_absolute_path():
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files?path=/etc"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files?path=/etc", headers=h)
        assert r.status == 400


async def test_read_file_returns_text():
    user = make_user()
    home = isolated_home()
    write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
        workspace_files={"hello.txt": "hello world\n"},
    )
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files/raw?path=hello.txt"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files/raw?path=hello.txt", headers=h)
        assert r.status == 200
        assert (await r.text()) == "hello world\n"


async def test_read_file_rejects_binary():
    user = make_user()
    home = isolated_home()
    workspace = write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    (Path(workspace) / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files/raw?path=blob.bin"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files/raw?path=blob.bin", headers=h)
        assert r.status == 415


async def test_read_file_caps_size():
    user = make_user()
    home = isolated_home()
    workspace = write_test_agent(
        home, "files-bot", owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )
    # 2 MiB > 1 MiB cap
    (Path(workspace) / "big.txt").write_bytes(b"x" * (2 * 1024 * 1024))
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        await _pair(c, user)
        h = signed_headers(user, "GET", "/v1/agents/files-bot/files/raw?path=big.txt"); h.update(_HOST)
        r = await c.get("/v1/agents/files-bot/files/raw?path=big.txt", headers=h)
        assert r.status == 413
