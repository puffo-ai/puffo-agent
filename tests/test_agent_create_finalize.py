"""finalize_and_pack writes the ws-local agent + a working .puffoagent bundle."""

import asyncio

from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.primitives import Ed25519KeyPair
from puffo_agent.portal.control.agent_create import finalize_and_pack, gen_agent_identity


def test_finalize_and_pack(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    operator_root = base64url_encode(Ed25519KeyPair.generate().public_key_bytes())
    ident = gen_agent_identity(operator_root)

    captured: dict = {}

    async def fake_finalize(binding: dict, token: str) -> None:
        captured["binding"] = binding
        captured["token"] = token

    result = asyncio.run(
        finalize_and_pack(
            ident,
            slug="helper-1234",
            pending_token="ptok_abc",
            operator_slug="op-99",
            server_url="https://chat.puffo.ai/relay",
            passcode="12345678",
            finalize_fn=fake_finalize,
            display_name="Helper",
        )
    )

    # The signed slug_binding + pending_token went to finalize.
    assert captured["token"] == "ptok_abc"
    assert captured["binding"]["slug"] == "helper-1234"
    assert captured["binding"]["self_signature"]

    assert result["agent_slug"] == "helper-1234"
    assert result["passcode"] == "12345678"

    # agent.yml written as ws-local with the operator linkage.
    from puffo_agent.portal.state import AgentConfig

    cfg = AgentConfig.load("helper-1234")
    assert cfg.runtime.kind == "ws-local"
    assert cfg.puffo_core.slug == "helper-1234"
    assert cfg.puffo_core.operator_slug == "op-99"
    assert cfg.puffo_core.device_id == ident.device_id

    # The bundle unpacks with the passcode and contains the agent.
    from puffo_agent.portal.export import unpack

    blob = open(result["bundle_path"], "rb").read()
    bundle = unpack(blob, "12345678")
    assert any(a.get("slug") == "helper-1234" for a in bundle.manifest["agents"])
