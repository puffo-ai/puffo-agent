"""Bridge client seam + in-repo stub.

``BridgeConfig`` carries the identity injected on first wake;
``BridgeOutbound`` / ``BridgeInbound`` are the two halves of the seam
(send plaintext / receive decrypted events); ``StubBridgeClient`` is
the fake the cli-cloud path is developed and tested against until the
real Bridge transport lands.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Optional, Protocol, runtime_checkable


# ── Late-binding env vars ─────────────────────────────────────────────
# Injected by the Agent Instance Manager at first wake, never baked into
# the E2B template snapshot. PUFFO_AGENT_ID / PUFFO_CORE_SLUG /
# PUFFO_CORE_SPACE_ID are shared with the existing MCP env names.
ENV_BRIDGE_URL = "PUFFO_BRIDGE_URL"
ENV_BRIDGE_TOKEN = "PUFFO_BRIDGE_TOKEN"
ENV_AGENT_ID = "PUFFO_AGENT_ID"
ENV_SLUG = "PUFFO_CORE_SLUG"
ENV_SPACE_ID = "PUFFO_CORE_SPACE_ID"
ENV_OPERATOR_SLUG = "PUFFO_OPERATOR_SLUG"
ENV_PACKAGE_REF = "PUFFO_PACKAGE_REF"
ENV_LLM_GATEWAY_URL = "PUFFO_LLM_GATEWAY_URL"
ENV_LLM_VIRTUAL_KEY = "PUFFO_LLM_VIRTUAL_KEY"


@dataclass
class BridgeConfig:
    """Identity + endpoints handed to a cli-cloud agent on wake.

    ``session_token`` authenticates the sandbox to the Bridge in place
    of the keystore signing the sandbox no longer holds.
    """
    bridge_url: str = ""
    session_token: str = ""
    agent_id: str = ""
    slug: str = ""
    space_id: str = ""
    operator_slug: str = ""
    package_ref: str = ""
    llm_gateway_url: str = ""
    llm_virtual_key: str = ""

    def is_configured(self) -> bool:
        return bool(self.bridge_url and self.session_token)

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "BridgeConfig":
        env = os.environ if environ is None else environ
        return cls(
            bridge_url=env.get(ENV_BRIDGE_URL, ""),
            session_token=env.get(ENV_BRIDGE_TOKEN, ""),
            agent_id=env.get(ENV_AGENT_ID, ""),
            slug=env.get(ENV_SLUG, ""),
            space_id=env.get(ENV_SPACE_ID, ""),
            operator_slug=env.get(ENV_OPERATOR_SLUG, ""),
            package_ref=env.get(ENV_PACKAGE_REF, ""),
            llm_gateway_url=env.get(ENV_LLM_GATEWAY_URL, ""),
            llm_virtual_key=env.get(ENV_LLM_VIRTUAL_KEY, ""),
        )


@dataclass
class BridgeInboundEvent:
    """A thread-batch the Bridge pushes after decrypting it server-side.

    ``messages`` are plaintext, StoredMessage-shaped dicts; the sandbox
    never sees ciphertext.
    """
    root_id: str
    messages: list[dict]
    channel_meta: dict = field(default_factory=dict)


OnEvent = Callable[[BridgeInboundEvent], Awaitable[None]]


@runtime_checkable
class BridgeOutbound(Protocol):
    """Outbound half: the Bridge encrypts/signs/forwards on our behalf."""

    async def send_message(
        self,
        *,
        channel: str,
        text: str,
        is_visible_to_human: bool,
        root_id: str = "",
    ) -> dict: ...

    async def report_status(self, status: dict) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class BridgeInbound(Protocol):
    """Inbound half: a long-lived loop pushing decrypted events.

    ``run`` is expected to raise a connection error on a dropped socket
    (an E2B pause cuts it) so the caller's reconnect-on-wake loop can
    re-establish it; it returns cleanly once ``stop`` is requested.
    """

    async def run(self, on_event: OnEvent) -> None: ...

    async def stop(self) -> None: ...


@runtime_checkable
class BridgeClient(BridgeOutbound, BridgeInbound, Protocol):
    """Both halves of the seam over one session-token'd connection."""


class StubBridgeClient:
    """In-memory fake Bridge for developing/testing cli-cloud.

    Tests push inbound events via ``push_event`` and inspect ``sent`` /
    ``status_reports``; ``fail_next_run`` simulates a dropped socket so
    the reconnect-on-wake loop can be exercised.
    """

    def __init__(self, config: Optional[BridgeConfig] = None) -> None:
        self.config = config or BridgeConfig()
        self.sent: list[dict] = []
        self.status_reports: list[dict] = []
        self.connects = 0
        self.fail_next_run = False
        self._inbox: "asyncio.Queue[BridgeInboundEvent]" = asyncio.Queue()
        self._stop = asyncio.Event()

    async def send_message(
        self,
        *,
        channel: str,
        text: str,
        is_visible_to_human: bool,
        root_id: str = "",
    ) -> dict:
        rec = {
            "channel": channel,
            "text": text,
            "is_visible_to_human": is_visible_to_human,
            "root_id": root_id,
            "envelope_id": f"stub-{len(self.sent)}",
        }
        self.sent.append(rec)
        return rec

    async def report_status(self, status: dict) -> None:
        self.status_reports.append(dict(status))

    async def close(self) -> None:
        self._stop.set()

    def push_event(self, event: BridgeInboundEvent) -> None:
        self._inbox.put_nowait(event)

    async def run(self, on_event: OnEvent) -> None:
        self.connects += 1
        if self.fail_next_run:
            self.fail_next_run = False
            raise ConnectionError("stub bridge: simulated socket drop")
        while not self._stop.is_set():
            try:
                event = await asyncio.wait_for(self._inbox.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            await on_event(event)

    async def stop(self) -> None:
        self._stop.set()
