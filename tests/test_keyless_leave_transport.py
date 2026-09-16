"""Transport branch inside ``_sign_and_post_leave``.

``test_leave_request_routing`` covers the approval gate by stubbing
``_sign_and_post_leave`` out; this file exercises the function itself, where a
keyless cloud agent and a native agent diverge.

The distinction that matters: a cloud agent has no local keystore session, so
the native path's ``keystore.load_session`` would raise *after* the operator
already approved. The keyless branch posts to the server-side route instead
and lets puffo-server emit the event in the agent's name (PUF-395).
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent import puffo_core_client as pcc
from puffo_agent.agent.event_kinds import EventKind
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


class _Keystore:
    """Stands in for a cloud agent's absent keystore: touching it is a bug."""

    def load_session(self, slug):  # noqa: D102 - test double
        raise AssertionError(
            "keyless leave must not touch the keystore — that is the bug "
            "PUF-395 fixes"
        )


class _Http:
    def __init__(self, *, keyless: bool) -> None:
        self.keyless = keyless
        self.unsigned: list[tuple[str, dict | None]] = []
        self.signed: list[tuple[str, dict]] = []

    async def post_unsigned(self, path, body=None):
        self.unsigned.append((path, body))
        return {"ok": True}

    async def post(self, path, body):
        self.signed.append((path, body))
        return {"ok": True}


def _client(*, keyless: bool) -> PuffoCoreMessageClient:
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-a1b2"
    client.device_id = "dev_a1b2"
    client._bridge = None
    client._log = logging.getLogger("keyless-leave-test")
    client.http = _Http(keyless=keyless)
    client.keystore = _Keystore()
    return client


# ── keyless: the cloud agent path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_keyless_leave_space_posts_unsigned_route():
    client = _client(keyless=True)

    await client._sign_and_post_leave(
        kind=EventKind.LEAVE_SPACE, space_id="sp_desk", channel_id=""
    )

    assert client.http.unsigned == [("/v2/cloud-agents/spaces/sp_desk/leave", None)]
    assert client.http.signed == [], "keyless must not hit /spaces/events"


@pytest.mark.asyncio
async def test_keyless_leave_channel_posts_channel_route():
    client = _client(keyless=True)

    await client._sign_and_post_leave(
        kind=EventKind.LEAVE_CHANNEL, space_id="sp_desk", channel_id="ch_7e21"
    )

    assert client.http.unsigned == [
        ("/v2/cloud-agents/spaces/sp_desk/channels/ch_7e21/leave", None)
    ]
    assert client.http.signed == []


@pytest.mark.asyncio
async def test_keyless_leave_url_quotes_ids():
    """Ids reach the path unescaped otherwise; a slug with a slash would
    silently address a different route."""
    client = _client(keyless=True)

    await client._sign_and_post_leave(
        kind=EventKind.LEAVE_CHANNEL, space_id="sp/a b", channel_id="ch/c d"
    )

    path, _ = client.http.unsigned[0]
    assert path == "/v2/cloud-agents/spaces/sp%2Fa%20b/channels/ch%2Fc%20d/leave"


@pytest.mark.asyncio
async def test_keyless_leave_never_touches_the_keystore():
    """The regression this whole change exists for: before it, the keystore
    read raised here and the operator saw an error in the thread where they
    had just approved."""
    client = _client(keyless=True)

    # _Keystore.load_session raises AssertionError if reached.
    await client._sign_and_post_leave(
        kind=EventKind.LEAVE_SPACE, space_id="sp_desk", channel_id=""
    )


@pytest.mark.asyncio
async def test_bridge_transport_counts_as_keyless():
    """``signed_http_available`` is False when a bridge is attached, even if
    the http client does not carry the keyless flag."""
    client = _client(keyless=False)
    client._bridge = object()

    await client._sign_and_post_leave(
        kind=EventKind.LEAVE_SPACE, space_id="sp_desk", channel_id=""
    )

    assert client.http.unsigned[0][0] == "/v2/cloud-agents/spaces/sp_desk/leave"


# ── native: unchanged ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_native_leave_still_signs_and_posts_spaces_events(monkeypatch):
    client = _client(keyless=False)

    class _Session:
        subkey_secret_key = "c2VjcmV0"
        subkey_id = "sk_1"

    client.keystore = type("_KS", (), {"load_session": lambda self, slug: _Session()})()

    monkeypatch.setattr(
        pcc.Ed25519KeyPair, "from_secret_bytes", staticmethod(lambda raw: "KEY")
    )
    monkeypatch.setattr(pcc, "decode_secret", lambda value: b"secret")
    monkeypatch.setattr(pcc, "random_nonce", lambda: "nonce-1")
    monkeypatch.setattr(
        pcc, "sign_event", lambda **kw: {"kind": kw["kind"], "payload": kw["payload"]}
    )

    await client._sign_and_post_leave(
        kind=EventKind.LEAVE_SPACE, space_id="sp_desk", channel_id=""
    )

    assert client.http.unsigned == [], "native must not hit the keyless route"
    assert len(client.http.signed) == 1
    path, body = client.http.signed[0]
    assert path == "/spaces/events"
    assert body["space_id"] == "sp_desk"
    assert body["events"][0]["kind"] == EventKind.LEAVE_SPACE
    assert body["events"][0]["payload"]["space_id"] == "sp_desk"
    assert "effective_from" in body["events"][0]["payload"]
