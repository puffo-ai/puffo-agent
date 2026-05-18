"""PUF-214: cover the worker-layer error-leak suppression filter.

The full ``Worker._run`` send-fallback wiring is integration-heavy.
The load-bearing logic is the pattern matcher; this matrix pins the
positive cases (the four real leak strings from FB-105 / FB-88 /
FB-159 case-studies) and the negative cases (legitimate prose that
mentions the same keywords). Overreach guard tests are explicit
because Equation flagged that as the main risk.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.state import RuntimeState
from puffo_agent.portal.worker import (
    _handle_suppressed_reply,
    _looks_like_auth_error,
    _suppress_worker_error_leak,
)


# ── Positive cases (must suppress) ─────────────────────────────────


def test_suppresses_claude_cli_not_logged_in():
    """Sheri / Jhope / Yasushi case-study line — Claude CLI emits
    this verbatim when its OAuth token is dead."""
    leak = "Not logged in · Please run /login"
    assert _suppress_worker_error_leak(leak) is None


def test_suppresses_claude_cli_not_logged_in_multiline():
    leak = (
        "Not logged in.\n"
        "Please run /login to authenticate, then retry."
    )
    assert _suppress_worker_error_leak(leak) is None


def test_suppresses_echoed_kick_text():
    """When Claude echoes the kick message verbatim instead of
    treating it as a system frame, the bracketed prefix is the
    load-bearing signal — the primer tells agents never to echo it."""
    leak = (
        "[puffo-agent system message] session errored on rate "
        "limiting, please resume processing."
    )
    assert _suppress_worker_error_leak(leak) is None


def test_suppresses_anthropic_authentication_error():
    leak = (
        "Error: 401 authentication_error — invalid x-api-key. "
        "Please check the credentials file."
    )
    assert _suppress_worker_error_leak(leak) is None


def test_suppresses_anthropic_rate_limit_error():
    leak = "Error: 429 rate_limit_error — too many requests. Retry later."
    assert _suppress_worker_error_leak(leak) is None
    # Also matches the hyphenated / spaced variants the model might
    # produce when reformatting.
    assert _suppress_worker_error_leak("rate-limit-error: try again") is None
    assert _suppress_worker_error_leak("rate limit error happened") is None


# ── Negative cases (must NOT suppress — legitimate prose) ────────


def test_lets_through_helpful_login_prose():
    """Real agent talking about logins / /login slash without the
    Claude CLI error signature — should pass through unchanged.
    This is the Equation overreach guard."""
    reply = (
        "I'd be happy to help you log in to your bank — what's the "
        "issue? Are you not logged in to the right account?"
    )
    assert _suppress_worker_error_leak(reply) == reply


def test_lets_through_helpful_rate_limit_prose():
    """Agent describing a rate limit it's hit, in prose. No error-
    name signature → pass-through."""
    reply = "Sorry, I'm hitting a rate limit, let me retry in a moment."
    assert _suppress_worker_error_leak(reply) == reply


def test_lets_through_authentication_topic_prose():
    """The word 'authentication' alone is fine; the regex anchors
    on the underscore-delimited error name."""
    reply = (
        "We can talk about authentication later — for now, let's "
        "focus on the API design."
    )
    assert _suppress_worker_error_leak(reply) == reply


def test_lets_through_empty_reply():
    """``if reply:`` at the call site already short-circuits empty
    replies; the filter shouldn't change that semantics."""
    assert _suppress_worker_error_leak("") == ""


def test_lets_through_normal_agent_message():
    reply = "Got it — I'll push that change and ping Solution for QA."
    assert _suppress_worker_error_leak(reply) == reply


# ── _looks_like_auth_error helper ──────────────────────────────────


def test_looks_like_auth_error_positive_cases():
    assert _looks_like_auth_error("Not logged in. Please run /login")
    assert _looks_like_auth_error("401 authentication_error: invalid api key")


def test_looks_like_auth_error_negative_cases():
    # Rate-limit string is suppressed by the broader filter but NOT
    # flagged as auth — runtime.health stays correct on the
    # operator's status view.
    assert not _looks_like_auth_error(
        "[puffo-agent system message] session errored on rate"
    )
    assert not _looks_like_auth_error("rate_limit_error")
    # Prose about authentication doesn't trip it.
    assert not _looks_like_auth_error(
        "Talking about authentication best practices."
    )
    assert not _looks_like_auth_error("")


# ── _handle_suppressed_reply: integration-level contract ───────────
#
# Equation's QA ask: confirm that when the filter suppresses,
# (a) runtime.error is populated, (b) the caller does NOT proceed to
# send_fallback_message. We can't easily mount a full Worker here,
# but the helper carries the suppression contract; the call site is
# the trivial ``if reply and not _handle_suppressed_reply(...): await
# client.send_fallback_message(...)``. Testing the helper's return
# value + side effects covers the contract.


def test_handle_suppressed_reply_returns_true_on_leak_fallback_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    suppressed = _handle_suppressed_reply(
        "Not logged in. Please run /login",
        runtime,
        "agent-suppress-fallback",
        scope="fallback",
        treat_auth_as_health=False,
    )
    assert suppressed is True
    # Operator-facing surface: runtime.error populated, NOT a channel
    # post. The "Check daemon logs" copy is the fallback-scope variant.
    assert "suppressed from channel post" in runtime.error
    assert "Check daemon logs" in runtime.error
    # treat_auth_as_health=False → health stays unchanged.
    assert runtime.health == "unknown"
    # Persisted to disk so ``puffo-agent status`` picks it up.
    reloaded = RuntimeState.load("agent-suppress-fallback")
    assert reloaded is not None
    assert reloaded.error == runtime.error


def test_handle_suppressed_reply_returns_true_on_leak_api_retry_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    suppressed = _handle_suppressed_reply(
        "Not logged in. Please run /login",
        runtime,
        "agent-suppress-retry",
        scope="api-error-retry",
        treat_auth_as_health=True,
    )
    assert suppressed is True
    # API-retry scope tells the operator how to recover.
    assert "puffo-agent agent resume agent-suppress-retry" in runtime.error
    # Auth-class leak flips health for the operator's status view.
    assert runtime.health == "auth_failed"


def test_handle_suppressed_reply_rate_limit_does_not_flip_health(tmp_path, monkeypatch):
    """Rate-limit leak is still suppressed but should NOT mark the
    agent auth_failed — that mark is reserved for OAuth-class
    failures the operator recovers via ``claude /login``."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    suppressed = _handle_suppressed_reply(
        "Error: 429 rate_limit_error — too many requests.",
        runtime,
        "agent-rate-limit",
        scope="api-error-retry",
        treat_auth_as_health=True,
    )
    assert suppressed is True
    assert runtime.health == "unknown"  # NOT auth_failed


def test_handle_suppressed_reply_returns_false_on_legit_prose(tmp_path, monkeypatch):
    """The Equation overreach guard at the call-site contract level:
    legit prose returns False, leaves runtime untouched, and the
    caller proceeds to send the message normally."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    suppressed = _handle_suppressed_reply(
        "Got it — pushing that PR shortly.",
        runtime,
        "agent-clean",
        scope="fallback",
        treat_auth_as_health=False,
    )
    assert suppressed is False
    # Runtime untouched — no error message, no health flip, no save
    # to disk.
    assert runtime.error == ""
    assert runtime.health == "unknown"
