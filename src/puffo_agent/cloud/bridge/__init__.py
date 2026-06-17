"""Posture-B Bridge seam for the cli-cloud runtime.

In the cloud topology the sandbox holds NO identity keys: all crypto
and the puffo-server protocol live in the Bridge (our VPC). The
sandbox's daemon + MCP server talk to the Bridge over a session
token — inbound is already-decrypted message events the Bridge
pushes; outbound is plaintext the Bridge encrypts, signs, and
forwards. This package is the in-sandbox client side of that seam.

The real Bridge wire contract is not yet locked with the Bridge owner,
so everything here is built and tested against ``StubBridgeClient``;
the transport is swapped when the schema lands.
"""

import os

from .client import (
    BridgeClient,
    BridgeConfig,
    BridgeInbound,
    BridgeInboundEvent,
    BridgeOutbound,
    StubBridgeClient,
)


def build_bridge_client(config: BridgeConfig) -> BridgeClient:
    """Resolve the Bridge transport. ``PUFFO_BRIDGE_STUB=1`` swaps in the
    in-memory fake for local dev/smoke; otherwise the HTTP+WS client."""
    if os.environ.get("PUFFO_BRIDGE_STUB") == "1":
        return StubBridgeClient(config)
    from .ws import HttpWsBridgeClient
    return HttpWsBridgeClient(config)


__all__ = [
    "BridgeClient",
    "BridgeConfig",
    "BridgeInbound",
    "BridgeInboundEvent",
    "BridgeOutbound",
    "StubBridgeClient",
    "build_bridge_client",
]
