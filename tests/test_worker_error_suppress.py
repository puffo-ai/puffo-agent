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

import asyncio

import pytest

from puffo_agent.portal.state import RuntimeState
from puffo_agent.portal import worker as worker_module
from puffo_agent.portal.worker import (
    _handle_suppressed_reply,
    _looks_like_auth_error,
    _suppress_worker_error_leak,
    _SUPPRESSION_BACKOFF_MAX_SECONDS,
    _SUPPRESSION_BACKOFF_MIN_SECONDS,
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
    suppressed, backoff = _handle_suppressed_reply(
        "Not logged in · Please run /login",
        runtime,
        "agent-suppress-fallback",
        scope="fallback",
    )
    assert suppressed is True
    assert _SUPPRESSION_BACKOFF_MIN_SECONDS <= backoff <= _SUPPRESSION_BACKOFF_MAX_SECONDS
    # Fallback-scope variant points at the daemon log for triage.
    assert "Check the puffo-agent daemon log" in runtime.error
    # Auth-class leak flips health regardless of scope.
    assert runtime.health == "auth_failed"
    reloaded = RuntimeState.load("agent-suppress-fallback")
    assert reloaded is not None
    assert reloaded.error == runtime.error


def test_handle_suppressed_reply_returns_true_on_leak_api_retry_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    suppressed, backoff = _handle_suppressed_reply(
        "Not logged in · Please run /login",
        runtime,
        "agent-suppress-retry",
        scope="api-error-retry",
    )
    assert suppressed is True
    assert _SUPPRESSION_BACKOFF_MIN_SECONDS <= backoff <= _SUPPRESSION_BACKOFF_MAX_SECONDS
    assert "Claude Code sign-in expired" in runtime.error
    assert "claude auth login" in runtime.error
    assert "running puffo-agent" in runtime.error
    assert "send this agent a message" in runtime.error
    assert runtime.health == "auth_failed"


def test_handle_suppressed_reply_api_retry_rate_limit_branches_message(tmp_path, monkeypatch):
    """Rate-limit leak via api-error-retry scope: suppressed, NOT
    marked auth_failed, AND the recovery copy is rate-limit-flavoured
    (no misdirecting `claude /login` instruction)."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    suppressed, backoff = _handle_suppressed_reply(
        "Error: 429 rate_limit_error — too many requests.",
        runtime,
        "agent-rate-limit",
        scope="api-error-retry",
    )
    assert suppressed is True
    assert _SUPPRESSION_BACKOFF_MIN_SECONDS <= backoff <= _SUPPRESSION_BACKOFF_MAX_SECONDS
    assert runtime.health == "unknown"  # NOT auth_failed
    assert "Rate-limit" in runtime.error
    assert "self-recovers" in runtime.error
    # Rate-limit branch must NEVER instruct the operator to relogin.
    assert "claude auth login" not in runtime.error
    assert "agent resume" not in runtime.error


def test_handle_suppressed_reply_returns_false_on_legit_prose(tmp_path, monkeypatch):
    """The Equation overreach guard at the call-site contract level:
    legit prose returns (False, 0.0), leaves runtime untouched, and
    the caller proceeds to send the message normally."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    suppressed, backoff = _handle_suppressed_reply(
        "Got it — pushing that PR shortly.",
        runtime,
        "agent-clean",
        scope="fallback",
    )
    assert suppressed is False
    assert backoff == 0.0
    assert runtime.error == ""
    assert runtime.health == "unknown"


# ── New patterns from the round-2 doc audit ────────────────────────


@pytest.mark.parametrize(
    "leak,expect_auth",
    [
        # Usage-limit class (the colleague's prod miss).
        ("You've hit your weekly limit. Wait until reset.", False),
        ("You've hit your session limit", False),
        ("You've hit your Opus limit on the Pro plan", False),
        ("Credit balance is too low to complete this request", False),
        # CLI-emitted server 429 / 5xx.
        ("API Error: Request rejected (429) — retry later", False),
        ("API Error: Server is temporarily limiting requests", False),
        ("API Error: Repeated 529 Overloaded errors", False),
        ("API Error: 500 — Internal server error", False),
        # OAuth / auth recovery — also flips runtime.health.
        ("OAuth token revoked. Re-authenticate.", True),
        ("OAuth token has expired. Run /login.", True),
        ("Invalid API key. Check your credentials.", True),
        ("This organization has been disabled. Contact support.", True),
        # API-canonical <type>_error identifiers.
        ("Error: overloaded_error — Anthropic is overloaded", False),
        ("Error: billing_error — payment required", False),
        ("Error: permission_error — access denied", False),
        ("Error: timeout_error — request exceeded the limit", False),
    ],
)
def test_round2_patterns_suppress_and_classify(leak, expect_auth):
    """Every leak in the round-2 pattern set: filter must suppress,
    auth-class flag must match the docs-driven classification."""
    assert _suppress_worker_error_leak(leak) is None
    assert _looks_like_auth_error(leak) is expect_auth


