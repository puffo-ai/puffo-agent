"""Wire frames for the localhost WS protocol.

Direction:
  daemon → tool : ``connected``, ``bundle``, ``ping``, ``pong``
  tool → daemon : ``connect``, ``ack``, ``reply``, ``ping``, ``pong``

Frames are plaintext JSON — the daemon has already decrypted inbound
messages and will encrypt outbound replies, so tools never touch the
Puffo crypto. ``decode_inbound`` is strict: unknown type or a missing
required field raises ``ProtocolError`` rather than guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class ProtocolError(Exception):
    """Malformed frame: bad JSON, unknown type, or missing field."""


# ── tool → daemon ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Connect:
    """Handshake opener. The tool proves it holds the agent's
    ``.puffoagent`` export and its password by sending both; the daemon
    authenticates by decrypting (``auth.authenticate_bundle``). ``bundle``
    is the base64 export blob."""

    bundle: str
    password: str


@dataclass(frozen=True)
class Ack:
    """Optional "I've started" signal. Idempotent: a duplicate ack
    or an ack landing after ``End`` is a no-op. The daemon may use
    it to flip the agent's external status from idle to working."""

    bundle_id: str


@dataclass(frozen=True)
class End:
    """Terminates work on a bundle: the daemon closes the turn,
    advances the cursor, and pumps the next bundle. Idempotent —
    duplicate ``End`` is a no-op. An agent that decides not to reply
    can send ``End`` directly without a preceding ``Ack``."""

    bundle_id: str


@dataclass(frozen=True)
class ToolCall:
    """RPC-style call to one of the ``WS_LOCAL_ALLOWED_TOOLS``. The
    daemon dispatches to the matching ``puffo_core_tools`` handler and
    returns a ``ToolResult`` keyed on ``command_id``. ``params`` is a
    flat dict matching the tool's keyword args."""

    command_id: str
    tool: str
    params: dict[str, Any] = field(default_factory=dict)


# ── daemon → tool ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Connected:
    session_id: str
    # The daemon's live agent context (role / profile.md / …) for the
    # tool to configure itself with. Opaque on the wire — the tool owns
    # interpretation.
    agent: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Error:
    """Terminal rejection sent just before the daemon closes the socket
    (bad password, agent not servable, slot already held)."""

    reason: str


@dataclass(frozen=True)
class SendBundle:
    bundle_id: str
    root_id: str
    channel_meta: dict[str, Any]
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ToolResult:
    """Response to a ``ToolCall``. ``ok=True`` ⇒ ``result`` carries the
    tool's return value (string for puffo_core_tools); ``ok=False`` ⇒
    ``error`` carries a one-line reason."""

    command_id: str
    ok: bool
    result: Any = None
    error: str = ""


# ── bidirectional ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Ping:
    pass


@dataclass(frozen=True)
class Pong:
    pass


_Outbound = Connected | Error | SendBundle | ToolResult | Ping | Pong


def encode(frame: _Outbound) -> str:
    if isinstance(frame, Connected):
        return json.dumps({
            "type": "connected",
            "session_id": frame.session_id,
            "agent": frame.agent,
        })
    if isinstance(frame, Error):
        return json.dumps({"type": "error", "reason": frame.reason})
    if isinstance(frame, SendBundle):
        return json.dumps({
            "type": "bundle",
            "bundle_id": frame.bundle_id,
            "root_id": frame.root_id,
            "channel_meta": frame.channel_meta,
            "messages": frame.messages,
        })
    if isinstance(frame, ToolResult):
        body: dict[str, Any] = {
            "type": "tool_result",
            "command_id": frame.command_id,
            "ok": frame.ok,
        }
        if frame.ok:
            body["result"] = frame.result
        else:
            body["error"] = frame.error
        return json.dumps(body)
    if isinstance(frame, Ping):
        return json.dumps({"type": "ping"})
    if isinstance(frame, Pong):
        return json.dumps({"type": "pong"})
    raise ProtocolError(f"cannot encode {type(frame).__name__}")


_Inbound = Connect | Ack | End | ToolCall | Ping | Pong


def decode_inbound(raw: str) -> _Inbound:
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("frame is not a JSON object")
    kind = msg.get("type")
    if kind == "connect":
        return Connect(bundle=_req(msg, "bundle"), password=_req(msg, "password"))
    if kind == "ack":
        return Ack(bundle_id=_req(msg, "bundle_id"))
    if kind == "end":
        return End(bundle_id=_req(msg, "bundle_id"))
    if kind == "tool_call":
        raw_params = msg.get("params")
        if raw_params is None:
            params: dict[str, Any] = {}
        elif isinstance(raw_params, dict):
            params = raw_params
        else:
            raise ProtocolError("tool_call.params must be an object")
        return ToolCall(
            command_id=_req(msg, "command_id"),
            tool=_req(msg, "tool"),
            params=params,
        )
    if kind == "ping":
        return Ping()
    if kind == "pong":
        return Pong()
    raise ProtocolError(f"unknown frame type: {kind!r}")


def _req(msg: dict[str, Any], key: str) -> str:
    val = msg.get(key)
    if not isinstance(val, str) or val == "":
        raise ProtocolError(f"missing/empty field {key!r}")
    return val
