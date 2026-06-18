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
