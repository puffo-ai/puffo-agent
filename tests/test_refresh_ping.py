"""Unit tests for refresh_ping orchestration in base.Adapter.

Covers: expiresAt threshold gate, daemon-wide mutex (concurrent
callers no-op instead of queueing), post-lock recheck, and the
before/after/didn't-advance logging.
"""

import asyncio
import logging

import pytest

from puffo_agent.agent.adapters import base


class _Fixture(base.Adapter):
    """Concrete Adapter for tests. Scripts ``expires_in`` values and
    observes ``_run_refresh_oneshot`` interaction.
    """

    def __init__(
        self,
        expires_queue: list[int | None],
        run_oneshot_delay: float = 0.0,
    ):
        # Each call to _credentials_expires_in_seconds pops from the
        # queue so tests can assert before / recheck / after values
        # separately.
        self._queue = list(expires_queue)
        self._run_oneshot_delay = run_oneshot_delay
        self.oneshot_calls = 0

    async def run_turn(self, ctx):
        raise NotImplementedError

    def _credentials_expires_in_seconds(self):
        if not self._queue:
            return None
        return self._queue.pop(0)

    async def _run_refresh_oneshot(self):
        self.oneshot_calls += 1
        if self._run_oneshot_delay:
            await asyncio.sleep(self._run_oneshot_delay)


def _run(coro):
    return asyncio.run(coro)


# ── Threshold gate ───────────────────────────────────────────────────────────


class TestThresholdGate:
    def test_fresh_skips_refresh(self):
        # 2h left -> no refresh.
        adapter = _Fixture(expires_queue=[2 * 3600])
        _run(adapter.refresh_ping())
        assert adapter.oneshot_calls == 0

    def test_near_expiry_triggers_refresh(self):
        # 5 min left (under the 15-min threshold). Queue values:
        # before-gate, after-lock recheck, after-refresh (success
        # bumps expiry back to 2h).
        adapter = _Fixture(expires_queue=[5 * 60, 5 * 60, 2 * 3600])
        _run(adapter.refresh_ping())
        assert adapter.oneshot_calls == 1

    def test_at_exact_threshold_triggers_refresh(self):
        # Boundary: ``> threshold`` is the skip predicate, so ``==``
        # falls into the refresh path.
        t = base.CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS
        adapter = _Fixture(expires_queue=[t, t, t * 4])
        _run(adapter.refresh_ping())
        assert adapter.oneshot_calls == 1

    def test_none_from_hook_shortcircuits(self):
        # sdk-local / chat-local return None -> no refresh.
        adapter = _Fixture(expires_queue=[None])
        _run(adapter.refresh_ping())
        assert adapter.oneshot_calls == 0


# ── Mutex ────────────────────────────────────────────────────────────────────


class TestMutex:
    def test_concurrent_agents_only_one_refresh(self):
        """Two agents tick past threshold simultaneously. The first
        to acquire the lock refreshes; the second sees the lock held
        and no-ops.
        """
        async def scenario():
            # Loser gets asked for expires_in once (initial gate).
            # Winner also gets recheck + post-refresh values.
            a = _Fixture(
                expires_queue=[60, 60, 2 * 3600],
                run_oneshot_delay=0.05,
            )
            b = _Fixture(expires_queue=[60])
            task_a = asyncio.create_task(a.refresh_ping())
            await asyncio.sleep(0.005)
            task_b = asyncio.create_task(b.refresh_ping())
            await asyncio.gather(task_a, task_b)
            return a, b

        a, b = _run(scenario())
        assert a.oneshot_calls == 1
        assert b.oneshot_calls == 0, (
            "Second agent should have no-oped while the first held the lock"
        )

    def test_sequential_agents_both_refresh_if_both_stale(self):
        # Lock released between calls; second agent's recheck still
        # sees stale (contrived) and refreshes too.
        async def scenario():
            a = _Fixture(expires_queue=[60, 60, 2 * 3600])
            b = _Fixture(expires_queue=[60, 60, 2 * 3600])
            await a.refresh_ping()
            await b.refresh_ping()
            return a, b

        a, b = _run(scenario())
        assert a.oneshot_calls == 1
        assert b.oneshot_calls == 1

    def test_recheck_after_lock_skips_if_another_just_refreshed(self):
        # Adapter ticks past threshold but the file has been refreshed
        # by another agent during the lock wait.
        adapter = _Fixture(expires_queue=[
            60,        # before-gate: near expiry
            2 * 3600,  # after-lock recheck: already refreshed
        ])
        _run(adapter.refresh_ping())
        assert adapter.oneshot_calls == 0


# ── Logging ──────────────────────────────────────────────────────────────────


class TestLogging:
    def test_successful_refresh_logs_before_and_after(self, caplog):
        adapter = _Fixture(expires_queue=[60, 60, 2 * 3600])
        with caplog.at_level(logging.INFO, logger="puffo_agent.agent.adapters.base"):
            _run(adapter.refresh_ping())
        messages = [r.message for r in caplog.records]
        assert any("credentials expire in 60s — running refresh ping" in m for m in messages)
        assert any("credentials refreshed: expires in 7200s (was 60s)" in m for m in messages)

    def test_refresh_that_doesnt_advance_expiry_warns(self, caplog):
        # Refresh ran but expiry didn't move forward -> warn.
        adapter = _Fixture(expires_queue=[60, 60, 60])
        with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.adapters.base"):
            _run(adapter.refresh_ping())
        assert any(
            "refresh_ping ran but token expiry didn't advance" in r.message
            for r in caplog.records
        )

    def test_fresh_token_does_not_log_info(self, caplog):
        # Fresh token: skip logged at DEBUG, INFO stays clean.
        adapter = _Fixture(expires_queue=[2 * 3600])
        with caplog.at_level(logging.INFO, logger="puffo_agent.agent.adapters.base"):
            _run(adapter.refresh_ping())
        assert not any(
            "running refresh ping" in r.message for r in caplog.records
        )
