"""PUF-398: quota-exhausted (`drained`) agent state.

The bug this closes (JYP, Sam DM msg_3083b1b6): an account whose plan
quota was spent surfaced as *"my Claude Code sign-in has expired — run
`claude auth login`"*. Re-login cannot refill a quota, so the operator
followed the instruction and stayed stuck.

The load-bearing property is therefore an ORDERING one — quota must be
classified before auth at every site — not merely "we can spot a
usage-limit string". The ordering tests below feed a body that matches
BOTH classes; they are the ones that go red if the checks are swapped
back.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncio

import pytest

from puffo_agent.agent._invite_strings import (
    format_codex_drained,
    format_drained,
)
from puffo_agent.agent._usage_markers import looks_like_usage_limit
from puffo_agent.agent.core import AgentAPIError, _classify_api_error
from puffo_agent.portal.control.usage_snapshot import (
    apply_drained_health,
    drained_harnesses,
)
from puffo_agent.portal.credential_refresh import (
    RefreshOutcome,
    _classify_failed_refresh,
)
from puffo_agent.portal.state import RuntimeState
from puffo_agent.portal.worker import (
    Worker,
    _handle_suppressed_reply,
    _looks_like_drained,
    _suppress_worker_error_leak,
)


# The spellings Claude Code has shipped for this event across versions
# and surfaces. Anchoring on any single one would silently stop matching
# on the next release, so all four are pinned.
CLAUDE_LIMIT_STRINGS = [
    "Claude usage limit reached. Your limit will reset at 1pm (Etc/GMT+5).",
    "Claude AI usage limit reached|1749924000",
    "5-hour limit reached ∙ resets 3pm",
    "Usage limit reached · resets at 11:30 · Upgrade",
]


# ── Detection ──────────────────────────────────────────────────────


@pytest.mark.parametrize("text", CLAUDE_LIMIT_STRINGS)
def test_detects_every_shipped_claude_usage_limit_spelling(text):
    assert looks_like_usage_limit(text) is True
    assert _looks_like_drained(text) is True


@pytest.mark.parametrize("text", CLAUDE_LIMIT_STRINGS)
def test_usage_limit_strings_are_suppressed_not_posted(text):
    """A quota string reaching the channel is a leak like any other."""
    assert _suppress_worker_error_leak(text) is None


@pytest.mark.parametrize(
    "prose",
    [
        "I've reached the limit of what I can infer from this diff.",
        "The rate limit on that endpoint is 100 req/s.",
        "We hit a limit reached in the parser's lookahead — see line 40.",
        "Their quota system resets weekly, which is worth documenting.",
    ],
)
def test_does_not_fire_on_agent_prose_about_limits(prose):
    """Overreach guard: these are things an agent legitimately says."""
    assert _looks_like_drained(prose) is False


# ── Ordering: quota beats auth (the actual JYP bug) ────────────────

# A real drained response that also carries auth-adjacent wording. Both
# classifiers match this; only the order decides which one wins.
QUOTA_WITH_AUTH_WORDING = (
    "API Error: Claude usage limit reached. Your limit will reset at "
    "1pm. authentication failed for this request."
)


def test_core_classifies_quota_before_auth():
    is_auth, is_drained, label = _classify_api_error(QUOTA_WITH_AUTH_WORDING)
    assert (is_auth, is_drained, label) == (False, True, "quota-drained")


def test_worker_flips_drained_not_auth_failed_on_mixed_body(tmp_path, monkeypatch):
    """The JYP repro. If this asserts auth_failed, the operator is being
    told to re-login over a spent quota again."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    suppressed, _ = _handle_suppressed_reply(
        QUOTA_WITH_AUTH_WORDING,
        runtime,
        "agent-jyp",
        scope="api-error-retry",
    )
    assert suppressed is True
    assert runtime.health == "drained"
    assert "claude auth login" not in runtime.error
    assert "sign-in expired" not in runtime.error.lower()
    # It says the opposite, on purpose — that's the corrected surface.
    assert "not a sign-in problem" in runtime.error.lower()
    assert RuntimeState.load("agent-jyp").health == "drained"


def test_refresher_does_not_call_a_spent_quota_an_auth_failure():
    """The other half of the JYP surface: the credential refresher's own
    probe hits the same limit and flips EVERY agent on the host."""
    outcome = _classify_failed_refresh(
        QUOTA_WITH_AUTH_WORDING, "", rc=1, elapsed=0.5, log_prefix="test",
    )
    assert outcome is RefreshOutcome.RATE_LIMITED


