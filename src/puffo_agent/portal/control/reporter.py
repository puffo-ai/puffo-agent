"""Daemon-wide singleton: streams agent.status events up the control WS to an
agent's owner. Best-effort + ephemeral — drops the event if the WS is down, the
owner isn't linked, or there are no active devices.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from ..state import AgentConfig
from . import agent_message
from .store import load_or_create_machine, load_pairings

log = logging.getLogger("puffo_agent.control")

# Re-fetch an operator's recipient device keys at most this often.
_RECIPIENT_TTL_SECONDS = 60.0

# send(operator_slug, envelope) → puts a {type:"message", ...} frame on the live WS.
Sender = Callable[[str, dict], Awaitable[None]]


class AgentStatusReporter:
    def __init__(self) -> None:
        self._sender: Sender | None = None
        self._machine = None
        self._cache: dict[str, tuple[float, list[agent_message.Recipient]]] = {}

    def set_sender(self, sender: Sender | None) -> None:
        """Called by the control client: a sender while the WS is up, None on drop."""
        self._sender = sender

    async def emit(self, agent_slug: str, event: str, payload: dict) -> None:
        if self._sender is None:
            return  # WS down — ephemeral, drop.
        operator_slug = self._owner(agent_slug)
        if not operator_slug:
            return
        pairing = load_pairings().get(operator_slug)
        if pairing is None:
            return  # owner not currently linked.
        try:
            recipients = await self._recipients(operator_slug, pairing)
            if not recipients:
                return
            envelope = agent_message.build_machine_message_envelope(
                self._machine_identity(),
                recipients,
                {"type": "agent.status", "agent_slug": agent_slug, "event": event, "payload": payload},
            )
            sender = self._sender
            if sender is not None:
                await sender(operator_slug, envelope)
                log.info(
                    "reporter: agent.status '%s' for %s → %s (%d device(s))",
                    event,
                    agent_slug,
                    operator_slug,
                    len(recipients),
                )
        except Exception as exc:  # noqa: BLE001 — best-effort; never break a turn.
            log.debug("reporter: emit failed: %s", exc)

    def _machine_identity(self):
        if self._machine is None:
            self._machine = load_or_create_machine()
        return self._machine

    def _owner(self, agent_slug: str) -> str | None:
        try:
            return AgentConfig.load(agent_slug).puffo_core.operator_slug or None
        except Exception:  # noqa: BLE001
            return None

    async def _recipients(self, operator_slug, pairing) -> list[agent_message.Recipient]:
        now = time.monotonic()
        hit = self._cache.get(operator_slug)
        if hit and hit[0] > now:
            return hit[1]
        recips = await agent_message.fetch_active_recipients(
            pairing.server_url, self._machine_identity(), operator_slug, pairing.operator_root_pubkey
        )
        self._cache[operator_slug] = (now + _RECIPIENT_TTL_SECONDS, recips)
        return recips


_REPORTER = AgentStatusReporter()


def get_reporter() -> AgentStatusReporter:
    return _REPORTER
