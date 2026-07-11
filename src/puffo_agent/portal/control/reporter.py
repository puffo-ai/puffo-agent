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
        # PUF-364: per-harness token deltas accrued since the last successful
        # report; drained by the control manager's usage loop. Values are
        # [input_sum, output_sum]. Sync-only mutation (single event loop).
        self._usage: dict[str, list[int]] = {}
        self._harness_cache: dict[str, str] = {}

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

    async def send_to_operator(self, operator_slug: str, payload: dict) -> None:
        """Send an arbitrary machine_message payload to a specific linked
        operator. Unlike ``emit`` this raises on failure — callers (e.g. the
        ws-local create flow) need to surface a non-delivery."""
        if self._sender is None:
            raise RuntimeError("control WS not connected")
        pairing = load_pairings().get(operator_slug)
        if pairing is None:
            raise RuntimeError(f"operator {operator_slug!r} is not linked")
        recipients = await self._recipients(operator_slug, pairing)
        if not recipients:
            raise RuntimeError(f"operator {operator_slug!r} has no active devices")
        envelope = agent_message.build_machine_message_envelope(
            self._machine_identity(), recipients, payload
        )
        await self._sender(operator_slug, envelope)

    def record_turn_usage(
        self, agent_slug: str, input_tokens: int, output_tokens: int
    ) -> None:
        """Accrue a turn's token counts against the agent's harness. Drained
        per (machine, harness) by the control manager's usage loop."""
        if input_tokens <= 0 and output_tokens <= 0:
            return
        harness = self._harness(agent_slug)
        if not harness:
            return
        slot = self._usage.setdefault(harness, [0, 0])
        slot[0] += max(0, input_tokens)
        slot[1] += max(0, output_tokens)

    def snapshot_usage(self) -> dict[str, tuple[int, int]]:
        """Non-zero accrued deltas per harness. The loop POSTs these then calls
        ``commit_usage_sent`` to subtract exactly what was sent, so turns
        accruing during the in-flight POST are preserved rather than lost."""
        return {h: (v[0], v[1]) for h, v in self._usage.items() if v[0] or v[1]}

    def commit_usage_sent(self, sent: dict[str, tuple[int, int]]) -> None:
        for harness, (inp, out) in sent.items():
            slot = self._usage.get(harness)
            if slot is not None:
                slot[0] -= inp
                slot[1] -= out

    def _harness(self, agent_slug: str) -> str | None:
        hit = self._harness_cache.get(agent_slug)
        if hit is not None:
            return hit
        try:
            harness = AgentConfig.load(agent_slug).runtime.harness or "claude-code"
        except Exception:  # noqa: BLE001
            return None
        self._harness_cache[agent_slug] = harness
        return harness

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
