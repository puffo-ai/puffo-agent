"""Frame codec for the localhost WS protocol.

Covers every encode branch, every decode branch, and the unhappy
paths decode must reject (bad JSON, non-object, unknown type, missing
or wrong-typed fields).
"""

from __future__ import annotations

import json

import pytest

from puffo_agent.portal.ws_local.protocol import (
    Ack,
    Connect,
    Connected,
    End,
    Error,
    Ping,
    Pong,
    ProtocolError,
    SendBundle,
    ToolCall,
    ToolResult,
    decode_inbound,
    encode,
)


# ── encode ───────────────────────────────────────────────────────────────────


def test_encode_connected_carries_agent_context():
    frame = Connected("sess_1", {"slug": "alice", "role": "cook"})
    assert json.loads(encode(frame)) == {
        "type": "connected",
        "session_id": "sess_1",
        "agent": {"slug": "alice", "role": "cook"},
    }


def test_encode_connected_defaults_agent_to_empty():
    assert json.loads(encode(Connected("sess_1")))["agent"] == {}


def test_encode_bundle_round_trip_fields():
    frame = SendBundle(
        bundle_id="bdl_1",
        root_id="root_1",
        channel_meta={"channel_id": "ch_1"},
        messages=[{"envelope_id": "e1", "text": "hi"}],
    )
    decoded = json.loads(encode(frame))
    assert decoded["type"] == "bundle"
    assert decoded["bundle_id"] == "bdl_1"
    assert decoded["root_id"] == "root_1"
    assert decoded["channel_meta"] == {"channel_id": "ch_1"}
    assert decoded["messages"][0]["envelope_id"] == "e1"


def test_encode_ping_pong():
    assert json.loads(encode(Ping())) == {"type": "ping"}
    assert json.loads(encode(Pong())) == {"type": "pong"}


def test_encode_error():
    assert json.loads(encode(Error("nope"))) == {"type": "error", "reason": "nope"}


def test_encode_rejects_unknown_frame():
    with pytest.raises(ProtocolError):
        encode(Ack("bdl_1"))  # Ack is inbound-only


# ── decode happy ─────────────────────────────────────────────────────────────


def test_decode_connect():
    raw = json.dumps({"type": "connect", "bundle": "YmxvYg==", "password": "pw"})
    assert decode_inbound(raw) == Connect("YmxvYg==", "pw")


def test_decode_ack():
    assert decode_inbound(json.dumps({"type": "ack", "bundle_id": "b"})) == Ack("b")


def test_decode_end():
    assert decode_inbound(json.dumps({"type": "end", "bundle_id": "b"})) == End("b")


def test_decode_tool_call_round_trip():
    frame = decode_inbound(json.dumps({
        "type": "tool_call",
        "command_id": "cmd_1",
        "tool": "send_message",
        "params": {"channel": "ch_1", "text": "hi", "is_visible_to_human": True},
    }))
    assert frame == ToolCall(
        command_id="cmd_1",
        tool="send_message",
        params={"channel": "ch_1", "text": "hi", "is_visible_to_human": True},
    )


def test_decode_tool_call_defaults_params_to_empty_dict():
    frame = decode_inbound(json.dumps({
        "type": "tool_call", "command_id": "cmd_2", "tool": "noop",
    }))
    assert frame == ToolCall(command_id="cmd_2", tool="noop", params={})


def test_decode_tool_call_rejects_non_object_params():
    with pytest.raises(ProtocolError):
        decode_inbound(json.dumps({
            "type": "tool_call", "command_id": "x", "tool": "y", "params": [],
        }))


def test_encode_tool_result_ok_carries_result():
    body = json.loads(encode(ToolResult(command_id="cmd_1", ok=True, result="done")))
    assert body == {"type": "tool_result", "command_id": "cmd_1", "ok": True, "result": "done"}


def test_encode_tool_result_error_carries_reason():
    body = json.loads(encode(ToolResult(command_id="cmd_1", ok=False, error="nope")))
    assert body == {"type": "tool_result", "command_id": "cmd_1", "ok": False, "error": "nope"}


def test_decode_ping_pong():
    assert decode_inbound(json.dumps({"type": "ping"})) == Ping()
    assert decode_inbound(json.dumps({"type": "pong"})) == Pong()


# ── decode unhappy ───────────────────────────────────────────────────────────


def test_decode_rejects_bad_json():
    with pytest.raises(ProtocolError):
        decode_inbound("{not json")


def test_decode_rejects_non_object():
    with pytest.raises(ProtocolError):
        decode_inbound("[1, 2, 3]")


def test_decode_rejects_unknown_type():
    with pytest.raises(ProtocolError):
        decode_inbound(json.dumps({"type": "explode"}))


@pytest.mark.parametrize("frame", [
    {"type": "connect", "password": "pw"},          # no bundle
    {"type": "connect", "bundle": "b"},             # no password
    {"type": "connect", "bundle": "", "password": "pw"},  # empty bundle
    {"type": "ack"},
    {"type": "reply", "channel_id": "c"},
])
def test_decode_rejects_missing_required_fields(frame):
    with pytest.raises(ProtocolError):
        decode_inbound(json.dumps(frame))
