"""Exception → process-outcome mapping. Sibling of ``_usage_markers``.

Quota before auth: a spent-plan API error also reports ``is_auth``.
"""

from __future__ import annotations

from .errors import AgentAPIError, ProviderFailureError


def failure_outcome(exc: Exception) -> str:
    """``drained`` | ``auth_failed`` | ``api_error_abandoned`` |
    ``provider_failed`` | ``failed``. Quota before auth."""
    if isinstance(exc, AgentAPIError):
        if exc.is_drained:
            return "drained"
        return "auth_failed" if exc.is_auth else "api_error_abandoned"
    if isinstance(exc, ProviderFailureError):
        return (
            "drained"
            if getattr(exc, "error_code", None) == "quota_exhausted"
            else "provider_failed"
        )
    return "failed"


def crash_resume_terminal(exc: Exception) -> tuple[str, str] | None:
    """``(error_text, outcome)``, or ``None`` when the retry budget applies."""
    outcome = failure_outcome(exc)
    if outcome == "drained":
        return (
            "crash resume quota exhausted"
            if isinstance(exc, AgentAPIError)
            else str(exc)
        ), "drained"
    if outcome == "provider_failed":
        return str(exc), "provider_failed"
    if outcome == "auth_failed":
        return "crash resume auth failure", "auth_failed"
    if not isinstance(exc, AgentAPIError):
        return f"crash resume unsafe failure: {type(exc).__name__}", "degraded"
    return None
