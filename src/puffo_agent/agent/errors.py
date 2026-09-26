"""Shared agent-runtime exceptions with no adapter or harness dependencies."""

from __future__ import annotations


class AgentAPIError(Exception):
    """Provider failure that the Global Inbox can recover from.

    ``is_auth`` distinguishes credentials requiring operator action from
    retryable provider failures, which are re-enqueued with backoff.
    ``is_drained``: spent plan quota — hold-no-retry, recovered by the
    window reset, not re-login. Mutually exclusive with ``is_auth``.
    ``error_code`` is an optional short tag for allowlisted logging.
    ``detail`` is a bounded tail of the provider's raw failure payload,
    for log-only post-mortems — never operator-facing text.
    """

    def __init__(
        self, message: str, *, is_auth: bool = False,
        is_drained: bool = False, error_code: str | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.is_auth = is_auth
        self.is_drained = is_drained
        self.error_code = error_code
        self.detail = detail


class ProviderFailureError(RuntimeError):
    """A categorized provider failure that must not be retried immediately."""

    def __init__(self, message: str, *, error_code: str, detail: str = "") -> None:
        super().__init__(message)
        self.error_code = error_code
        self.detail = detail
