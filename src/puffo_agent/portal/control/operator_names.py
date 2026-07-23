"""Operator display-name cache for the Operators tab; Qt-free + unit-testable."""
from __future__ import annotations

import time
from typing import Callable

# Cap re-resolves so a rename (or a late-shipping endpoint) is picked up.
REFRESH_AFTER_SECONDS = 300.0


class OperatorNameCache:
    def __init__(
        self,
        refresh_after: float = REFRESH_AFTER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._names: dict[str, tuple[str, float]] = {}
        self._pending: set[str] = set()
        self._refresh_after = refresh_after
        self._clock = clock

    def label(self, slug: str) -> str:
        """Display name if resolved to a non-empty value, else the slug."""
        entry = self._names.get(slug)
        return (entry[0] if entry else "") or slug

    def slugs_to_fetch(self, slugs: list[str]) -> list[str]:
        """Slugs that are unresolved, stale, or not already in flight."""
        now = self._clock()
        out: list[str] = []
        for s in slugs:
            if s in self._pending:
                continue
            entry = self._names.get(s)
            if entry is None or (now - entry[1]) > self._refresh_after:
                out.append(s)
        return out

    def mark_pending(self, slug: str) -> None:
        self._pending.add(slug)

    def resolved(self, slug: str, name: str) -> None:
        """Record a fetch result; empty is cached too so failures don't re-fire."""
        self._pending.discard(slug)
        self._names[slug] = (name, self._clock())
