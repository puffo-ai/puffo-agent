"""Bounded, credential-safe diagnostics from provider error text."""

from __future__ import annotations

import re
from typing import Any

_AUTHORIZATION_FIELD = re.compile(
    r"(?i)(?P<prefix>[\"']?authorization[\"']?\s*[:=]\s*)"
    # An unquoted Authorization value can contain both a scheme and payload.
    # Consume conservatively through the next structured-value delimiter;
    # losing adjacent diagnostic prose is preferable to leaking credentials.
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^,}\]]+)"
)
_SENSITIVE_ERROR_FIELD = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token)[\"']?\s*[:=]\s*)"
    r"(?P<value>(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,}\]]+))"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+(?:\"[^\"]*\"|'[^']*'|[^\s,}\]]+)")
_TOKENISH = re.compile(
    r"(?i)\b(?:sk[_-][a-z0-9_-]{12,}|eyJ[a-zA-Z0-9_-]{12,}"
    r"\.[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)?)"
)


def safe_provider_message(message: Any) -> str:
    """Keep a bounded diagnostic without copying credential-shaped text."""
    if not isinstance(message, str):
        return "(missing or invalid provider message)"
    compact = " ".join(message.split())
    redacted = _AUTHORIZATION_FIELD.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]", compact
    )
    redacted = _SENSITIVE_ERROR_FIELD.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]", redacted
    )
    redacted = _BEARER_VALUE.sub("Bearer [REDACTED]", redacted)
    redacted = _TOKENISH.sub("[REDACTED]", redacted)
    return redacted[:300] or "(empty provider message)"
