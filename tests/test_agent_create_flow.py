"""create_ws_local_agent ties validate → mint → request → approval → finalize."""

import asyncio

import pytest

from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.primitives import Ed25519KeyPair
from puffo_agent.portal.control import store as store_mod
from puffo_agent.portal.control.agent_create import create_ws_local_agent


def test_create_ws_local_agent_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    operator_root = base64url_encode(Ed25519KeyPair.generate().public_key_bytes())

    class FakePairing:
        operator_root_pubkey = operator_root
        server_url = "https://chat.puffo.ai/relay"

    monkeypatch.setattr(
        store_mod, "get_pairing", lambda slug: FakePairing() if slug == "op-99" else None
    )

    sent: dict = {}

    async def send_request(op_slug, payload):
        sent["op_slug"] = op_slug
        sent["payload"] = payload

    async def await_approval(request_id):
        sent["awaited_id"] = request_id
        return {"agent_slug": "helper-1234", "pending_token": "ptok"}

    async def finalize(binding, token):
        sent["finalize_token"] = token

    result = asyncio.run(
        create_ws_local_agent(
            "op-99",
            "12345678",
            send_request_fn=send_request,
            await_approval_fn=await_approval,
            finalize_fn=finalize,
            display_name="Helper",
        )
    )

    assert sent["op_slug"] == "op-99"
    assert sent["payload"]["type"] == "agent.create_request"
    assert "identity_cert" in sent["payload"] and "device_cert" in sent["payload"]
    # the request_id sent is the one awaited
    assert sent["payload"]["request_id"] == sent["awaited_id"]
    assert sent["finalize_token"] == "ptok"
    assert result["agent_slug"] == "helper-1234"


def test_unlinked_operator_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(store_mod, "get_pairing", lambda slug: None)

    async def noop(*a, **k):
        return {}

    with pytest.raises(ValueError, match="not linked"):
        asyncio.run(
            create_ws_local_agent(
                "nope",
                "x",
                send_request_fn=noop,
                await_approval_fn=noop,
                finalize_fn=noop,
            )
        )


def test_pending_approvals_resolve():
    from puffo_agent.portal.control.agent_create import PendingApprovals

    async def go():
        p = PendingApprovals()

        async def resolver():
            await asyncio.sleep(0.01)
            assert p.resolve("r1", {"agent_slug": "s", "pending_token": "t"})

        task = asyncio.ensure_future(resolver())
        result = await p.wait("r1", timeout=1.0)
        await task
        return result

    assert asyncio.run(go())["agent_slug"] == "s"


def test_pending_approvals_timeout():
    from puffo_agent.portal.control.agent_create import PendingApprovals

    async def go():
        p = PendingApprovals()
        with pytest.raises(asyncio.TimeoutError):
            await p.wait("r2", timeout=0.05)

    asyncio.run(go())
