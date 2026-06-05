"""``serve_attached`` end to end: real session + bridge + endpoint, with
only the client / reporter / agent-config / auth faked.

Drives a full attach: handshake → Connected(profile) → a consumer batch
becomes a bundle → tool acks (consumer advances) → tool replies (relayed
to the client) → disconnect tears everything down.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from puffo_agent.portal.ws_local import auth as auth_mod
from puffo_agent.portal.ws_local import route as route_mod
from puffo_agent.portal.ws_local.auth import AuthedAgent
from puffo_agent.portal.ws_local.hub import AttachPoint, WsLocalHub
from puffo_agent.portal.ws_local.route import serve_attached


class FakeTransport:
    def __init__(self) -> None:
        self._inbound: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self):
        return await self._inbound.get()

    async def close(self) -> None:
        self.closed = True
        self._inbound.put_nowait(None)

    def feed(self, frame: dict) -> None:
        self._inbound.put_nowait(json.dumps(frame))

    def by_type(self, t: str) -> list:
        return [f for f in self.sent if f["type"] == t]


class FakeReporter:
    def __init__(self) -> None:
        self.heartbeats = 0
        self.stopped = False

    async def run_heartbeat_loop(self):
        self.heartbeats += 1
        await asyncio.Event().wait()  # runs until cancelled

    def stop(self):
        self.stopped = True

    async def begin_turn(self, message_id):
        return "run_x"

    async def end_turn_batch(self, runs):
        pass


class FakeClient:
    def __init__(self) -> None:
        self.replies: list = []
        self._on_message = None
        self._release = asyncio.Event()

    async def listen(self, on_message):
        # One batch, then stay parked until the connection tears us down.
        await on_message("r1", [{"envelope_id": "a", "text": "hi"}], {"channel_id": "c"})
        await self._release.wait()

    async def send_fallback_message(self, channel_id, text, root_id=""):
        self.replies.append((channel_id, text, root_id))


class FakeCfg:
    display_name = "Puffo Test"

    def resolve_profile_path(self):
        return "/nonexistent/profile.md"  # OSError → empty profile_md branch


@pytest.mark.asyncio
async def test_full_attach_flow(monkeypatch):
    hub = WsLocalHub()
    client = FakeClient()
    reporter = FakeReporter()
    hub.register(AttachPoint(
        slug="puffotest", agent_id="puffotest-1", agent_cfg=FakeCfg(),
        client=client, reporter=reporter, ack_timeout_s=180.0, ping_interval_s=30.0,
    ))
    monkeypatch.setattr(
        route_mod, "authenticate_bundle",
        lambda blob, pw: AuthedAgent("puffotest-1", "puffotest", "Puffo Test"),
    )

    t = FakeTransport()
    t.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})
    served = asyncio.ensure_future(serve_attached(t, hub))

    # Let the handshake + first consumer batch land, then ack + reply.
    for _ in range(6):
        await asyncio.sleep(0)
    connected = t.by_type("connected")
    assert connected and connected[0]["agent"]["display_name"] == "Puffo Test"
    bundle = t.by_type("bundle")[0]
    assert bundle["messages"][0]["envelope_id"] == "a"

    t.feed({"type": "reply", "channel_id": "c", "target_root_id": "r1", "text": "done"})
    t.feed({"type": "ack", "bundle_id": bundle["bundle_id"]})
    for _ in range(4):
        await asyncio.sleep(0)
    assert client.replies == [("c", "done", "r1")]
    assert reporter.heartbeats == 1  # online while attached

    # Tool disconnects → session ends → consumer + heartbeat torn down.
    t.feed({"type": "__close__"}) if False else t._inbound.put_nowait(None)
    await served
    assert reporter.stopped
    assert hub.registry.current("puffotest") is None  # slot freed


@pytest.mark.asyncio
async def test_unservable_slug_rejected(monkeypatch):
    hub = WsLocalHub()  # nothing registered
    monkeypatch.setattr(
        route_mod, "authenticate_bundle",
        lambda blob, pw: AuthedAgent("ghost-1", "ghost", "Ghost"),
    )
    t = FakeTransport()
    t.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})
    await serve_attached(t, hub)
    errors = t.by_type("error")
    assert errors and "not a ws-local agent" in errors[0]["reason"]
