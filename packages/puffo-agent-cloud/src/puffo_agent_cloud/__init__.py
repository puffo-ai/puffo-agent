"""api-puffo runtime: cloud-hosted agents.

Thin client. The runtime holds NO key material; puffo-server's
``cloud_agent`` module owns the keystore and drives all seal/open.
The runtime opens one WS (``/v2/cloud-agents/subscribe``) with a
``x-sandbox-token`` bearer, sends/receives plaintext frames, and
runs an in-process LLM loop against a cloud HTTP endpoint —
no CLI subprocess, no MCP subprocess. Wire spec:
``puffo-server/roadmap/cloud-agent/BRIDGE-WIRE-PROTOCOL.md``."""

from __future__ import annotations

from .cloud_http import CloudMetadataClient, CloudMetadataError
from .config import CloudAgentConfig
from .runner import ApiPuffoRunner

__all__ = [
    "ApiPuffoRunner",
    "CloudAgentConfig",
    "CloudMetadataClient",
    "CloudMetadataError",
]
