"""PUF-213: cover the adaptive credential-refresh tick policy.

The full credential_refresh() loop is integration-heavy (real
Adapter, real stop event, asyncio scheduling). The load-bearing
logic — when to wake up next — is in the pure ``_next_refresh_tick``
helper, which this matrix covers exhaustively.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.adapters.base import (
    CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS,
)
from puffo_agent.portal.worker import (
    CREDENTIAL_REFRESH_TICK_FLOOR_SECONDS,
    CREDENTIAL_REFRESH_TICK_SECONDS,
    _next_refresh_tick,
)


def test_no_ttl_returns_default_tick():
    """sdk / chat-only adapters report ``None``; the loop is then a
    pure health heartbeat. Use the default tick."""
    assert _next_refresh_tick(None) == CREDENTIAL_REFRESH_TICK_SECONDS


def test_far_future_returns_default_tick():
    # 4h away from a 5-min threshold + 10-min tick → cap at default.
    assert _next_refresh_tick(4 * 3600) == CREDENTIAL_REFRESH_TICK_SECONDS


def test_just_above_window_returns_pre_window_margin():
    """7 min away with a 5-min threshold → wake up in 2 min so the
    next tick lands inside the refresh window with margin."""
    expires_in = CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS + 2 * 60
    assert _next_refresh_tick(expires_in) == 2 * 60


def test_at_window_floor_clamps_to_floor():
    """Inside the refresh window the target goes to 0; clamp up to
    the floor so we don't busy-loop, but still tick fast enough
    to retry if refresh is failing."""
    expires_in = CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS
    assert _next_refresh_tick(expires_in) == CREDENTIAL_REFRESH_TICK_FLOOR_SECONDS


def test_below_window_clamps_to_floor():
    expires_in = CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS - 60
    assert _next_refresh_tick(expires_in) == CREDENTIAL_REFRESH_TICK_FLOOR_SECONDS


def test_already_expired_clamps_to_floor():
    """Negative TTL — token already expired. The next tick will
    retry the refresh; we just bound that retry to once a minute
    so a sustained failure doesn't dogpile."""
    assert _next_refresh_tick(-30) == CREDENTIAL_REFRESH_TICK_FLOOR_SECONDS


def test_default_tick_caps_long_horizon():
    """Even when TTL minus threshold exceeds default_tick, return
    default_tick — the loop's role above default cadence is the
    heartbeat-only check."""
    expires_in = CREDENTIAL_REFRESH_TICK_SECONDS + CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS + 60
    assert _next_refresh_tick(expires_in) == CREDENTIAL_REFRESH_TICK_SECONDS


def test_custom_thresholds_propagate():
    """The defaults bind to the production constants, but tests pin
    their own so failures don't shift if the production threshold
    is later tuned."""
    # 7-second token with 2-second threshold, 10-second tick, 1-second
    # floor → target = 5, returns 5.
    assert (
        _next_refresh_tick(7, default_tick=10, threshold=2, floor=1) == 5
    )
    # 1-second token (inside window) → clamped to 1-second floor.
    assert (
        _next_refresh_tick(1, default_tick=10, threshold=2, floor=1) == 1
    )
    # Negative → clamped to floor.
    assert (
        _next_refresh_tick(-5, default_tick=10, threshold=2, floor=1) == 1
    )
    # 100-second token → target 98, default cap 10 → returns 10.
    assert (
        _next_refresh_tick(100, default_tick=10, threshold=2, floor=1) == 10
    )
