"""Shared, operator-safe provider failure semantics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from ._auth_markers import looks_like_provider_auth_error
from ._usage_markers import looks_like_usage_limit
from .errors import AgentAPIError, ProviderFailureError


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    message: str
    retryable: bool = False
    is_auth: bool = False
    runtime_event_code: str = "unknown"


PROVIDER_FAILURES: Mapping[str, ProviderFailure] = MappingProxyType(
    {
        "authentication": ProviderFailure(
            "Provider authentication failed; sign in again and retry.",
            is_auth=True,
            runtime_event_code="permission_denied",
        ),
        "permission_denied": ProviderFailure(
            "The requested operation was not permitted.",
            runtime_event_code="permission_denied",
        ),
        "plan_drained": ProviderFailure(
            "The provider plan's usage quota is spent; wait for the "
            "usage window to reset.",
            runtime_event_code="provider_unavailable",
        ),
        "quota_exhausted": ProviderFailure(
            "The selected provider model has reached its usage limit; "
            "switch models or wait for the limit to reset.",
            runtime_event_code="provider_unavailable",
        ),
        "rate_limit": ProviderFailure(
            "The provider rate-limited this turn; retry later.",
            retryable=True,
            runtime_event_code="provider_unavailable",
        ),
        "provider_unavailable": ProviderFailure(
            "The provider is temporarily unavailable; retry later.",
            retryable=True,
            runtime_event_code="provider_unavailable",
        ),
        "provider_error": ProviderFailure(
            "The provider could not complete the turn.",
        ),
        "runtime_exited": ProviderFailure(
            "The Agent runtime stopped before completing the turn.",
            retryable=True,
            runtime_event_code="runtime_exited",
        ),
        "resume_unconfirmed": ProviderFailure(
            "The saved provider session could not be resumed.",
            retryable=True,
            runtime_event_code="runtime_exited",
        ),
        "protocol_error": ProviderFailure(
            "The Agent runtime returned an invalid response.",
            runtime_event_code="protocol_error",
        ),
        "cancel_failed": ProviderFailure(
            "The Agent runtime could not cancel the turn.",
            runtime_event_code="cancel_failed",
        ),
        "unknown": ProviderFailure(
            "The Agent runtime could not complete the turn.",
        ),
    }
)

RUNTIME_EVENT_FAILURE_MESSAGES: Mapping[str, str] = MappingProxyType({
    "provider_unavailable": "The Agent runtime became unavailable.",
    "runtime_exited": PROVIDER_FAILURES["runtime_exited"].message,
    "protocol_error": PROVIDER_FAILURES["protocol_error"].message,
    "permission_denied": PROVIDER_FAILURES["permission_denied"].message,
    "cancel_failed": PROVIDER_FAILURES["cancel_failed"].message,
    "unknown": PROVIDER_FAILURES["unknown"].message,
})

_DEFAULT_PROVIDER_FAILURE = PROVIDER_FAILURES["provider_error"]
_DIAGNOSTIC_HTTP_STATUS = re.compile(r"(?<!\d)(401|403|408|425|429|5\d\d)(?!\d)")


def provider_failure(error_code: str) -> ProviderFailure:
    return PROVIDER_FAILURES.get(error_code, _DEFAULT_PROVIDER_FAILURE)


def is_provider_failure_code(error_code: str) -> bool:
    return error_code in PROVIDER_FAILURES


def provider_failure_retryable(
    error_code: str, *, explicitly_retryable: bool = False
) -> bool:
    failure = PROVIDER_FAILURES.get(error_code)
    if failure is not None:
        return failure.retryable
    return explicitly_retryable


def provider_failure_message(error_code: str, *, outcome: str | None = None) -> str:
    if error_code in PROVIDER_FAILURES:
        return provider_failure(error_code).message
    if outcome is not None:
        return f"provider turn ended with outcome {outcome} (error_code={error_code})"
    return _DEFAULT_PROVIDER_FAILURE.message


def operator_failure_text(exc: Exception) -> str:
    if isinstance(exc, (AgentAPIError, ProviderFailureError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def runtime_event_failure_code(error_code: str) -> str:
    """Map private provider detail onto Runtime Events v1's closed vocabulary."""
    return provider_failure(error_code).runtime_event_code


def classify_provider_failure(*, status: int | None, diagnostic: str) -> str:
    """Classify an explicit provider diagnostic into the shared vocabulary."""
    normalized = diagnostic.lower()
    if status is None:
        match = _DIAGNOSTIC_HTTP_STATUS.search(normalized)
        status = int(match.group(1)) if match is not None else None
    # order: hard 401 -> plan quota -> broad quota -> auth substrings
    if status == 401:
        return "authentication"
    if looks_like_usage_limit(normalized):
        return "plan_drained"
    if (
        ("reached your" in normalized and "limit" in normalized)
        or ("hit your" in normalized and "limit" in normalized)
        or "credit balance is too low" in normalized
        or "billing_error" in normalized
        or "insufficient_quota" in normalized
        or re.search(r"\bquota\b", normalized) is not None
    ):
        return "quota_exhausted"
    if looks_like_provider_auth_error(normalized):
        return "authentication"
    if status == 429 or any(
        marker in normalized
        for marker in ("rate_limit", "rate limit", "too many requests")
    ):
        return "rate_limit"
    if (
        status in {408, 425}
        or (status is not None and status >= 500)
        or any(
            marker in normalized
            for marker in (
                "overloaded",
                "temporarily unavailable",
                "service unavailable",
                "timed out",
                "timeout",
                "connection reset",
                "connection refused",
            )
        )
    ):
        return "provider_unavailable"
    if status == 403 or "permission_error" in normalized:
        return "permission_denied"
    return "provider_error"
