"""Shared auth-error detection for adapter / provider error output.

Used by the CLI session adapter (retry decision) and ``core`` (tagging
``AgentAPIError.is_auth``) so the two can't drift — a divergence here is
exactly what let a ``401 Invalid authentication credentials`` get
mis-handled as a rate-limit.

Substring match: safe on adapter *error output*, but do NOT apply to
free-form agent prose. The worker uses anchored patterns for that, so a
reply merely *mentioning* an auth concept isn't suppressed.
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
