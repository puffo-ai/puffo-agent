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
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ack:
    """Optional "I've started" signal — daemon flips status to
    working_on. Idempotent."""

    bundle_id: str


@dataclass(frozen=True)
class Admitted:
    """v2 model-visible transition, distinct from status-only ``ack``."""

    bundle_id: str
    turn_id: str
    correlation_key: str = ""


@dataclass(frozen=True)
class End:
    """Closes the turn + advances the cursor. Idempotent. May be sent
    without a prior ``Ack`` (agent decides not to reply)."""

    bundle_id: str


@dataclass(frozen=True)
class ToolCall:
    """RPC-style call to one of the ``WS_LOCAL_ALLOWED_TOOLS``."""

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
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class Error:
    """Terminal rejection sent just before the daemon closes the socket
    (bad password, agent not servable, slot already held)."""

    reason: str


@dataclass(frozen=True)
class SendBundle:
    bundle_id: str
    root_id: str = ""
    channel_meta: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    version: int = 2
    turn_id: str = ""
    targets: list[list[str]] = field(default_factory=list)
    routes: list[dict[str, Any]] = field(default_factory=list)
    notice: dict[str, Any] = field(default_factory=dict)


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
        body: dict[str, Any] = {
            "type": "connected",
            "session_id": frame.session_id,
            "agent": frame.agent,
        }
        if frame.capabilities:
            body["capabilities"] = list(frame.capabilities)
        return json.dumps(body)
    if isinstance(frame, Error):
        return json.dumps({"type": "error", "reason": frame.reason})
    if isinstance(frame, SendBundle):
        body: dict[str, Any] = {
            "type": "bundle",
            "version": frame.version,
            "bundle_id": frame.bundle_id,
            "turn_id": frame.turn_id,
            "targets": frame.targets,
            "routes": frame.routes,
            "messages": frame.messages,
            "notice": frame.notice,
        }
        # Compatibility route fields are safe only for one destination.
        if len(frame.targets) <= 1:
            body["root_id"] = frame.root_id
            body["channel_meta"] = frame.channel_meta
        return json.dumps(body)
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


_Inbound = Connect | Ack | Admitted | End | ToolCall | Ping | Pong


def decode_inbound(raw: str) -> _Inbound:
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("frame is not a JSON object")
    kind = msg.get("type")
    if kind == "connect":
        raw_capabilities = msg.get("capabilities") or []
        if not isinstance(raw_capabilities, list) or not all(
            isinstance(value, str) for value in raw_capabilities
        ):
            raise ProtocolError("connect.capabilities must be a string list")
        return Connect(
            bundle=_req(msg, "bundle"),
            password=_req(msg, "password"),
            capabilities=tuple(raw_capabilities),
        )
    if kind == "ack":
        return Ack(bundle_id=_req(msg, "bundle_id"))
    if kind == "admitted":
        version = msg.get("version")
        if version != 2:
            raise ProtocolError("admitted.version must be 2")
        return Admitted(
            bundle_id=_req(msg, "bundle_id"),
            turn_id=_req(msg, "turn_id"),
            correlation_key=str(msg.get("correlation_key") or ""),
        )
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
