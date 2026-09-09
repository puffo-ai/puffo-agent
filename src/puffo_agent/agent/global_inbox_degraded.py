"""Failure gates for the Global Inbox runtime: degraded backoff + drained park."""

from __future__ import annotations

import time

from .global_inbox_types import RuntimeHealth

# degrade = transient incident, not a verdict on pending rows; the runtime
# re-arms its own bounded backoff so retries don't depend on unrelated ingress
DEGRADED_RECOVERY_BASE_SECONDS = 5.0
DEGRADED_RECOVERY_MAX_SECONDS = 300.0
# budget-cap park: no reset window exists, so hold, then probe once. Doubles
# per consecutive cap hit; a completed turn resets it. Bounded so a wallet
# topped up at minute 31 is not ignored until the next day.
BUDGET_PARK_BASE_SECONDS = 300.0
BUDGET_PARK_MAX_SECONDS = 1800.0


class DegradedRecoveryMixin:
    """State owner for ``GlobalInboxRuntime``'s failure gates."""

    def _init_recovery_gates(self, drained_check) -> None:
        self._degraded = False
        self._degraded_until: float | None = None
        self._degraded_attempts = 0
        # drained park (see _park_drained); notify() never clears it
        self.drained_check = drained_check
        self._parked_drained = False
        # timed hold for a budget-cap park; None = wait for drained_check
        self._drained_park_until: float | None = None
        self._budget_park_attempts = 0

    def _clear_degraded_backoff(self) -> None:
        self._degraded = False
        self._degraded_until = None
        self._degraded_attempts = 0

    def _degrade(self, diagnostic: str) -> None:
        self.health = RuntimeHealth("degraded", diagnostic)
        self._degraded = True
        self._degraded_attempts += 1
        backoff = min(
            DEGRADED_RECOVERY_BASE_SECONDS * 2 ** (self._degraded_attempts - 1),
            DEGRADED_RECOVERY_MAX_SECONDS,
        )
        self._degraded_until = time.monotonic() + backoff
        # wake rides the existing coalescer — no extra task/timer/thread
        self.coalescer.notify(delay_seconds=backoff)

    def _park_drained(
        self,
        outcome: str = "drained",
        *,
        hold_seconds: float | None = None,
        diagnostic: str | None = None,
    ) -> None:
        """Hold, don't retry — backoff can't refill a quota. Rows stay
        pending; unpark = ``drained_check`` clear + a wake.

        With ``hold_seconds`` the park is timed instead: nothing runs until
        the hold expires, then ONE probe turn is allowed regardless of
        ``drained_check``. That is the only exit a gateway-routed agent has
        — the usage snapshot that clears a plan drain comes from the host's
        own ``claude /usage``, which a sandbox cannot produce.
        """
        self.health = RuntimeHealth(
            "degraded",
            diagnostic
            or (
                "extra usage unavailable; parked until operator retry"
                if outcome == "extra_usage_required"
                else "provider quota exhausted; parked until the usage window resets"
            ),
        )
        self._parked_drained = True
        if hold_seconds is not None:
            self._drained_park_until = time.monotonic() + hold_seconds
            # the wake rides the coalescer, like the degraded backoff
            self.coalescer.notify(delay_seconds=hold_seconds)
        else:
            self._drained_park_until = None

    def next_budget_park_hold(self) -> float:
        """Escalating hold for consecutive budget-cap parks (5 → 30 min)."""
        self._budget_park_attempts += 1
        return min(
            BUDGET_PARK_BASE_SECONDS * 2 ** (self._budget_park_attempts - 1),
            BUDGET_PARK_MAX_SECONDS,
        )

    def _clear_budget_park_backoff(self) -> None:
        self._budget_park_attempts = 0

    def _drained_park_allows_processing(self) -> bool:
        if self._parked_drained:
            if self._drained_park_until is not None:
                remaining = self._drained_park_until - time.monotonic()
                if remaining > 0:
                    # a wake arrived inside the hold; re-arm and keep holding
                    self.coalescer.notify(delay_seconds=remaining)
                    return False
                # hold expired: one probe, whatever the snapshot says
                self._drained_park_until = None
                self._parked_drained = False
                return True
            if self.drained_check is not None and self.drained_check():
                return False
            self._parked_drained = False
        return True

    def _try_degraded_recovery(self) -> bool:
        """Return whether a degraded runtime may retry its durable work now."""
        if not self._degraded:
            return True
        remaining = (
            0.0
            if self._degraded_until is None
            else self._degraded_until - time.monotonic()
        )
        if remaining > 0:
            # an earlier coalescer deadline may have consumed the wake; re-arm
            self.coalescer.notify(delay_seconds=remaining)
            return False
        self._degraded = False
        self._degraded_until = None
        return True
