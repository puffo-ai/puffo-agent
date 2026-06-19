"""Codex auth-failure classification — `_looks_like_codex_auth_error`
matches the verbatim strings captured in the d2d2 stuck-state case
(refresh token revoked / token_invalidated / /responses 401) while
NOT matching downstream symptoms (invalid thread id) or unrelated
adapter failures (model not supported / timeout). Pinning this keeps
the PUF-310 substrate from auto-flipping legitimate non-auth errors
to ``auth_failed``."""

from __future__ import annotations

import pytest

from puffo_agent.agent.adapters.codex_session import (
    _looks_like_codex_auth_error,
)


@pytest.mark.parametrize("err_text", [
    "codex turn failed: refresh token was revoked",
    "codex turn failed: refresh token revoked",  # missing 'was'
    "codex turn failed: REFRESH TOKEN WAS REVOKED",  # case-insensitive
    "codex turn failed: {'error': 'token_invalidated'}",
    "codex turn failed: token_invalidated by server",
    "codex turn failed: websocket /responses returned 401 Unauthorized",
    "codex turn failed: 401 Unauthorized from /responses",  # reversed order
    "codex turn failed: {error: {code: 401, path: /responses}}",
])
def test_verbatim_d2d2_auth_strings_classify_as_auth(err_text):
    """The four pattern families anchored to msg_2237ad78's d2d2-case
    verbatim strings + the most likely whitespace/case variants."""
    assert _looks_like_codex_auth_error(err_text)


@pytest.mark.parametrize("err_text", [
    "codex turn failed: invalid thread id: invalid length: expected 32, found 0",
    "codex turn failed: model not supported",
    "codex turn failed: thread limit reached",
    "codex turn failed: connection reset by peer",
    "codex turn failed: TimeoutError",
    "agent thread limit reached",
    "",
])
def test_non_auth_errors_do_not_classify_as_auth(err_text):
    """``invalid thread id ... found 0`` is downstream symptom of an
    empty conversation_id (per d2d2 diagnostic chain), NOT an auth
    signal. Model / timeout / quota errors stay out too — they reach
    the worker via the generic Exception path and don't fire DM."""
    assert not _looks_like_codex_auth_error(err_text)


def test_none_input_does_not_raise():
    """Defensive: classifier is called on ``str(turn_failed_exc)``,
    which is always a string in practice, but ``err_text or ""``
    coalesces a stray ``None`` to "" rather than raising."""
    assert _looks_like_codex_auth_error(None) is False  # type: ignore[arg-type]


def test_invalid_thread_id_not_auth_class():
    """Explicit regression-pin for the d2d2 diagnostic chain: ``invalid
    thread id ... found 0`` was 矩阵's initial (incorrect) auth-guess,
    later narrowed to in-memory empty-cid (PUF-311 substance). Keep
    this pattern OUT of PUF-310's auth set so the runtime.health flip
    doesn't fire on a thread-state issue."""
    assert not _looks_like_codex_auth_error(
        "codex turn failed: invalid thread id: invalid length: "
        "expected length 32 for simple format, found 0"
    )


# ── Classifier precision: /responses + 401 false-positive probes ──────────
# The clause-bound pattern rejects cases where /responses and 401 appear
# in the same string but for unrelated reasons (cache references, prose
# mentioning the endpoint, retries-exhausted log lines).


@pytest.mark.parametrize("err_text", [
    # Solution's polish-round flag: cache hit references both substrings.
    "upstream cache hit for /responses; 401 retries exhausted in batch 401",
    # Solution's polish-round flag: agent prose + a separate quota error.
    "prompt referenced /responses endpoint; 401 model not supported",
    # Cross-sentence: a period breaks the clause.
    "Hit /responses. Later batch returned 401 from another endpoint.",
    # Multi-line log: \n breaks the clause.
    "fetched /responses ok\n401 errors logged on a different stream",
    # /responses and 401 too far apart in the same clause (>40 chars).
    "/responses is the streaming endpoint per the codex docs page 42 and 401 "
    "is mentioned only as part of a quota retry path with backoff",
])
def test_responses_and_401_unrelated_does_not_classify(err_text):
    """False-positive probes for the /responses + 401 co-occurrence
    rule. Each string contains BOTH substrings but the surrounding
    structure makes it clear they're not signalling a real /responses
    401 auth failure — the clause-bound regex must reject them."""
    assert not _looks_like_codex_auth_error(err_text)


@pytest.mark.parametrize("err_text", [
    # JSON envelope shape (TRUE — code + path in same object).
    "{\"error\": {\"code\": 401, \"path\": \"/responses\"}}",
    # Stderr-style line.
    "websocket /responses returned 401 Unauthorized",
    # Reversed ordering with short prefix.
    "401 Unauthorized from /responses",
    # The verbatim d2d2 paraphrase Equation captured.
    "websocket /responses 401",
])
def test_responses_and_401_genuine_still_classifies(err_text):
    """Regression-pin: the precision tightening must NOT regress the
    genuine d2d2-class shapes the classifier was built to catch."""
    assert _looks_like_codex_auth_error(err_text)


@pytest.mark.parametrize("err_text", [
    # Observed live on a codex relogin: the human-readable form (a space,
    # not the ``token_invalidated`` JSON field) + a 401 on
    # ``/backend-api/codex`` rather than ``/responses``.
    "failed to refresh available models: unexpected status 401 Unauthorized: "
    "Encountered invalidated oauth token for user, failing request, "
    "url: https://chatgpt.com/backend-api/codex/models, "
    "auth error: identity_edge_internal_error",
    "401 Unauthorized: Encountered invalidated oauth token",
    "invalidated oauth token for user",
    "request to /backend-api/codex/models returned 401",
])
def test_real_world_invalidated_oauth_token_classifies(err_text):
    """Live-observed variant the original d2d2 anchors missed: codex emits
    ``invalidated oauth token`` (space form) and 401s on /backend-api/codex,
    not just /responses. Both must classify as auth."""
    assert _looks_like_codex_auth_error(err_text)
