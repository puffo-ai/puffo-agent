"""One active local WS per agent identity.

A later connection for a slug that already holds a *live* session is
rejected. When that session dies (ack/pong timeout → ``release``), the
slot frees and the next connection takes over. ``acquire``/``release``
never await, so the check-and-set is atomic on the event loop.
"""

from __future__ import annotations

from typing import Generic, TypeVar

S = TypeVar("S")


class SessionRegistry(Generic[S]):
    def __init__(self) -> None:
        self._active: dict[str, S] = {}

    def acquire(self, slug: str, session: S) -> bool:
        """Claim the slot for ``slug``. False when a live session holds it."""
        if slug in self._active:
            return False
        self._active[slug] = session
        return True

    def release(self, slug: str, session: S) -> None:
        """Free the slot only if ``session`` still owns it — a session
        that already lost the slot to a takeover must not evict the
        winner."""
        if self._active.get(slug) is session:
            del self._active[slug]

    def current(self, slug: str) -> S | None:
        return self._active.get(slug)

    def active_count(self) -> int:
        return len(self._active)