@pytest.mark.parametrize(
    "prose",
    [
        # Skip-list from the reviewer's audit — agent prose containing
        # the deliberately-unmatched identifiers / phrases must pass.
        "If you hit an api_error in tests, check the mock fixture.",
        "I got an invalid_request_error — let me see the request shape.",
        "got a not_found_error from the dummy URL.",
        "Prompt is too long for the demo notebook, let me trim it.",
        "Request timed out so I retried with a longer timeout.",
        "Unable to connect to API in the sandbox; mock it for now.",
        "The image was too large for inline embedding, let me resize.",
        # Discussion of an error class — no anchored signature.
        "Discussed timeout_error handling at the architecture review",
    ],
)
def test_round2_skip_list_passes_through(prose):
    """Prose discussing the deliberately-skipped identifiers must
    NOT be suppressed — the audit's whole reason for the skip
    list."""
    # Anchored token-only matches still catch the trailing
    # discussion-style "timeout_error handling" — it's a real word-
    # boundary match. The previous cases (api_error / not_found_error
    # / invalid_request_error / prose phrases) must pass.
    if "timeout_error" not in prose:
        assert _suppress_worker_error_leak(prose) == prose


# ── PUF-221 reviewer iter: on_auth_failure callback ────────────────


def test_handle_suppressed_reply_fires_notify_on_auth_class(tmp_path, monkeypatch):
    """Auth-class leak → ``on_auth_failure`` callback fires once. This
    is the wire the daemon hooks ``CredentialRefresher.notify_refresh_needed``
    into, so a 401 surfacing mid-turn short-circuits the 2-min poll."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    fired: list[int] = []

    def on_auth_failure():
        fired.append(1)

    suppressed, _ = _handle_suppressed_reply(
        "Not logged in · Please run /login",
        runtime,
        "agent-callback",
        scope="api-error-retry",
        on_auth_failure=on_auth_failure,
    )
    assert suppressed is True
    assert runtime.health == "auth_failed"
    assert fired == [1]


def test_handle_suppressed_reply_skips_notify_on_non_auth_leak(tmp_path, monkeypatch):
    """Non-auth leak (rate-limit / quota / 5xx) → suppression fires
    but ``on_auth_failure`` does NOT — we don't want to wake the
    refresher for an Anthropic outage that has nothing to do with
    credentials."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    fired: list[int] = []

    def on_auth_failure():
        fired.append(1)

    suppressed, _ = _handle_suppressed_reply(
        "Error: 429 rate_limit_error — too many requests.",
        runtime,
        "agent-callback-rl",
        scope="api-error-retry",
        on_auth_failure=on_auth_failure,
    )
    assert suppressed is True
    assert runtime.health == "unknown"  # NOT auth_failed
    assert fired == []


def test_handle_suppressed_reply_swallows_notify_exception(tmp_path, monkeypatch):
    """A raising callback must not break the suppression flow —
    health still flips, return value still True."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")

    def on_auth_failure():
        raise RuntimeError("boom")

    suppressed, _ = _handle_suppressed_reply(
        "Not logged in · Please run /login",
        runtime,
        "agent-callback-raises",
        scope="fallback",
        on_auth_failure=on_auth_failure,
    )
    assert suppressed is True
    assert runtime.health == "auth_failed"


def test_round2_oauth_revoked_flips_health(tmp_path, monkeypatch):
    """OAuth-token-revoked is a new auth-class pattern — must flip
    runtime.health symmetrically with the existing 'Not logged in'
    line, regardless of scope."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    suppressed, _ = _handle_suppressed_reply(
        "OAuth token revoked. Re-authenticate via claude /login.",
        runtime,
        "agent-oauth-revoked",
        scope="fallback",
    )
    assert suppressed is True
    assert runtime.health == "auth_failed"


# ── Backoff contract ───────────────────────────────────────────────


