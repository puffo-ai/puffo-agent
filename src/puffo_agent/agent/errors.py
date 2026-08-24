"""Shared agent-runtime exceptions with no adapter or harness dependencies."""

from __future__ import annotations


class AgentAPIError(Exception):
    """Provider failure that the Global Inbox can recover from.

    ``is_auth`` distinguishes credentials requiring operator action from
    retryable provider failures, which are re-enqueued with backoff.
    ``is_drained`` is spent plan quota: operator-visible like ``is_auth``
    but hold-no-retry, and recovered by the usage window resetting rather
    than by signing in again. The two are mutually exclusive.
    ``error_code`` is an optional short tag for allowlisted logging.
    """

    def __init__(
        self, message: str, *, is_auth: bool = False,
        is_drained: bool = False, error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.is_auth = is_auth
        self.is_drained = is_drained
        self.error_code = error_code


class ProviderFailureError(RuntimeError):
    """A categorized provider failure that must not be retried immediately."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code
