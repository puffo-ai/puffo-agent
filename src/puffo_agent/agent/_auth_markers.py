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
    "authentication failed",
    "failed to authenticate",
    "credentials expired",
    "api error: 401",
    "invalid authentication credentials",
    '"type":"authentication_error"',
)


def looks_like_auth_error(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in AUTH_ERROR_MARKERS)
