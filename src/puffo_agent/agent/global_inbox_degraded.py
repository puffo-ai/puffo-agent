"""Failure gates for the Global Inbox runtime: degraded backoff + drained park."""

from __future__ import annotations

import time

from .global_inbox_types import RuntimeHealth

# degrade = transient incident, not a verdict on pending rows; the runtime
# re-arms its own bounded backoff so retries don't depend on unrelated ingress
DEGRADED_RECOVERY_BASE_SECONDS = 5.0
DEGRADED_RECOVERY_MAX_SECONDS = 300.0


class DegradedRecoveryMixin:
    """State owner for ``GlobalInboxRuntime``'s failure gates."""

    def _init_recovery_gates(self, drained_check) -> None:
        self._degraded = False
        self._degraded_until: float | None = None
        self._degraded_attempts = 0
        # drained park (see _park_drained); notify() never clears it
        self.drained_check = drained_check
        self._parked_drained = False

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

    def _park_drained(self) -> None:
        """Hold, don't retry — backoff can't refill a quota. Rows stay
        pending; unpark = ``drained_check`` clear + a wake."""
        self.health = RuntimeHealth(
            "degraded",
            "provider quota exhausted; parked until the usage window resets",
        )
        self._parked_drained = True

    def _drained_park_allows_processing(self) -> bool:
        if self._parked_drained:
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
