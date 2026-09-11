"""Production dispatch coverage against the pinned Pi 0.84.3 fixture."""

from __future__ import annotations

from puffo_agent.agent.harness.drivers.pi_protocol import PI_EVENT_DISPATCH_KEYS
from tests.fixtures.pi_rpc_0_84_3 import EVENTS


def test_exhaustive_event_parsing_is_required():
    """Production dispatch keys must exactly match the pinned event surface."""
    assert PI_EVENT_DISPATCH_KEYS == EVENTS
