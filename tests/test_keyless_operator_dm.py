"""Operator-facing DMs from a keyless cloud agent.

Every path that needs human attention funnels through ``_send_dm`` — the leave
approval prompt above all. ``send_direct_message`` signs with the local
keystore, which a cloud agent does not have, so before this branch existed the
prompt died with ``session not found: <slug>``, the tool handed that string
back to the model, and the operator was never asked anything.

Observed on staging 2026-09-17 with `bensanders-4063-b646ee66`: "leave_space
and leave_channel both returned session not found ... so no approval DM was
ever sent to you."
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


class _Keystore:
    """A cloud agent's absent keystore — reaching it is the bug."""

    def load_session(self, slug):  # noqa: D102 - test double
        raise FileNotFoundError(f"session not found: {slug}")


class _Bridge:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_send(self, **kwargs):
        self.sent.append(kwargs)
        return {
            "type": "ack",
            "envelope_id": f"env_{len(self.sent)}",
            "devices_queued": 1,
        }


def _client(*, bridge) -> PuffoCoreMessageClient:
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "bensanders-4063-b646ee66"
    client.device_id = "dev_ben"
    client.operator_slug = "shan-chalk-0e85"
    client.keystore = _Keystore()
    client.http = object()
    client._bridge = bridge
    client._log = logging.getLogger("keyless-dm-test")
    return client


@pytest.mark.asyncio
async def test_keyless_dm_goes_over_the_bridge():
    bridge = _Bridge()
    client = _client(bridge=bridge)

    out = await client._send_dm("shan-chalk-0e85", "approve my leave?", "")

    assert len(bridge.sent) == 1
    assert bridge.sent[0]["recipient_slug"] == "shan-chalk-0e85"
    assert bridge.sent[0]["plaintext"] == "approve my leave?"
    assert out["envelope_id"] == "env_1", "callers key pending prompts on this"


@pytest.mark.asyncio
async def test_keyless_dm_never_touches_the_keystore():
    """The regression itself: `session not found` reached the model as a tool
    result, and the operator was never asked."""
    client = _client(bridge=_Bridge())

    # _Keystore.load_session raises FileNotFoundError if reached.
    await client._send_dm("shan-chalk-0e85", "hello", "")


@pytest.mark.asyncio
async def test_keyless_dm_carries_the_thread_root():
    bridge = _Bridge()
    client = _client(bridge=bridge)

    await client._send_dm("shan-chalk-0e85", "Left LasVegas. ✓", "env_prompt_1")

    assert bridge.sent[0]["thread_root_id"] == "env_prompt_1", (
        "the confirmation must land in the approval thread, not a new DM"
    )


@pytest.mark.asyncio
async def test_root_post_sends_no_thread_root():
    bridge = _Bridge()
    client = _client(bridge=bridge)

    await client._send_dm("shan-chalk-0e85", "approve my leave?", "")

    assert bridge.sent[0]["thread_root_id"] is None


@pytest.mark.asyncio
async def test_native_client_still_signs(monkeypatch):
    """No bridge attached ⇒ unchanged: the signed HTTP path."""
    import puffo_agent.agent.puffo_core_client as pcc

    calls: list[dict] = []

    async def _fake_send_direct_message(**kwargs):
        calls.append(kwargs)
        return {"envelope_id": "env_signed"}

    monkeypatch.setattr(pcc, "send_direct_message", _fake_send_direct_message)

    client = _client(bridge=None)
    out = await client._send_dm("shan-chalk-0e85", "hi", "root_1")

    assert len(calls) == 1
    assert calls[0]["recipient_slug"] == "shan-chalk-0e85"
    assert calls[0]["root_id"] == "root_1"
    assert out["envelope_id"] == "env_signed"