def test_refresher_still_reports_a_genuine_auth_failure():
    """Non-regression on PUF-283 / PUF-303: a real revoked token must
    still reach AUTH_FAILED now that a check sits in front of it."""
    outcome = _classify_failed_refresh(
        "OAuth token revoked: authentication failed", "",
        rc=1, elapsed=0.5, log_prefix="test",
    )
    assert outcome is RefreshOutcome.AUTH_FAILED


def test_auth_body_without_quota_wording_still_flips_auth_failed(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    _handle_suppressed_reply(
        "Not logged in · Please run /login",
        runtime,
        "agent-real-auth",
        scope="api-error-retry",
    )
    assert runtime.health == "auth_failed"


# ── Transition + DM dedup ──────────────────────────────────────────


def test_drained_enter_fires_callback_once_per_episode(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")
    fired = []
    for _ in range(3):
        _handle_suppressed_reply(
            CLAUDE_LIMIT_STRINGS[0],
            runtime,
            "agent-dedup",
            scope="fallback",
            on_drained_enter=lambda: fired.append(1),
        )
    # Only the was-ok → drained edge notifies; re-entries stay quiet.
    assert fired == [1]


def test_drained_enter_callback_failure_does_not_break_suppression(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running")

    def boom():
        raise RuntimeError("no client")

    suppressed, _ = _handle_suppressed_reply(
        CLAUDE_LIMIT_STRINGS[0],
        runtime,
        "agent-cb-raises",
        scope="fallback",
        on_drained_enter=boom,
    )
    assert suppressed is True
    assert runtime.health == "drained"


# ── Clear ──────────────────────────────────────────────────────────


def test_successful_turn_clears_drained(tmp_path, monkeypatch):
    """A turn only completes once the window actually refilled, so
    success IS the reset signal."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running", health="drained", error="spent")
    import logging

    assert Worker._clear_drained_if_recoverable(
        runtime, "agent-clear", logging.getLogger("t"),
    ) is True
    assert runtime.health == "ok"
    assert runtime.error == ""


def test_clear_leaves_other_red_states_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    import logging

    runtime = RuntimeState(status="running", health="auth_failed", error="e")
    assert Worker._clear_drained_if_recoverable(
        runtime, "agent-other-red", logging.getLogger("t"),
    ) is False
    assert runtime.health == "auth_failed"


# ── Snapshot-driven detection (both providers) ─────────────────────


def test_claude_session_at_100_is_drained():
    snapshot = {"claude-code": {
        "session": {"used_pct": 100, "resets_at": 1760000000},
        "weekly": {"used_pct": 42},
    }}
    assert drained_harnesses(snapshot) == {"claude-code": 1760000000}


def test_weekly_at_100_is_drained_even_with_session_headroom():
    """The provider refuses turns either way; reporting only the session
    window would leave the agent looking healthy while it can't answer."""
    snapshot = {"claude-code": {
        "session": {"used_pct": 3},
        "weekly": {"used_pct": 100, "resets_at": 1760000000},
    }}
    assert drained_harnesses(snapshot) == {"claude-code": 1760000000}


def test_codex_at_100_is_drained():
    snapshot = {"codex": {
        "session": {"used_pct": 100},
        "weekly": {"used_pct": 100, "resets_at": 1770000000},
    }}
    # No resets_at on the session window → the soonest KNOWN reset wins.
    assert drained_harnesses(snapshot) == {"codex": 1770000000}


def test_below_100_is_not_drained():
    snapshot = {"claude-code": {"session": {"used_pct": 99}}}
    assert drained_harnesses(snapshot) == {}


def test_missing_resets_at_still_reports_drained():
    snapshot = {"claude-code": {"session": {"used_pct": 100}}}
    assert drained_harnesses(snapshot) == {"claude-code": None}


def _stub_agents(monkeypatch, mapping):
    monkeypatch.setattr(
        "puffo_agent.portal.control.usage_snapshot.agent_harnesses",
        lambda: mapping,
    )


def test_snapshot_flips_health_for_agents_on_the_spent_harness(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    _stub_agents(monkeypatch, {"a-claude": "claude-code", "a-codex": "codex"})
    RuntimeState(status="running", health="ok").save("a-claude")
    RuntimeState(status="running", health="ok").save("a-codex")

    apply_drained_health({
        "claude-code": {"session": {"used_pct": 100}},
        "codex": {"session": {"used_pct": 10}},
    })

    assert RuntimeState.load("a-claude").health == "drained"
    # Same host, different harness, still has budget — must not be swept in.
    assert RuntimeState.load("a-codex").health == "ok"


def test_snapshot_clears_drained_when_budget_returns(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    _stub_agents(monkeypatch, {"a-claude": "claude-code"})
    RuntimeState(status="running", health="drained", error="spent").save("a-claude")

    apply_drained_health({"claude-code": {"session": {"used_pct": 12}}})

    reloaded = RuntimeState.load("a-claude")
    assert reloaded.health == "ok"
    assert reloaded.error == ""


def test_snapshot_does_not_paper_over_auth_failed(tmp_path, monkeypatch):
    """A drained snapshot must not overwrite a genuine auth failure —
    that would hide the one state the operator CAN act on."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    _stub_agents(monkeypatch, {"a-claude": "claude-code"})
    RuntimeState(status="running", health="auth_failed", error="e").save("a-claude")

    apply_drained_health({"claude-code": {"session": {"used_pct": 100}}})

    assert RuntimeState.load("a-claude").health == "auth_failed"


# ── Retry boundary: drained holds, it does not retry ───────────────


def _make_client():
    import logging

    from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient

    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "tester-1234"
    client._log = logging.getLogger("test-drained")
    client.MAX_API_ERROR_RETRIES = 3  # type: ignore[attr-defined]

    class _StubStore:
        async def mark_thread_processed(self, *a, **k):
            return None

    client.store = _StubStore()  # type: ignore[assignment]
    return client


class _Entry:
    def __init__(self):
        self.dispatching_ids: set[str] = set()


@pytest.mark.asyncio
async def test_drained_skips_kick_retries_and_does_not_abandon():
    """Vase's approved shape: hold, don't retry. Retrying just re-hits
    the same wall; firing abandon would overwrite `drained` with
    `api_error_abandoned` and lose the state the UI shows."""
    client = _make_client()
    retries: list[int] = []
    abandons: list[int] = []

    async def on_retry(root_id, batch, channel_meta):
        retries.append(1)

    async def on_abandon(root_id, batch, channel_meta, attempts):
        abandons.append(attempts)

    await client._do_api_error_retries(  # type: ignore[arg-type]
        root_id="r",
        entry=_Entry(),  # type: ignore[arg-type]
        batch=[{"envelope_id": "e1"}],
        channel_meta={},
        on_api_error_retry=on_retry,
        on_api_error_abandon=on_abandon,
        last_envelope="e1",
        is_auth=False,
        is_drained=True,
    )
    assert retries == []
    assert abandons == []


@pytest.mark.asyncio
async def test_plain_rate_limit_still_retries_with_drained_flag_off():
    """Non-regression: adding the drained short-circuit must not turn
    ordinary transient 429s into no-retry."""
    client = _make_client()
    retries: list[int] = []

    async def on_retry(root_id, batch, channel_meta):
        retries.append(1)
        raise AgentAPIError("still rate-limited")

    async def on_abandon(*a):
        return None

    real_sleep = asyncio.sleep
    asyncio.sleep = lambda _s: real_sleep(0)  # type: ignore[assignment]
    try:
        await client._do_api_error_retries(  # type: ignore[arg-type]
            root_id="r",
            entry=_Entry(),  # type: ignore[arg-type]
            batch=[{"envelope_id": "e1"}],
            channel_meta={},
            on_api_error_retry=on_retry,
            on_api_error_abandon=on_abandon,
            last_envelope="e1",
            is_auth=False,
            is_drained=False,
        )
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
    assert len(retries) == client.MAX_API_ERROR_RETRIES


# ── Operator-facing copy ───────────────────────────────────────────


def test_drained_dm_never_tells_the_operator_to_log_in():
    """The entire point of the ticket."""
    text = format_drained("agent-1", "Testy", resets_at=1760000000)
    low = text.lower()
    assert "auth login" not in low
    assert "codex login" not in low
    assert "sign-in has expired" not in low
    assert "not a sign-in problem" in low


def test_drained_dm_offers_the_four_real_recoveries():
    text = format_drained("agent-1", "Testy")
    low = text.lower()
    assert "wait for the window to reset" in low
    assert "smaller model" in low
    assert "add credits" in low
    assert "upgrade the plan" in low


def test_drained_dm_is_bilingual():
    text = format_drained("agent-1", "Testy")
    assert "额度" in text and "**Your options:**" in text


def test_drained_dm_includes_reset_time_when_known():
    with_reset = format_drained("agent-1", "Testy", resets_at=1760000000)
    without = format_drained("agent-1", "Testy")
    assert "It resets around" in with_reset
    # No reset time from the provider → no empty/garbled clause.
    assert "It resets around" not in without
    assert "None" not in without


def test_codex_dm_names_codex_not_claude():
    text = format_codex_drained("agent-1", "Testy")
    assert "Codex usage limit" in text
    assert "Claude Code usage limit" not in text
