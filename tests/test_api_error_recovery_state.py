"""Health recovery remains attached to the durable worker/runtime path."""

from __future__ import annotations

import logging

from puffo_agent.portal.worker import Worker


class Runtime:
    def __init__(self, health: str):
        self.health = health
        self.error = "old error"
        self.saved = 0

    def save(self, _agent_id: str) -> None:
        self.saved += 1


def test_success_clears_recoverable_api_error_state():
    runtime = Runtime("api_error_abandoned")
    Worker._clear_api_error_abandoned_if_recoverable(
        runtime, "agent", "durable-turn", logging.getLogger(__name__)
    )
    assert runtime.health == "ok"
    assert runtime.error == ""
    assert runtime.saved == 1


def test_success_does_not_overwrite_auth_failure():
    runtime = Runtime("auth_failed")
    Worker._clear_api_error_abandoned_if_recoverable(
        runtime, "agent", "durable-turn", logging.getLogger(__name__)
    )
    assert runtime.health == "auth_failed"
    assert runtime.error == "old error"
    assert runtime.saved == 0
