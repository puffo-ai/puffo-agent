"""Agent-initiated leave requests. The agent calls leave_space /
leave_channel; the daemon DMs the operator for approval and signs the
leave only on a threaded ``y``. Mirrors the invite-reply gate, but
threaded-only (no direct/bulk path).
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent import puffo_core_client as pcc
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.http_client import HttpError


def _make_client(operator_slug: str = "op-1") -> PuffoCoreMessageClient:
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.device_id = "dev-1"
    client.operator_slug = operator_slug
    client._pending_leave_dms = {}
    client._gate_left_spaces = set()
    client._log = logging.getLogger("leave-routing-test")

    leave_calls: list[tuple] = []
    sent_dms: list[dict] = []

    async def _stub_sign_post(*, kind, space_id, channel_id):
        leave_calls.append((kind, space_id, channel_id))

    async def _stub_send_dm(recipient_slug, text, root_id=""):
        sent_dms.append({"to": recipient_slug, "text": text, "root_id": root_id})
        return {"envelope_id": f"env_dm_{len(sent_dms)}"}

    async def _stub_resolve_space(space_id):
        return {"sp_1": "Team"}.get(space_id, space_id)

    async def _stub_resolve_channel(*, space_id, channel_id):
        return {"ch_1": "general"}.get(channel_id, channel_id)

    client._sign_and_post_leave = _stub_sign_post  # type: ignore[assignment]
    client._send_dm = _stub_send_dm  # type: ignore[assignment]
    client._resolve_space_name = _stub_resolve_space  # type: ignore[assignment]
    client._resolve_channel_name = _stub_resolve_channel  # type: ignore[assignment]
    client._leave_calls = leave_calls  # type: ignore[attr-defined]
    client._sent_dms = sent_dms  # type: ignore[attr-defined]
    return client


# ─── request_leave_approval (DM + register pending) ────────────────


@pytest.mark.asyncio
async def test_request_leave_space_registers_pending_and_dms():
    client = _make_client()
    msg = await client.request_leave_approval(
        kind="leave_space", space_id="sp_1", channel_id="", reason="too noisy",
    )
    assert len(client._sent_dms) == 1
    dm = client._sent_dms[0]
    assert dm["to"] == "op-1"
    assert dm["root_id"] == ""
    assert "too noisy" in dm["text"]
    assert "**Team**" in dm["text"]
    meta = client._pending_leave_dms["env_dm_1"]
    assert meta["kind"] == "leave_space"
    assert meta["space_id"] == "sp_1"
    assert "Team" in msg


@pytest.mark.asyncio
async def test_request_leave_channel_resolves_name_and_omits_empty_reason():
    client = _make_client()
    await client.request_leave_approval(
        kind="leave_channel", space_id="sp_1", channel_id="ch_1", reason="",
    )
    meta = client._pending_leave_dms["env_dm_1"]
    assert meta["channel_name"] == "general"
    text = client._sent_dms[0]["text"]
    assert "**general**" in text
    assert "Reason:" not in text


@pytest.mark.asyncio
async def test_request_leave_no_operator_no_dm():
    client = _make_client(operator_slug="")
    msg = await client.request_leave_approval(
        kind="leave_space", space_id="sp_1", channel_id="", reason="",
    )
    assert "no operator" in msg.lower()
    assert client._sent_dms == []
    assert client._pending_leave_dms == {}


# ─── _maybe_handle_leave_reply (threaded y/n gate) ─────────────────


@pytest.mark.asyncio
async def test_threaded_y_signs_leave_and_confirms_in_thread():
    client = _make_client()
    client._pending_leave_dms["env_x"] = {
        "kind": "leave_space", "space_id": "sp_1", "channel_id": "",
        "space_name": "Team", "channel_name": None,
    }
    handled = await client._maybe_handle_leave_reply(
        thread_root_id="env_x", text="y",
    )
    assert handled is True
    assert client._leave_calls == [("leave_space", "sp_1", "")]
    assert "env_x" not in client._pending_leave_dms
    assert "sp_1" in client._gate_left_spaces  # suppresses the WS-echo DM
    confirm = client._sent_dms[-1]
    assert confirm["root_id"] == "env_x"
    assert confirm["text"].startswith("Left space **Team**")
    assert confirm["text"].endswith("✓")


@pytest.mark.asyncio
async def test_threaded_n_keeps_and_signs_nothing():
    client = _make_client()
    client._pending_leave_dms["env_x"] = {
        "kind": "leave_space", "space_id": "sp_1", "channel_id": "",
        "space_name": "Team", "channel_name": None,
    }
    handled = await client._maybe_handle_leave_reply(
        thread_root_id="env_x", text="n",
    )
    assert handled is True
    assert client._leave_calls == []
    assert "env_x" not in client._pending_leave_dms
    assert "sp_1" not in client._gate_left_spaces
    assert "stay" in client._sent_dms[-1]["text"].lower()
    assert client._sent_dms[-1]["root_id"] == "env_x"


@pytest.mark.asyncio
async def test_non_yn_threaded_reply_falls_through_untouched():
    client = _make_client()
    client._pending_leave_dms["env_x"] = {
        "kind": "leave_space", "space_id": "sp_1", "channel_id": "",
        "space_name": "Team", "channel_name": None,
    }
    handled = await client._maybe_handle_leave_reply(
        thread_root_id="env_x", text="why do you want to go?",
    )
    assert handled is False
    assert "env_x" in client._pending_leave_dms
    assert client._leave_calls == []
    assert client._sent_dms == []


@pytest.mark.asyncio
async def test_reply_in_unknown_thread_is_not_a_leave():
    client = _make_client()
    client._pending_leave_dms["env_x"] = {
        "kind": "leave_space", "space_id": "sp_1", "channel_id": "",
        "space_name": "Team", "channel_name": None,
    }
    handled = await client._maybe_handle_leave_reply(
        thread_root_id="env_other", text="y",
    )
    assert handled is False
    assert client._leave_calls == []


@pytest.mark.asyncio
async def test_channel_leave_does_not_set_space_suppression_flag():
    client = _make_client()
    client._pending_leave_dms["env_c"] = {
        "kind": "leave_channel", "space_id": "sp_1", "channel_id": "ch_1",
        "space_name": "Team", "channel_name": "general",
    }
    await client._maybe_handle_leave_reply(thread_root_id="env_c", text="y")
    assert client._leave_calls == [("leave_channel", "sp_1", "ch_1")]
    assert client._gate_left_spaces == set()
    assert "channel **general**" in client._sent_dms[-1]["text"]


@pytest.mark.asyncio
async def test_owner_rejection_is_reported_and_pending_cleared():
    client = _make_client()

    async def _boom(*, kind, space_id, channel_id):
        raise HttpError(
            403, '{"error":"FORBIDDEN","message":"space owner cannot leave"}',
        )

    client._sign_and_post_leave = _boom  # type: ignore[assignment]
    client._pending_leave_dms["env_x"] = {
        "kind": "leave_space", "space_id": "sp_1", "channel_id": "",
        "space_name": "Team", "channel_name": None,
    }
    handled = await client._maybe_handle_leave_reply(
        thread_root_id="env_x", text="y",
    )
    assert handled is True
    assert "owner" in client._sent_dms[-1]["text"].lower()
    assert "env_x" not in client._pending_leave_dms
    assert "sp_1" not in client._gate_left_spaces


# ─── _sign_and_post_leave (exact event payload) ────────────────────


@pytest.mark.asyncio
async def test_sign_and_post_leave_space_payload_shape(monkeypatch):
    posted: list[dict] = []

    class _FakeSess:
        subkey_secret_key = "sk"
        subkey_id = "subkey-1"

    class _FakeHttp:
        async def post(self, path, body):
            posted.append({"path": path, "body": body})

    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.device_id = "dev-1"
    client.keystore = type("KS", (), {"load_session": lambda self, slug: _FakeSess()})()
    client.http = _FakeHttp()

    monkeypatch.setattr(pcc, "decode_secret", lambda v: b"x")
    monkeypatch.setattr(pcc.Ed25519KeyPair, "from_secret_bytes", staticmethod(lambda b: "KEY"))
    monkeypatch.setattr(pcc, "random_nonce", lambda: "NONCE")
    monkeypatch.setattr(
        pcc, "sign_event",
        lambda **kw: {"kind": kw["kind"], "payload": kw["payload"]},
    )

    await client._sign_and_post_leave(
        kind="leave_space", space_id="sp_1", channel_id="",
    )
    assert posted[0]["path"] == "/spaces/events"
    assert posted[0]["body"]["space_id"] == "sp_1"
    event = posted[0]["body"]["events"][0]
    assert event["kind"] == "leave_space"
    payload = event["payload"]
    assert set(payload) == {"space_id", "effective_from", "nonce"}
    assert payload["space_id"] == "sp_1"
    assert "channel_id" not in payload
    assert "left_at" not in payload  # server uses effective_from


@pytest.mark.asyncio
async def test_sign_and_post_leave_channel_payload_includes_channel(monkeypatch):
    posted: list[dict] = []

    class _FakeSess:
        subkey_secret_key = "sk"
        subkey_id = "subkey-1"

    class _FakeHttp:
        async def post(self, path, body):
            posted.append({"path": path, "body": body})

    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.device_id = "dev-1"
    client.keystore = type("KS", (), {"load_session": lambda self, slug: _FakeSess()})()
    client.http = _FakeHttp()

    monkeypatch.setattr(pcc, "decode_secret", lambda v: b"x")
    monkeypatch.setattr(pcc.Ed25519KeyPair, "from_secret_bytes", staticmethod(lambda b: "KEY"))
    monkeypatch.setattr(pcc, "random_nonce", lambda: "NONCE")
    monkeypatch.setattr(
        pcc, "sign_event",
        lambda **kw: {"kind": kw["kind"], "payload": kw["payload"]},
    )

    await client._sign_and_post_leave(
        kind="leave_channel", space_id="sp_1", channel_id="ch_1",
    )
    payload = posted[0]["body"]["events"][0]["payload"]
    assert set(payload) == {"space_id", "channel_id", "effective_from", "nonce"}
    assert payload["channel_id"] == "ch_1"


# ─── _on_left_space dedup (suppress generic DM after gate leave) ────


@pytest.mark.asyncio
async def test_on_left_space_suppresses_generic_dm_after_gate_leave():
    client = _make_client()
    evicted: list[str] = []
    membership: list[str] = []

    async def _evict(space_id):
        evicted.append(space_id)

    async def _still(space_id):
        return False

    async def _membership_dm(text):
        membership.append(text)

    client._evict_space_caches = _evict  # type: ignore[assignment]
    client._still_member_of_space = _still  # type: ignore[assignment]
    client._dm_operator_membership_change = _membership_dm  # type: ignore[assignment]
    client._gate_left_spaces = {"sp_1"}

    await client._on_left_space(space_id="sp_1", synthetic=False)
    assert membership == []  # the gate already reported in-thread
    assert "sp_1" not in client._gate_left_spaces  # flag consumed
    assert evicted == ["sp_1"]  # cache cleanup still runs


@pytest.mark.asyncio
async def test_on_left_space_dms_when_not_gate_initiated():
    client = _make_client()
    membership: list[str] = []

    async def _evict(space_id):
        return None

    async def _still(space_id):
        return False

    async def _membership_dm(text):
        membership.append(text)

    client._evict_space_caches = _evict  # type: ignore[assignment]
    client._still_member_of_space = _still  # type: ignore[assignment]
    client._dm_operator_membership_change = _membership_dm  # type: ignore[assignment]

    await client._on_left_space(space_id="sp_1", synthetic=False)
    assert len(membership) == 1
    assert "Team" in membership[0]