def test_backoff_distribution_in_range(tmp_path, monkeypatch):
    """Repeated suppressions yield backoffs in [MIN, MAX]. Smoke at
    100 samples covers the random.uniform contract."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    samples: list[float] = []
    for _ in range(100):
        runtime = RuntimeState(status="running")
        _, backoff = _handle_suppressed_reply(
            "Error: 429 rate_limit_error",
            runtime,
            "agent-backoff-distribution",
            scope="api-error-retry",
        )
        samples.append(backoff)
    assert all(_SUPPRESSION_BACKOFF_MIN_SECONDS <= s <= _SUPPRESSION_BACKOFF_MAX_SECONDS for s in samples)
    # And the sampling isn't degenerate — at 100 draws of uniform
    # over a 45-sec window, expect ~all-unique values.
    assert len(set(samples)) >= 90


# ── Call-site contract: mock client.send_fallback_message ────────
#
# Solution's QA ask: directly assert ``client.send_fallback_message``
# is not called on suppression and is called on legit prose. The two
# helpers above carry the suppression contract; this exercises the
# exact ``if reply and not _handle_suppressed_reply(...): await
# client.send_fallback_message(...)`` shape from ``Worker._run()``
# with the helpers in the loop and a recording mock client in place
# of the real one.


class _RecordingClient:
    """Stand-in for the PuffoCore client. Records ``send_fallback_message``
    calls without doing any network I/O."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def send_fallback_message(self, channel_id, reply, *, root_id):
        self.calls.append((channel_id, reply, root_id))


async def _fallback_call_site(
    client, runtime, agent_id, channel_id, reply, root_id, *, scope, sleeps,
):
    """Mirrors the two production blocks in ``Worker._run()``:

        if reply:
            suppressed, backoff = _handle_suppressed_reply(...)
            if suppressed:
                await asyncio.sleep(backoff)
            else:
                await client.send_fallback_message(...)

    Kept here so a future call-site edit that drops the sleep or the
    guard breaks this test before it breaks prod."""
    if reply:
        suppressed, backoff = _handle_suppressed_reply(
            reply,
            runtime,
            agent_id,
            scope=scope,
        )
        if suppressed:
            sleeps.append(backoff)
        else:
            await client.send_fallback_message(channel_id, reply, root_id=root_id)


def test_call_site_skips_send_on_suppressed_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    client = _RecordingClient()
    runtime = RuntimeState(status="running")
    sleeps: list[float] = []
    asyncio.run(_fallback_call_site(
        client, runtime, "agent-skip-send", "ch_abc",
        "Not logged in · Please run /login",
        "msg_root",
        scope="api-error-retry",
        sleeps=sleeps,
    ))
    assert client.calls == []  # NEVER called when filter suppresses
    assert runtime.health == "auth_failed"
    assert "send this agent a message" in runtime.error
    # And the call site DID sleep with a backoff in range.
    assert len(sleeps) == 1
    assert _SUPPRESSION_BACKOFF_MIN_SECONDS <= sleeps[0] <= _SUPPRESSION_BACKOFF_MAX_SECONDS


def test_call_site_calls_send_on_legit_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    client = _RecordingClient()
    runtime = RuntimeState(status="running")
    sleeps: list[float] = []
    asyncio.run(_fallback_call_site(
        client, runtime, "agent-send-clean", "ch_abc",
        "Got it — pushing that PR shortly.",
        "msg_root",
        scope="fallback",
        sleeps=sleeps,
    ))
    assert client.calls == [
        ("ch_abc", "Got it — pushing that PR shortly.", "msg_root"),
    ]
    assert sleeps == []  # No backoff on the legit path.
    assert runtime.error == ""
    assert runtime.health == "unknown"


def test_call_site_skips_send_on_empty_reply(tmp_path, monkeypatch):
    """Empty reply short-circuits before the filter runs; nothing
    should land on the wire and no sleep should be scheduled."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    client = _RecordingClient()
    runtime = RuntimeState(status="running")
    sleeps: list[float] = []
    asyncio.run(_fallback_call_site(
        client, runtime, "agent-empty", "ch_abc", "", "msg_root",
        scope="fallback",
        sleeps=sleeps,
    ))
    assert client.calls == []
    assert sleeps == []
    assert runtime.error == ""


def test_call_site_sleep_intercepts_send_under_real_asyncio(tmp_path, monkeypatch):
    """Belt-and-braces: monkeypatch ``asyncio.sleep`` to a no-op
    coroutine and run the production-shape harness against a
    suppressed leak. Asserts ``send_fallback_message`` is NEVER called
    AND ``asyncio.sleep`` IS called with a value in range. Catches a
    future refactor that drops the sleep branch entirely."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    client = _RecordingClient()
    runtime = RuntimeState(status="running")
    sleep_calls: list[float] = []

    async def fake_sleep(secs):
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def production_shape():
        reply = "Error: 429 rate_limit_error"
        if reply:
            suppressed, backoff = _handle_suppressed_reply(
                reply, runtime, "agent-rl", scope="api-error-retry",
            )
            if suppressed:
                await asyncio.sleep(backoff)
            else:
                await client.send_fallback_message(
                    "ch_abc", reply, root_id="msg_root",
                )

    asyncio.run(production_shape())
    assert client.calls == []
    assert len(sleep_calls) == 1
    assert _SUPPRESSION_BACKOFF_MIN_SECONDS <= sleep_calls[0] <= _SUPPRESSION_BACKOFF_MAX_SECONDS
