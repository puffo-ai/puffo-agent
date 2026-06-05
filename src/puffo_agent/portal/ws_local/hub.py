"""Shared attach points for ws-local agents.

A ws-local Worker doesn't run a consumer — it registers an
``AttachPoint`` here and idles. When a tool connects on the bridge WS,
the route looks the agent up by slug, and (if the slot is free) runs the
session + consumer against that agent's client. One hub per daemon.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .registry import SessionRegistry
from .session import WsLocalSession


@dataclass
class AttachPoint:
    """Everything the bridge route needs to bring one agent online when a
    tool attaches. ``client`` and ``reporter`` are built by the Worker."""

    slug: str
    agent_id: str
    agent_cfg: Any
    client: Any
    reporter: Any
    ack_timeout_s: float
    ping_interval_s: float


class WsLocalHub:
    def __init__(self) -> None:
        self.registry: SessionRegistry[WsLocalSession] = SessionRegistry()
        self._points: dict[str, AttachPoint] = {}

    def register(self, point: AttachPoint) -> None:
        self._points[point.slug] = point

    def unregister(self, point: AttachPoint) -> None:
        if self._points.get(point.slug) is point:
            del self._points[point.slug]

    def get(self, slug: str) -> AttachPoint | None:
        return self._points.get(slug)

    def is_servable(self, slug: str) -> bool:
        return slug in self._points
