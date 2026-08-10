"""Shared agent-runtime exceptions with no adapter or harness dependencies."""

from __future__ import annotations


class AgentAPIError(Exception):
    """Provider failure that the Global Inbox can recover from.

    ``is_auth`` distinguishes credentials requiring operator action from
    retryable provider failures, which are re-enqueued with backoff.
    """

    def __init__(self, message: str, *, is_auth: bool = False) -> None:
        super().__init__(message)
        self.is_auth = is_auth
