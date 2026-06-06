"""Connection coordinator: handshake → auth → gate → single-WS → attach.

Drives ``serve_connection`` against fakes: every rejection path emits an
``error`` frame + close, the happy path hands back agent context and
runs the attached session+consumer, the slot is freed on exit (enabling
takeover), and a slot already held rejects the newcomer without evicting
the holder.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from puffo_agent.portal.ws_local.auth import AuthedAgent, AuthError
from puffo_agent.portal.ws_local.endpoint import serve_connection
from puffo_agent.portal.ws_local.registry import SessionRegistry


class FakeTransport:
    def __init__(self) -> None:
        self._inbound: list = []
        self.sent: list = []
        self.closed = False

    def feed(self, frame: dict) -> None:
        self._inbound.append(json.dumps(frame))

    def feed_raw(self, raw) -> None:
        self._inbound.append(raw)

    async def recv(self):
        return self._inbound.pop(0) if self._inbound else None

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True

    def sent_types(self) -> list:
        return [f["type"] for f in self.sent]


class _FakeBundle:
    bundle_id = "bdl_1"


class FakeSession:
    def __init__(self, bridge=None) -> None:
        self.bridge = bridge
        self.ran = False
        self.delivered: list = []
        self._stop = asyncio.Event()

    async def run(self) -> str:
        self.ran = True
        await self._stop.wait()
        return "stopped"

    async def deliver_batch(self, root_id, batch, channel_meta) -> None:
        self.delivered.append((root_id, batch, channel_meta))
        # Stand in for the frame-loop's ack so dispatch unblocks.
        await self.bridge.on_acked(_FakeBundle())


AUTHED = AuthedAgent("puffotest-19b1", "puffotest", "Puffo Test")


def _serve(transport, **overrides):
    built = []

    def make_session(authed, session_id, t, bridge):
        sess = overrides.get("session") or FakeSession(bridge)
        built.append((authed, session_id, sess, bridge))
        return sess

    async def default_context(slug):
        return overrides.get("context", {"slug": slug, "role": "cook"})

    agent_context = overrides.get("agent_context", default_context)

    async def start_consumer(authed, on_message):
        hook = overrides.get("consumer")
        if hook is not None:
            await hook(authed, on_message)
        # default: server stream "ends" immediately → tears down session

    kwargs = dict(
        authenticate=overrides.get("authenticate", lambda blob, pw: AUTHED),
        is_servable=overrides.get("is_servable", lambda slug: True),
        agent_context=agent_context,
        registry=overrides["registry"],
        make_session=make_session,
        start_consumer=overrides.get("start_consumer", start_consumer),
        new_session_id=lambda: "sess_1",
        base64_decode=overrides.get("base64_decode", lambda s: b"blob"),
    )
    return serve_connection(transport, **kwargs), built


# ── happy ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_handshake_attaches_then_releases_slot():
    reg: SessionRegistry = SessionRegistry()
    t = FakeTransport()
    t.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})
    coro, built = _serve(t, registry=reg)
    await coro

    assert t.sent_types() == ["connected"]
    assert t.sent[0]["agent"] == {"slug": "puffotest", "role": "cook"}
    assert built[0][1] == "sess_1" and built[0][2].ran  # session started
    assert reg.current("puffotest") is None  # released on exit


# ── rejection paths ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consumer_dispatch_routes_through_bridge():
    reg: SessionRegistry = SessionRegistry()
    t = FakeTransport()
    t.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})

    async def consumer(authed, on_message):
        await on_message("r1", [{"envelope_id": "a"}], {"channel_id": "c"})

    coro, built = _serve(t, registry=reg, consumer=consumer)
    await coro
    assert built[0][2].delivered == [("r1", [{"envelope_id": "a"}], {"channel_id": "c"})]


@pytest.mark.asyncio
async def test_release_and_close_on_error_after_acquire():
    """If completing the handshake raises after the slot is claimed, the
    slot is still freed and the socket closed (error swallowed + logged)."""
    reg: SessionRegistry = SessionRegistry()
    t = FakeTransport()
    t.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})

    async def boom_context(slug):
        raise RuntimeError("context load failed")

    coro, _ = _serve(t, registry=reg, agent_context=boom_context)
    await coro  # swallowed, not propagated
    assert reg.active_count() == 0
    assert t.closed


@pytest.mark.asyncio
async def test_consumer_exception_torn_down_cleanly():
    reg: SessionRegistry = SessionRegistry()
    t = FakeTransport()
    t.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})

    async def consumer(authed, on_message):
        raise RuntimeError("server stream died")

    coro, _ = _serve(t, registry=reg, consumer=consumer)
    await coro  # exception is logged, not propagated
    assert reg.current("puffotest") is None


@pytest.mark.asyncio
async def test_closed_before_handshake_is_silent():
    reg: SessionRegistry = SessionRegistry()
    t = FakeTransport()  # no frames → recv returns None
    coro, _ = _serve(t, registry=reg)
    await coro
    assert t.sent == []
    assert not t.closed


@pytest.mark.asyncio
async def test_first_frame_not_connect_rejected():
    reg: SessionRegistry = SessionRegistry()
    t = FakeTransport()
    t.feed({"type": "ack", "bundle_id": "b"})
    coro, _ = _serve(t, registry=reg)
    await coro
    assert t.sent_types() == ["error"]
    assert "connect" in t.sent[0]["reason"]
    assert t.closed


@pytest.mark.asyncio
async def test_malformed_first_frame_rejected():
    reg: SessionRegistry = SessionRegistry()
    t = FakeTransport()
    t.feed_raw("{not json")
    coro, _ = _serve(t, registry=reg)
    await coro
    assert t.sent_types() == ["error"]
    assert "malformed" in t.sent[0]["reason"]


@pytest.mark.asyncio
async def test_bad_base64_bundle_rejected():
    reg: SessionRegistry = SessionRegistry()
    t = FakeTransport()
    t.feed({"type": "connect", "bundle": "x", "password": "pw"})

    def boom(_s):
        raise ValueError("bad b64")

    coro, _ = _serve(t, registry=reg, base64_decode=boom)
    await coro
    assert t.sent_types() == ["error"]
    assert "base64" in t.sent[0]["reason"]


@pytest.mark.asyncio
async def test_auth_failure_rejected():
    reg: SessionRegistry = SessionRegistry()
    t = FakeTransport()
    t.feed({"type": "connect", "bundle": "Yg==", "password": "wrong"})

    def boom(_b, _p):
        raise AuthError("decryption failed")

    coro, _ = _serve(t, registry=reg, authenticate=boom)
    await coro
    assert t.sent_types() == ["error"]
    assert "authentication failed" in t.sent[0]["reason"]
    assert reg.active_count() == 0


@pytest.mark.asyncio
async def test_not_servable_rejected():
    reg: SessionRegistry = SessionRegistry()
    t = FakeTransport()
    t.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})
    coro, _ = _serve(t, registry=reg, is_servable=lambda slug: False)
    await coro
    assert t.sent_types() == ["error"]
    assert "not a ws-local agent" in t.sent[0]["reason"]
    assert reg.active_count() == 0


# ── single-WS ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slot_already_held_rejects_newcomer_without_evicting():
    reg: SessionRegistry = SessionRegistry()
    holder = FakeSession()
    reg.acquire("puffotest", holder)  # an existing live connection

    t = FakeTransport()
    t.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})
    coro, _ = _serve(t, registry=reg)
    await coro

    assert t.sent_types() == ["error"]
    assert "already has an active connection" in t.sent[0]["reason"]
    assert reg.current("puffotest") is holder  # holder kept


@pytest.mark.asyncio
async def test_takeover_after_previous_session_released():
    reg: SessionRegistry = SessionRegistry()
    for _ in range(2):
        t = FakeTransport()
        t.feed({"type": "connect", "bundle": "Yg==", "password": "pw"})
        coro, built = _serve(t, registry=reg)
        await coro
        assert built[0][2].ran  # each connection ran in turn
        assert reg.current("puffotest") is None


@pytest.mark.asyncio
async def test_reject_swallows_transport_errors():
    class BrokenTransport(FakeTransport):
        async def send(self, raw):
            raise ConnectionResetError("pipe gone")

        async def close(self):
            raise OSError("already closed")

    reg: SessionRegistry = SessionRegistry()
    t = BrokenTransport()
    t.feed({"type": "ack", "bundle_id": "b"})  # not a connect → reject path
    coro, _ = _serve(t, registry=reg)
    await coro  # must complete without propagating
    assert reg.active_count() == 0
