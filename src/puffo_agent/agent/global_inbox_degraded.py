"""Failure gates for the Global Inbox runtime: the degraded backoff
window, and the drained park that holds durable work without retrying a
spent plan quota."""

from __future__ import annotations

import time

from .global_inbox_types import RuntimeHealth

# A degrade is a transient provider incident, never a durable verdict about
# pending Inbox work.  Recovery is a bounded backoff window the runtime re-arms
# itself, so requeued rows stay retryable without depending on unrelated ingress.
DEGRADED_RECOVERY_BASE_SECONDS = 5.0
DEGRADED_RECOVERY_MAX_SECONDS = 300.0


class DegradedRecoveryMixin:
    """State owner for ``GlobalInboxRuntime``'s failure gates."""

    def _init_recovery_gates(self, drained_check) -> None:
        self._degraded = False
        self._degraded_until: float | None = None
        self._degraded_attempts = 0
        # Hold-no-retry for a spent plan quota: rows stay pending but no
        # provider turn is scheduled until ``drained_check`` reports the
        # quota cleared (usage-snapshot driven) and a wake arrives.
        # ``notify()`` deliberately does not clear this gate.
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
        # Arm the autonomous recovery wake through the existing coalescer only:
        # no extra task, timer, or thread, so shutdown behaviour is unchanged.
        self.coalescer.notify(delay_seconds=backoff)

    def _park_drained(self) -> None:
        """Hold, don't retry: a spent quota is not recovered by backoff.
        Durable rows stay pending; the gate in ``process_once`` clears
        once ``drained_check`` reports the quota back and a wake arrives."""
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
            # An earlier coalescer deadline may have fired ahead of the degrade
            # wake and consumed it; re-arm so the window still ends in a retry.
            self.coalescer.notify(delay_seconds=remaining)
            return False
        self._degraded = False
        self._degraded_until = None
        return True
