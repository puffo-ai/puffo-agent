"""``ws-local`` runtime: external agent tools consume an agent's
decrypted message stream over a localhost WebSocket.

The daemon stays the sole crypto boundary — it owns the server WS,
E2EE decrypt/encrypt, cursor, and status reporting. A connected tool
speaks a small plaintext JSON protocol: receive a ``bundle``, return
an ``ack`` when done, and send ``tool_call`` frames that the daemon
dispatches to its puffo_core_tools and answers with ``tool_result``.
"""

from __future__ import annotations

from .auth import AuthedAgent, AuthError, authenticate_bundle
from .bundles import Bundle, BundleQueue
from .protocol import (
    Ack,
    Connect,
    Connected,
    End,
    Ping,
    Pong,
    ProtocolError,
    SendBundle,
    ToolCall,
    ToolResult,
    decode_inbound,
    encode,
)
from .registry import SessionRegistry
from .session import WsLocalSession
from .tool_dispatch import WS_LOCAL_ALLOWED_TOOLS, build_dispatch

__all__ = [
    "Bundle",
    "BundleQueue",
    "SessionRegistry",
    "WS_LOCAL_ALLOWED_TOOLS",
    "WsLocalSession",
    "AuthError",
    "AuthedAgent",
    "authenticate_bundle",
    "build_dispatch",
    "ProtocolError",
    "Ack",
    "Connect",
    "Connected",
    "End",
    "Ping",
    "Pong",
    "SendBundle",
    "ToolCall",
    "ToolResult",
    "decode_inbound",
    "encode",
]
