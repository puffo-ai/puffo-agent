"""Non-blocking create: start_create stashes + sends; finalize_from_command
finalizes on the approval command; CreateRegistry bridges request → result."""

import asyncio

import pytest

from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.primitives import Ed25519KeyPair
from puffo_agent.portal.control import agent_create
from puffo_agent.portal.control import reporter as reporter_mod
from puffo_agent.portal.control import store as store_mod

OPERATOR_ROOT = base64url_encode(Ed25519KeyPair.generate().public_key_bytes())


class _FakePairing:
    operator_root_pubkey = OPERATOR_ROOT
    server_url = "https://chat.puffo.ai/relay"


def test_start_create_stashes_and_sends(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(
        store_mod, "get_pairing", lambda slug: _FakePairing() if slug == "op-99" else None
    )

    sent: dict = {}

    class FakeReporter:
        async def send_to_operator(self, op_slug, payload):
            sent["op_slug"] = op_slug
            sent["payload"] = payload

    monkeypatch.setattr(reporter_mod, "get_reporter", lambda: FakeReporter())

    started = asyncio.run(
        agent_create.start_create("op-99", "12345678", username="Helper", message="need a coder")
    )
    assert started["request_id"].startswith("acr_")
    assert sent["op_slug"] == "op-99"
    assert sent["payload"]["type"] == "agent.create_request"
    assert sent["payload"]["message"] == "need a coder"
    # identity stashed under the request_id, retrievable by the approval command
    assert agent_create.get_registry().pop_pending(started["request_id"]) is not None


def test_start_create_rejects_unlinked(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(store_mod, "get_pairing", lambda slug: None)
    with pytest.raises(ValueError, match="not linked"):
        asyncio.run(agent_create.start_create("nope", "x"))


def test_registry_record_then_wait():
    async def go():
        reg = agent_create.CreateRegistry()

        async def resolver():
            await asyncio.sleep(0.01)
            reg.record_result("cmd1", {"ok": True, "agent_slug": "s"})

        task = asyncio.ensure_future(resolver())
        result = await reg.wait_result("cmd1", timeout=1.0)
        await task
        return result

    assert asyncio.run(go())["agent_slug"] == "s"


def test_registry_wait_timeout():
    async def go():
        reg = agent_create.CreateRegistry()
        with pytest.raises(asyncio.TimeoutError):
            await reg.wait_result("nope", timeout=0.05)

    asyncio.run(go())


def test_finalize_from_command_writes_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    ident = agent_create.gen_agent_identity(OPERATOR_ROOT)
    agent_create.get_registry().put_pending(
        "acr_x",
        agent_create._PendingCreate(ident, "op-99", "https://chat.puffo.ai/relay", "12345678"),
    )

    async def fake_post(server_url, binding, token):
        return None

    monkeypatch.setattr(agent_create, "post_slug_binding", fake_post)

    result = asyncio.run(
        agent_create.finalize_from_command(
            "acr_x",
            {
                "agent_slug": "helper-1234",
                "pending_token": "ptok",
                "name": "Helper",
                "role": "coder",
                "space_id": "sp_1",
            },
        )
    )
    assert result["agent_slug"] == "helper-1234"

    from puffo_agent.portal.state import AgentConfig

    ac = AgentConfig.load("helper-1234")
    assert ac.puffo_core.space_id == "sp_1"
    assert ac.role == "coder"
    assert ac.runtime.kind == "ws-local"


def test_finalize_from_command_unknown_request():
    with pytest.raises(ValueError, match="no pending create"):
        asyncio.run(agent_create.finalize_from_command("acr_missing", {}))


def test_cmd_agent_create_seeds_profile_briefing(tmp_path, monkeypatch):
    """`puffo-agent agent create` seeds briefing/profile.md with the
    managed identity block, so a freshly created agent's first prompt
    rebuild already has managed identity framing."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    from puffo_agent.agent.memory import (
        PROFILE_MANAGED_BEGIN,
        PROFILE_MANAGED_END,
    )
    from puffo_agent.portal.cli import build_parser

    # cli-local resolves the API key without prompting, so the create
    # flow runs headless.
    args = build_parser().parse_args([
        "agent", "create",
        "--id", "helper-0001",
        "--display-name", "Helper",
        "--role", "coder: writes code",
        "--runtime", "cli-local",
    ])
    assert args.func(args) == 0

    profile_briefing = (
        tmp_path / "agents" / "helper-0001" / "memory" / "briefing"
        / "profile.md"
    )
    assert profile_briefing.is_file()
    text = profile_briefing.read_text(encoding="utf-8")
    assert PROFILE_MANAGED_BEGIN in text
    assert PROFILE_MANAGED_END in text
    # Identity fields from the create args land inside the managed block.
    assert "Helper" in text
    assert "coder: writes code" in text
