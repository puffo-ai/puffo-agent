"""Shared auth-error detection for adapter / provider error output.

Used by the CLI session adapter and ``core`` so their auth-vs-rate-limit
split can't drift. Substring match — safe on adapter error output, but
NOT on free-form agent prose (the worker uses anchored patterns there).
"""

from __future__ import annotations

AUTH_ERROR_MARKERS: tuple[str, ...] = (
    "please run /login",
    "please run `claude /login`",
    "run `claude login`",
    "invalid api key",
    "invalid_grant",
    "oauth token revoked",
    "oauth token has expired",
    "token_invalidated",
    "this organization has been disabled",
    "authentication failed",
    "failed to authenticate",
    "credentials expired",
    "api error: 401",
    "invalid authentication credentials",
    '"type":"authentication_error"',
)

_PROVIDER_DIAGNOSTIC_AUTH_MARKERS: tuple[str, ...] = (
    "unauthorized",
    "unauthorised",
    "please run codex login",
    "run `codex login`",
    "run codex login",
    "authentication required",
    "login required",
    "invalid token",
    "invalid credential",
    "token revoked",
)


def looks_like_auth_error(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in AUTH_ERROR_MARKERS)


def looks_like_provider_auth_error(text: str) -> bool:
    """Match explicit provider diagnostics without widening prose checks."""
    if looks_like_auth_error(text):
        return True
    low = text.lower()
    return any(marker in low for marker in _PROVIDER_DIAGNOSTIC_AUTH_MARKERS)
