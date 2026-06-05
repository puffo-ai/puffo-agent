"""``ws-local`` runtime: external agent tools consume an agent's
decrypted message stream over a localhost WebSocket.

The daemon stays the sole crypto boundary — it owns the server WS,
E2EE decrypt/encrypt, cursor, and status reporting. A connected tool
speaks a small plaintext JSON protocol: receive a ``bundle``, return
an ``ack`` when done, and send ``reply`` frames the daemon encrypts
and posts. See ``docs`` / the design note for the full rationale.
"""

from __future__ import annotations

from .auth import AuthedAgent, AuthError, authenticate_bundle
from .bundles import Bundle, BundleQueue
from .protocol import (
    Ack,
    Connect,
    Connected,
    Ping,
    Pong,
    ProtocolError,
    ReplyOut,
    SendBundle,
    decode_inbound,
    encode,
)
from .registry import SessionRegistry
from .session import WsLocalSession

__all__ = [
    "Bundle",
    "BundleQueue",
    "SessionRegistry",
    "WsLocalSession",
    "AuthError",
    "AuthedAgent",
    "authenticate_bundle",
    "ProtocolError",
    "Ack",
    "Connect",
    "Connected",
    "Ping",
    "Pong",
    "ReplyOut",
    "SendBundle",
    "decode_inbound",
    "encode",
]
