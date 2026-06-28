"""api-puffo runner: envelope decrypt + LLM turn + tool dispatch.

Uses an aiohttp TestServer as the mock cloud and drives one
end-to-end turn — verifies decrypt → llm_complete → tool_use →
dispatch_tool round-trips correctly."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.api_puffo.bundle import (
    ApiPuffoBundle,
    materialise_agent_dir,
)
from puffo_agent.agent.api_puffo.cloud_client import CloudHttpClient
from puffo_agent.agent.api_puffo.tools import TOOL_SCHEMAS, dispatch_tool
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.message import (
    EncryptInput,
    RecipientDevice,
    encrypt_message,
)
from puffo_agent.crypto.primitives import Ed25519KeyPair, KemKeyPair


def _isolated_home() -> str:
    home = tempfile.mkdtemp(prefix="puffo-api-puffo-run-")
    os.environ["PUFFO_AGENT_HOME"] = home
    os.environ["PUFFO_HOME"] = home
    Path(home, "agents").mkdir(parents=True, exist_ok=True)
    return home


def _make_bundle_with_real_kem(
    agent_slug: str, cloud_url: str,
) -> tuple[ApiPuffoBundle, KemKeyPair]:
    kp = KemKeyPair.generate()
    raw = {
        "agent_slug": agent_slug,
        "operator_slug": "user-test",
        "device_id": "dev_test_cloud",
        "kem_secret_key": base64url_encode(kp.secret_bytes()),
        "kem_cert": {"type": "device_cert", "version": 1, "device_id": "dev_test_cloud"},
        "session_token": "tok_test_xyz",
        "puffo_cloud_server_url": cloud_url,
        "display_name": "Test Bot",
        "role": "tester: api-puffo runtime",
        "role_short": "tester",
        "soul": "I am the api-puffo end-to-end test bot.",
        "avatar_url": "",
        "api_key": "sk-mock",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    }
    return ApiPuffoBundle.from_dict(raw), kp


# ── tool dispatch ────────────────────────────────────────────────


def test_tool_schemas_cover_4_load_bearing_tools():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {"send_message", "get_channel_history",
                     "get_thread_history", "whoami"}


@pytest.mark.asyncio
async def test_dispatch_tool_posts_to_cloud_endpoint():
    received: list[tuple[str, dict]] = []

    async def send_message(request: web.Request) -> web.Response:
        received.append(("send_message", await request.json()))
        return web.json_response({"text": "posted msg_xxxx"})

    app = web.Application()
    app.router.add_post("/v1/send_message", send_message)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url(""))
        http = CloudHttpClient(url, "tok_test")
        try:
            result = await dispatch_tool(http, "send_message", {
                "channel": "@alice",
                "text": "hello",
                "is_visible_to_human": True,
            })
        finally:
            await http.close()
    assert result == "posted msg_xxxx"
    assert len(received) == 1
    name, body = received[0]
    assert body == {"channel": "@alice", "text": "hello", "is_visible_to_human": True}


@pytest.mark.asyncio
async def test_dispatch_tool_returns_error_string_on_http_failure():
    async def boom(request: web.Request) -> web.Response:
        return web.json_response({"error": "nope"}, status=503)

    app = web.Application()
    app.router.add_post("/v1/whoami", boom)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url(""))
        http = CloudHttpClient(url, "tok_test")
        try:
            result = await dispatch_tool(http, "whoami", {})
        finally:
            await http.close()
    assert result.startswith("error: HTTP 503")


@pytest.mark.asyncio
async def test_dispatch_tool_unknown_name():
    http = CloudHttpClient("http://127.0.0.1:1", "tok_test")
    try:
        result = await dispatch_tool(http, "nonexistent_tool", {})
    finally:
        await http.close()
    assert result == "error: unknown tool 'nonexistent_tool'"


# ── runner end-to-end: decrypt + LLM turn + tool call ────────────


@pytest.mark.asyncio
async def test_runner_end_to_end_decrypt_llm_tool():
    _isolated_home()

    # Mock cloud: implements /v1/llm/complete + /v1/send_message.
    llm_calls: list[dict] = []
    tool_calls: list[dict] = []

    async def llm_complete(request: web.Request) -> web.Response:
        body = await request.json()
        llm_calls.append(body)
        msgs = body.get("messages") or []
        last = msgs[-1] if msgs else {}
        last_content = last.get("content")
        # Round 2: tool_result came back → emit final text.
        if isinstance(last_content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result"
            for c in last_content
        ):
            return web.json_response({
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "all done"}],
            })
        # Round 1: ask to call send_message.
        return web.json_response({
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_001",
                    "name": "send_message",
                    "input": {
                        "channel": "@smoker",
                        "text": "echo: hi",
                        "is_visible_to_human": True,
                    },
                },
            ],
        })

    async def send_message(request: web.Request) -> web.Response:
        tool_calls.append(await request.json())
        return web.json_response({"text": "posted msg_xxxx"})

    app = web.Application()
    app.router.add_post("/v1/llm/complete", llm_complete)
    app.router.add_post("/v1/send_message", send_message)

    async with TestClient(TestServer(app)) as client:
        cloud_url = str(client.make_url("")).rstrip("/")

        # Provision the api-puffo agent on disk using a bundle.
        bundle, agent_kem = _make_bundle_with_real_kem(
            agent_slug="rb-bot", cloud_url=cloud_url,
        )
        materialise_agent_dir(bundle)

        # Build a fake inbound envelope from a foreign sender.
        sender_signing = Ed25519KeyPair.generate()
        device = RecipientDevice(
            device_id=bundle.device_id,
            kem_public_key=agent_kem.public_key_bytes(),
        )
        envelope = encrypt_message(
            EncryptInput(
                envelope_kind="dm",
                sender_slug="smoker",
                sender_subkey_id="subkey_test_001",
                is_visible_to_human=True,
                recipient_slug=bundle.agent_slug,
                content_type="text/plain",
                content="hi",
                recipients=[device],
            ),
            sender_signing,
        )

        # Drive the runner one frame deep — skip the WS by calling
        # ``_handle_envelope_frame`` directly.
        from puffo_agent.agent.api_puffo.runner import ApiPuffoRunner
        from puffo_agent.agent.api_puffo.keystore import ApiPuffoKeystore
        from puffo_agent.crypto.primitives import KemKeyPair
        from puffo_agent.crypto.encoding import base64url_decode
        from puffo_agent.portal.state import AgentConfig

        runner = ApiPuffoRunner("rb-bot", asyncio.Event())
        runner._keys = ApiPuffoKeystore.for_agent("rb-bot")
        runner._cfg = AgentConfig.load("rb-bot")
        runner._kem_kp = KemKeyPair.from_secret_bytes(
            base64url_decode(runner._keys.kem_secret_key),
        )
        runner._http = CloudHttpClient(cloud_url, runner._keys.session_token)
        try:
            await runner._handle_envelope_frame({
                "type": "envelope",
                "envelope": envelope,
                "sender_signing_public_key": base64url_encode(
                    sender_signing.public_key_bytes(),
                ),
            })
        finally:
            await runner._http.close()

    # Two LLM rounds, one tool POST.
    assert len(llm_calls) == 2
    assert llm_calls[0]["provider"] == "anthropic"
    assert llm_calls[0]["model"] == "claude-sonnet-4-6"
    assert llm_calls[0]["api_key"] == "sk-mock"
    assert "I am the api-puffo end-to-end test bot." in llm_calls[0]["system_prompt"]
    assert llm_calls[0]["messages"][0]["content"] == "hi"
    assert len(tool_calls) == 1
    assert tool_calls[0]["channel"] == "@smoker"
    assert tool_calls[0]["text"] == "echo: hi"


@pytest.mark.asyncio
async def test_runner_skips_non_text_content_type():
    _isolated_home()
    # No mock cloud needed — the early return should fire before any
    # HTTP traffic.
    bundle, agent_kem = _make_bundle_with_real_kem(
        agent_slug="nt-bot", cloud_url="http://127.0.0.1:1",
    )
    materialise_agent_dir(bundle)

    sender_signing = Ed25519KeyPair.generate()
    envelope = encrypt_message(
        EncryptInput(
            envelope_kind="dm",
            sender_slug="smoker",
            sender_subkey_id="subkey_test_002",
            is_visible_to_human=True,
            recipient_slug=bundle.agent_slug,
            content_type="image/png",
            content="binary blob placeholder",
            recipients=[RecipientDevice(
                device_id=bundle.device_id,
                kem_public_key=agent_kem.public_key_bytes(),
            )],
        ),
        sender_signing,
    )

    from puffo_agent.agent.api_puffo.runner import ApiPuffoRunner
    from puffo_agent.agent.api_puffo.keystore import ApiPuffoKeystore
    from puffo_agent.crypto.encoding import base64url_decode
    from puffo_agent.portal.state import AgentConfig

    runner = ApiPuffoRunner("nt-bot", asyncio.Event())
    runner._keys = ApiPuffoKeystore.for_agent("nt-bot")
    runner._cfg = AgentConfig.load("nt-bot")
    runner._kem_kp = KemKeyPair.from_secret_bytes(
        base64url_decode(runner._keys.kem_secret_key),
    )
    runner._http = CloudHttpClient("http://127.0.0.1:1", "tok")
    try:
        # Should NOT raise (we'd see ClientConnectorError if the
        # runner tried to call _run_turn → llm_complete).
        await runner._handle_envelope_frame({
            "type": "envelope",
            "envelope": envelope,
            "sender_signing_public_key": base64url_encode(
                sender_signing.public_key_bytes(),
            ),
        })
    finally:
        await runner._http.close()
