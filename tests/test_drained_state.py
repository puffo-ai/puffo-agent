"""Quota-exhausted (``drained``) agent state.

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
import logging

import pytest

from puffo_agent.agent._invite_strings import (
    format_codex_drained,
    format_drained,
)
from puffo_agent.agent._usage_markers import (
    DRAINED_RUNTIME_ERROR,
    looks_like_usage_limit,
    parse_reset_epoch,
)
from puffo_agent.agent.core import _classify_api_error
from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime
from puffo_agent.agent.errors import AgentAPIError, ProviderFailureError
from puffo_agent.agent._failure_outcomes import (
    crash_resume_terminal,
    failure_outcome,
)
from puffo_agent.agent.provider_failures import classify_provider_failure
from puffo_agent.portal.control.usage_snapshot import (
    apply_drained_health,
    drained_harnesses,
)
from puffo_agent.portal.credential_refresh import _classify_failed_refresh
from puffo_agent.portal.state import RuntimeState
from puffo_agent.portal.worker import Worker
from puffo_agent.portal.worker_run import StandardWorkerRun


# The spellings Claude Code has shipped for this event across versions
# and surfaces. Anchoring on any single one would silently stop matching
# on the next release, so all four are pinned.
CLAUDE_LIMIT_STRINGS = [
    "Claude usage limit reached. Your limit will reset at 1pm (Etc/GMT+5).",
    "Claude AI usage limit reached|1749924000",
    "5-hour limit reached ∙ resets 3pm",
    "Usage limit reached · resets at 11:30 · Upgrade",
]

# A real drained response that also carries auth-adjacent wording. Both
# classifiers match this; only the order decides which one wins.
QUOTA_WITH_AUTH_WORDING = (
    "API Error: Claude usage limit reached. Your limit will reset at "
    "1pm. authentication failed for this request."
)


# ── Detection ──────────────────────────────────────────────────────


@pytest.mark.parametrize("text", CLAUDE_LIMIT_STRINGS)
def test_detects_every_shipped_claude_usage_limit_spelling(text):
    assert looks_like_usage_limit(text) is True


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
    assert looks_like_usage_limit(prose) is False


def test_detection_is_case_insensitive():
    """Provider copy has shipped both cases; the `.lower()` in the
    substring path and the `IGNORECASE` on the anchored pattern are the
    same guarantee, so pin both rather than trusting one."""
    for text in ("USAGE LIMIT REACHED", "Usage Limit Reached", "usage limit reached"):
        assert looks_like_usage_limit(text) is True, text
    assert parse_reset_epoch("CLAUDE AI USAGE LIMIT REACHED|1749924000") == 1749924000


def test_empty_body_is_not_a_usage_limit():
    assert looks_like_usage_limit("") is False


def test_reset_epoch_parses_only_the_unambiguous_form():
    """The `|<epoch>` spelling is the only machine-readable one. The prose
    forms are year-less and tz-ambiguous — parsing them would put a wrong
    time in the operator's DM, which is worse than no time."""
    assert parse_reset_epoch("Claude AI usage limit reached|1749924000") == 1749924000
    for prose in CLAUDE_LIMIT_STRINGS:
        if "|" in prose:
            continue
        assert parse_reset_epoch(prose) is None, prose
    assert parse_reset_epoch("") is None


def test_short_lived_rate_limit_is_not_drained():
    """`drained` means the plan's budget is spent and holding is correct.
    A transient 429 must stay retry-able — sweeping it into drained would
    park an agent that would have recovered on the next kick.

    The discriminator is NOT "does it mention a reset time": it's which
    limit is named. "rate limit reached" carries no `usage` / `weekly` /
    `N-hour` qualifier, so no drained marker matches it.
    """
    for text in (
        "API Error: rate limit reached — please retry.",
        "Error: 429 rate_limit_error — too many requests.",
        "API Error: Request rejected (429)",
    ):
        assert looks_like_usage_limit(text) is False, text
        assert _classify_api_error(text)[1] is False, text


def test_fable5_model_limit_is_not_quota_classified():
    """Out-of-scope boundary: LiteLLM per-model limits, not plan budgets."""
    leak = "You've reached your Fable 5 limit. Try again later."
    assert looks_like_usage_limit(leak) is False
    assert _classify_api_error(leak) == (False, False, "rate-limited")


# ── Ordering at the provider-failure classifier ────────────────────
#
# On 2.0.0 a spent quota surfaces through the driver's JSON-RPC / stream
# error path, so `classify_provider_failure` is the load-bearing site —
# the one that decides `quota_exhausted` vs `authentication`.


def test_provider_classifier_puts_quota_before_auth_substrings():
    """The JYP repro at the 2.0.0 site: a quota body carrying
    auth-adjacent wording must classify quota, not authentication."""
    assert classify_provider_failure(
        status=None, diagnostic=QUOTA_WITH_AUTH_WORDING,
    ) == "quota_exhausted"


def test_provider_classifier_keeps_hard_401_as_authentication():
    """The one case auth must still win: an explicit 401 is a transport
    fact, not a substring guess, so it outranks quota wording."""
    assert classify_provider_failure(
        status=401, diagnostic=QUOTA_WITH_AUTH_WORDING,
    ) == "authentication"
    # ...including when the status is only readable out of the diagnostic.
    assert classify_provider_failure(
        status=None, diagnostic="http 401: usage limit reached",
    ) == "authentication"


def test_provider_classifier_still_reports_a_genuine_auth_failure():
    """Non-regression: a real revoked token with no quota wording must
    still reach `authentication` now that a check sits in front of it."""
    assert classify_provider_failure(
        status=None, diagnostic="OAuth token has expired; please sign in again",
    ) == "authentication"


@pytest.mark.parametrize(
    "diagnostic",
    [
        "your credit balance is too low to run this request",
        "billing_error: payment required",
        "insufficient_quota",
        "quota exceeded for this project",
        "you have reached your monthly limit",
        "you have hit your usage limit",
    ],
)
def test_provider_classifier_recognises_every_quota_shape(diagnostic):
    assert classify_provider_failure(
        status=None, diagnostic=diagnostic,
    ) == "quota_exhausted"


def test_provider_classifier_rate_limit_is_not_quota():
    """A 429 holds its own class — collapsing it into quota would park an
    agent that a retry would have cleared."""
    assert classify_provider_failure(
        status=429, diagnostic="too many requests",
    ) == "rate_limit"


# ── Ordering at the adapter-output classifier ──────────────────────


def test_core_classifies_quota_before_auth():
    is_auth, is_drained, label = _classify_api_error(QUOTA_WITH_AUTH_WORDING)
    assert (is_auth, is_drained, label) == (False, True, "quota-drained")


def test_core_still_classifies_a_pure_auth_body_as_auth():
    assert _classify_api_error(
        "API Error: OAuth token has expired",
    ) == (True, False, "auth-failed")


def test_core_tags_is_drained_on_the_global_inbox_retry_path(tmp_path):
    """The global-inbox resume path re-hits the same spent quota; its
    raise must carry is_drained too, or the runtime's no-retry gate and
    outcome split never see the flag on resumed turns."""
    from puffo_agent.agent.adapters import Adapter, TurnResult
    from puffo_agent.agent.core import PuffoAgent

    class _StubAdapter(Adapter):
        async def run_turn(self, ctx):  # pragma: no cover — retry path only
            return TurnResult(reply="", metadata={})

        async def run_retry_turn(self, kick_text, provider_input, ctx):
            return TurnResult(reply=QUOTA_WITH_AUTH_WORDING, metadata={})

    agent = PuffoAgent(
        adapter=_StubAdapter(),
        system_prompt="t",
        memory_dir=str(tmp_path),
    )
    planned = type("Planned", (), {"provider_input": "input"})()
    with pytest.raises(AgentAPIError) as err:
        asyncio.run(agent.handle_global_inbox_retry(planned))
    assert err.value.is_drained is True
    assert err.value.is_auth is False


# ── Ordering at the credential refresher ───────────────────────────


def test_refresher_does_not_call_a_spent_quota_an_auth_failure():
    """The other half of the JYP surface: the credential refresher's own
    probe hits the same limit and flips EVERY agent on the host."""
    outcome = _classify_failed_refresh(
        QUOTA_WITH_AUTH_WORDING, "", rc=1, elapsed=0.5, log_prefix="test",
    )
    # By value, not identity: test_credential_refresher reloads the module,
    # so a second RefreshOutcome class can exist in the same run and `is`
    # compares two enums that are equal in every way that matters.
    assert outcome.value == "quota_exhausted"


def test_refresher_reads_the_quota_off_stderr_too():
    outcome = _classify_failed_refresh(
        "", QUOTA_WITH_AUTH_WORDING, rc=1, elapsed=0.5, log_prefix="test",
    )
    assert outcome.value == "quota_exhausted"


def test_quota_ticks_do_not_feed_the_refresh_broken_streak():
    """A long drained window means many consecutive quota probes; if they
    counted as non-success ticks, every idle agent on the host would flip
    ``refresh_broken`` with copy that misdiagnoses a spent quota."""
    from puffo_agent.portal.credential_refresh import (
        CredentialRefresher,
        RefreshOutcome,
    )

    refresher = CredentialRefresher.__new__(CredentialRefresher)
    refresher._consecutive_non_success = 1  # one genuine failure already
    for _ in range(3):
        refresher._propagate_outcome(RefreshOutcome.QUOTA_EXHAUSTED)
    # Streak untouched: not incremented past the flip threshold, and not
    # reset either — quota says nothing about whether refresh works.
    assert refresher._consecutive_non_success == 1


def test_refresher_still_reports_a_genuine_auth_failure():
    """Non-regression: revoked token still reaches AUTH_FAILED behind the
    new quota check."""
    outcome = _classify_failed_refresh(
        "OAuth token revoked: authentication failed", "",
        rc=1, elapsed=0.5, log_prefix="test",
    )
    assert outcome.value == "auth_failed"


def test_auth_failed_flip_skips_a_drained_agent(tmp_path, monkeypatch):
    """The refresher's host-wide auth flip is the second half of the JYP
    mis-surface: it must not clobber a drained agent with `auth_failed`
    plus re-login copy, while still flipping genuinely stale agents."""
    from puffo_agent.portal.credential_refresh import CredentialRefresher

    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    RuntimeState(status="running", health="drained", error="spent").save("a-drained")
    RuntimeState(status="running", health="ok").save("a-ok")

    refresher = CredentialRefresher.__new__(CredentialRefresher)
    refresher._agent_homes = [
        str(tmp_path / "agents" / "a-drained"),
        str(tmp_path / "agents" / "a-ok"),
    ]
    refresher._flip_auth_failed()

    drained = RuntimeState.load("a-drained")
    assert drained.health == "drained"
    assert drained.error == "spent"
    assert RuntimeState.load("a-ok").health == "auth_failed"


# ── Outcome dispatch ───────────────────────────────────────────────


def test_provider_quota_failure_routes_to_drained():
    """`quota_exhausted` is neither retryable nor is_auth, so it arrives
    as ProviderFailureError — the 2.0.0 shape of the JYP body."""
    exc = ProviderFailureError("usage limit", error_code="quota_exhausted")
    assert failure_outcome(exc) == "drained"


def test_other_provider_failures_do_not_route_to_drained():
    assert failure_outcome(
        ProviderFailureError("nope", error_code="permission_denied"),
    ) == "provider_failed"


def test_api_error_routes_drained_before_auth():
    """Both flags set (a classifier regression upstream) still routes to
    drained — the safe side of the split."""
    assert failure_outcome(
        AgentAPIError("usage limit reached", is_drained=True),
    ) == "drained"
    assert failure_outcome(
        AgentAPIError("usage limit reached", is_auth=True, is_drained=True),
    ) == "drained"


def test_api_error_auth_and_plain_outcomes_are_unchanged():
    assert failure_outcome(
        AgentAPIError("OAuth token has expired", is_auth=True),
    ) == "auth_failed"
    assert failure_outcome(AgentAPIError("API Error: 500")) == "api_error_abandoned"
    assert failure_outcome(RuntimeError("boom")) == "failed"


def test_crash_resume_terminal_returns_drained_for_both_exception_shapes():
    """API-error drained gets the fixed text; provider-error drained keeps
    the body, which carries the reset epoch."""
    assert crash_resume_terminal(
        AgentAPIError("usage limit reached", is_drained=True),
    ) == ("crash resume quota exhausted", "drained")
    assert crash_resume_terminal(
        ProviderFailureError("usage limit reached|1780000000",
                             error_code="quota_exhausted"),
    ) == ("usage limit reached|1780000000", "drained")


def test_crash_resume_terminal_keeps_the_pre_drained_outcomes():
    assert crash_resume_terminal(
        ProviderFailureError("nope", error_code="permission_denied"),
    ) == ("nope", "provider_failed")
    assert crash_resume_terminal(
        AgentAPIError("OAuth token has expired", is_auth=True),
    ) == ("crash resume auth failure", "auth_failed")
    assert crash_resume_terminal(RuntimeError("boom")) == (
        "crash resume unsafe failure: RuntimeError",
        "degraded",
    )


def test_crash_resume_terminal_defers_a_retryable_api_error():
    # None: caller owns the retry budget
    assert crash_resume_terminal(AgentAPIError("API Error: 500")) is None


def test_recovery_retries_stop_at_a_drained_terminal():
    """Crash resume must not spend its retry budget re-hitting a spent
    quota: the first drained failure ends the loop as terminal instead
    of burning max_api_retries against the same wall."""
    runtime = GlobalInboxRuntime.__new__(GlobalInboxRuntime)
    runtime.active = type("Active", (), {"provider_turn_id": None})()
    runtime.adapter = type(
        "Adapter", (), {"register_admission_callback": lambda self, cb, key: None},
    )()
    attempts = []

    async def _run_retry(_planned):
        attempts.append(1)
        raise AgentAPIError("usage limit reached", is_drained=True)

    runtime._run_retry = _run_retry
    runtime.max_api_retries = 3

    async def _sleep(_delay):  # pragma: no cover — drained never sleeps
        pass

    runtime.retry_sleep = _sleep
    planned = type("Planned", (), {"planning_cycle_key": "cycle"})()

    result = asyncio.run(runtime._run_recovery_retries(planned))

    assert result == ("crash resume quota exhausted", "drained")
    assert len(attempts) == 1


def test_settle_routes_drained_into_enter_drained_with_the_epoch():
    """The runtime's process-outcome callback must land a drained turn on
    the worker's drained ENTER with the parsed reset epoch — dropping the
    route would leave health stuck in_progress with no DM."""
    entered = []

    class _EnterRecorder:
        def _enter_drained(self, agent_id, resets_at=None):
            entered.append((agent_id, resets_at))

    StandardWorkerRun._settle_process_health(
        _EnterRecorder(), "agent-1", "drained",
        "Claude AI usage limit reached|1780000000",
    )
    # A drained outcome with no error text still routes; the DM just
    # loses its reset time.
    StandardWorkerRun._settle_process_health(
        _EnterRecorder(), "agent-1", "drained", None,
    )
    assert entered == [("agent-1", 1780000000), ("agent-1", None)]


def test_settle_routes_every_other_outcome_to_its_health(tmp_path, monkeypatch):
    """The outcome → health dispatch is registry-style bookkeeping that
    can silently drift: a renamed outcome string would fall through to
    ``unhandled_error`` and misreport every turn after it."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))

    routed = []

    class _Worker:
        def __init__(self):
            self.runtime = RuntimeState(status="running", health="in_progress")

        def _resolve_health_after_success(self, agent_id):
            routed.append("succeeded")

        def _enter_auth_failed(self, agent_id):
            routed.append("auth_failed")

    w = _Worker()
    StandardWorkerRun._settle_process_health(w, "agent-1", "succeeded", None)
    StandardWorkerRun._settle_process_health(w, "agent-1", "auth_failed", "401")
    assert routed == ["succeeded", "auth_failed"]

    for outcome, health in (
        ("cancelled", "ok"),
        ("api_error_abandoned", "api_error_abandoned"),
        ("provider_failed", "provider_error"),
        ("something_new", "unhandled_error"),
    ):
        w = _Worker()
        StandardWorkerRun._settle_process_health(w, "agent-1", outcome, "err")
        assert w.runtime.health == health, outcome


def test_settle_swallows_a_raising_route(tmp_path, monkeypatch):
    """Health settlement runs inside the runtime's turn loop; a raise
    here would take down the loop over a status write."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))

    class _Worker:
        runtime = None  # forces an AttributeError inside the route

        def _enter_auth_failed(self, agent_id):
            raise RuntimeError("status backend down")

    StandardWorkerRun._settle_process_health(
        _Worker(), "agent-1", "auth_failed", "401"
    )  # must not raise


# ── Retry boundary: drained holds, it does not retry ───────────────


def test_local_warm_holds_on_a_drained_api_error():
    """Vase's approved shape: hold, don't retry. Retrying just re-hits
    the same wall."""
    assert StandardWorkerRun._retryable_local_warm_error(
        AgentAPIError("usage limit reached", is_drained=True),
    ) is False


def test_local_warm_holds_on_a_quota_provider_failure():
    """The 2.0.0 arrival shape — quota reaches warm as
    ProviderFailureError, not AgentAPIError."""
    assert StandardWorkerRun._retryable_local_warm_error(
        ProviderFailureError("usage limit", error_code="quota_exhausted"),
    ) is False


def test_local_warm_still_retries_a_plain_rate_limit():
    """Non-regression: adding the drained short-circuit must not turn
    ordinary transient 429s into no-retry."""
    assert StandardWorkerRun._retryable_local_warm_error(
        AgentAPIError("still rate-limited"),
    ) is True


def test_local_warm_still_retries_a_non_quota_provider_failure():
    assert StandardWorkerRun._retryable_local_warm_error(
        ProviderFailureError("upstream down", error_code="provider_unavailable"),
    ) is True


def test_local_warm_still_holds_on_auth_and_on_programming_errors():
    assert StandardWorkerRun._retryable_local_warm_error(
        AgentAPIError("OAuth token has expired", is_auth=True),
    ) is False
    assert StandardWorkerRun._retryable_local_warm_error(ValueError("bad config")) is False


# ── Worker transition + DM dedup ───────────────────────────────────


class _StubLoop:
    """Stand-in for asyncio.create_task that records the call but doesn't
    schedule, so the dedup gate is observable without an event loop."""

    def __init__(self):
        self.calls = 0

    def create_task(self, coro):
        self.calls += 1
        coro.close()
        return None


def _stub_create_task(monkeypatch):
    """Neuter the DM scheduling so state transitions can be observed
    without an event loop; returns the call recorder."""
    from puffo_agent.portal import worker as worker_module

    stub = _StubLoop()
    monkeypatch.setattr(worker_module.asyncio, "create_task", stub.create_task)
    return stub


def _drained_worker(agent_id="agent-x", *, harness="claude-code", client=None):
    from puffo_agent.portal import worker as worker_module

    w = worker_module.Worker.__new__(worker_module.Worker)
    w.runtime = RuntimeState(status="running")
    w.agent_cfg = type(
        "A", (), {
            "id": agent_id,
            "display_name": "Testy",
            "runtime": type("R", (), {"harness": harness})(),
        },
    )()
    w._client = client
    w._drained_notification_sent = False
    w._drained_resets_at = None
    return w


def test_enter_drained_flips_health_and_says_it_is_not_a_login_problem(
    tmp_path, monkeypatch,
):
    """The JYP repro. If the operator-facing error still points at a
    login command, they are being told to re-login over a spent quota."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    _stub_create_task(monkeypatch)
    w = _drained_worker("agent-jyp")
    w._enter_drained("agent-jyp", 1749924000)

    assert w.runtime.health == "drained"
    assert w.runtime.error == DRAINED_RUNTIME_ERROR
    assert "claude auth login" not in w.runtime.error
    assert "not a sign-in problem" in w.runtime.error.lower()
    # Persisted, not just in memory — the UI reads it back off disk.
    assert RuntimeState.load("agent-jyp").health == "drained"
    assert w._drained_resets_at == 1749924000


def test_enter_drained_without_an_epoch_leaves_the_reset_unset(
    tmp_path, monkeypatch,
):
    """Detection and time-parsing fail independently: a prose-only limit
    body must still flip drained, just with no reset time for the DM."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    _stub_create_task(monkeypatch)
    w = _drained_worker("agent-no-epoch")
    w._enter_drained("agent-no-epoch", None)
    assert w.runtime.health == "drained"
    assert w._drained_resets_at is None


def test_enter_drained_notifies_once_per_episode(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    stub = _stub_create_task(monkeypatch)
    w = _drained_worker("agent-dedup")
    for _ in range(3):
        w._enter_drained("agent-dedup")
    # Only the was-ok → drained edge notifies; re-entries stay quiet.
    assert stub.calls == 1
    assert w._drained_notification_sent is True


def test_snapshot_clearing_health_does_not_buy_a_second_dm(tmp_path, monkeypatch):
    """The snapshot poller clears ``runtime.health`` without touching the
    worker's flag, so the was-ok edge alone cannot gate the DM — only a
    successful turn re-arms it."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    stub = _stub_create_task(monkeypatch)
    w = _drained_worker("agent-resnap")
    w._enter_drained("agent-resnap")
    assert stub.calls == 1
    w.runtime.health = "ok"  # snapshot clear: flag untouched
    w._enter_drained("agent-resnap")
    assert stub.calls == 1


def test_scheduling_failure_re_arms_the_notification(tmp_path, monkeypatch):
    """A DM that never got scheduled must not consume the episode's one
    shot, or the operator hears nothing at all."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    from puffo_agent.portal import worker as worker_module

    def boom(coro):
        coro.close()
        raise RuntimeError("no running loop")

    monkeypatch.setattr(worker_module.asyncio, "create_task", boom)
    w = _drained_worker("agent-cb-raises")
    w._enter_drained("agent-cb-raises")
    assert w.runtime.health == "drained"
    assert w._drained_notification_sent is False


class _StubClient:
    def __init__(self, operator_slug="op-1234", raises=False):
        self.operator_slug = operator_slug
        self.raises = raises
        self.sent: list[tuple[str, str]] = []

    async def _send_dm(self, slug, text, root_id=""):
        if self.raises:
            raise RuntimeError("transport down")
        self.sent.append((slug, text))


def test_drained_dm_goes_to_the_operator_with_the_reset_time():
    client = _StubClient()
    w = _drained_worker(client=client)
    w._drained_resets_at = 1760000000
    asyncio.run(w._notify_operator_of_drained())

    assert len(client.sent) == 1
    slug, text = client.sent[0]
    assert slug == "op-1234"
    assert "It resets around" in text
    assert "auth login" not in text.lower()


def _stub_reset_probe(monkeypatch, result=None):
    """Patch the /usage reset-time probe (a subprocess in production)."""
    async def _probe(_harness):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        "puffo_agent.portal.control.usage_snapshot.predicted_reset_epoch",
        _probe,
    )


def test_drained_dm_names_codex_for_a_codex_agent(monkeypatch):
    _stub_reset_probe(monkeypatch)
    client = _StubClient()
    asyncio.run(_drained_worker(harness="codex", client=client)
                ._notify_operator_of_drained())
    assert "Codex usage limit" in client.sent[0][1]
    assert "Claude Code usage limit" not in client.sent[0][1]


def test_drained_dm_probes_for_a_predicted_reset_time(monkeypatch):
    """The error body almost never carries a reset time, so the DM
    predicts one off the /usage budget — losing the probe would ship a
    time-less DM whenever detection came from a turn failure."""
    _stub_reset_probe(monkeypatch, 1760000000)
    client = _StubClient()
    w = _drained_worker(client=client)
    asyncio.run(w._notify_operator_of_drained())
    assert "It resets around" in client.sent[0][1]
    assert w._drained_resets_at == 1760000000


def test_drained_dm_still_sends_when_the_probe_fails(monkeypatch):
    _stub_reset_probe(monkeypatch, RuntimeError("usage CLI unavailable"))
    client = _StubClient()
    w = _drained_worker(client=client)
    asyncio.run(w._notify_operator_of_drained())
    assert len(client.sent) == 1
    assert "It resets around" not in client.sent[0][1]


def test_drained_dm_re_arms_when_the_client_is_not_warm_yet():
    """Assert the side effect, not the end state: a skipped send must
    leave the gate open so the next drained turn can try again."""
    w = _drained_worker(client=None)
    w._drained_notification_sent = True
    asyncio.run(w._notify_operator_of_drained())
    assert w._drained_notification_sent is False


def test_drained_dm_stays_gated_when_there_is_no_operator():
    """No operator_slug is permanent for the session; re-arming would
    respin the whole path on every drained turn for nothing."""
    w = _drained_worker(client=_StubClient(operator_slug=""))
    w._drained_notification_sent = True
    asyncio.run(w._notify_operator_of_drained())
    assert w._drained_notification_sent is True


def test_drained_dm_re_arms_when_the_send_raises(monkeypatch):
    _stub_reset_probe(monkeypatch)
    client = _StubClient(raises=True)
    w = _drained_worker(client=client)
    w._drained_notification_sent = True
    asyncio.run(w._notify_operator_of_drained())
    assert client.sent == []
    assert w._drained_notification_sent is False


# ── Clear ──────────────────────────────────────────────────────────


def test_successful_turn_clears_drained(tmp_path, monkeypatch):
    """A turn only completes once the window actually refilled, so
    success IS the reset signal."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running", health="drained", error="spent")
    Worker._clear_drained(runtime, "agent-clear", logging.getLogger("t"))
    assert runtime.health == "ok"
    assert runtime.error == ""
    assert RuntimeState.load("agent-clear").health == "ok"


def test_clear_leaves_other_red_states_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    runtime = RuntimeState(status="running", health="auth_failed", error="e")
    Worker._clear_drained(runtime, "agent-other-red", logging.getLogger("t"))
    assert runtime.health == "auth_failed"
    assert runtime.error == "e"


def test_success_re_arms_the_drained_notification(tmp_path, monkeypatch):
    """Dedup is per-episode, not per-process: the next time quota runs
    out the operator must be told again."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    w = _drained_worker("agent-rearm")
    w.runtime = RuntimeState(status="running", health="drained", error="spent")
    w._drained_notification_sent = True
    w._drained_resets_at = 1760000000
    w._claude_api_key_mode = False
    w._api_key_auth_recovery_pending = False
    w._auth_failed_notification_sent = False

    w._resolve_health_after_success("agent-rearm")

    assert w.runtime.health == "ok"
    assert w._drained_notification_sent is False
    assert w._drained_resets_at is None


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
    assert drained_harnesses({"claude-code": {"session": {"used_pct": 99}}}) == {}


def test_missing_resets_at_still_reports_drained():
    assert drained_harnesses(
        {"claude-code": {"session": {"used_pct": 100}}},
    ) == {"claude-code": None}


def test_used_pct_accepts_int_and_float():
    """The provider serializes usage as int today; ``>= 100`` on a float
    must behave the same if that changes."""
    for value in (100, 100.0, 100.5):
        snap = {"claude-code": {"session": {"used_pct": value}}}
        assert "claude-code" in drained_harnesses(snap), value
    for value in (99, 99.9):
        snap = {"claude-code": {"session": {"used_pct": value}}}
        assert drained_harnesses(snap) == {}, value


def test_malformed_snapshot_entries_are_ignored():
    assert drained_harnesses({"claude-code": "not-a-dict"}) == {}
    assert drained_harnesses({"claude-code": {"session": None}}) == {}
    assert drained_harnesses({}) == {}


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

    flipped = RuntimeState.load("a-claude")
    assert flipped.health == "drained"
    assert flipped.error == DRAINED_RUNTIME_ERROR
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


def test_snapshot_skips_harnesses_it_did_not_probe(tmp_path, monkeypatch):
    """A partial probe (one harness timed out) must leave the unprobed
    agents alone rather than reading their absence as recovery."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    _stub_agents(monkeypatch, {"a-codex": "codex"})
    RuntimeState(status="running", health="drained", error="spent").save("a-codex")

    apply_drained_health({"claude-code": {"session": {"used_pct": 12}}})

    assert RuntimeState.load("a-codex").health == "drained"


def _stub_live_workers(monkeypatch, mapping):
    from puffo_agent.portal.control import usage_snapshot as us

    monkeypatch.setattr(us, "_live_workers_provider", lambda: mapping)


def test_snapshot_flips_a_live_worker_in_memory(tmp_path, monkeypatch):
    """The worker heartbeat rewrites runtime.json from memory every few
    seconds and the status reporter reads memory, so a disk-only flip
    for a live worker evaporates and never reaches the server UI. The
    flip must go through the worker's own drained ENTER — which also
    carries the snapshot's reset time into the operator DM."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    _stub_agents(monkeypatch, {"a-live": "claude-code"})
    stub = _stub_create_task(monkeypatch)
    w = _drained_worker("a-live")
    _stub_live_workers(monkeypatch, {"a-live": w})

    apply_drained_health({"claude-code": {
        "session": {"used_pct": 100, "resets_at": 1760000000},
    }})

    assert w.runtime.health == "drained"
    assert w._drained_resets_at == 1760000000
    assert stub.calls == 1  # DM went through the worker's episode gate
    assert RuntimeState.load("a-live").health == "drained"


def test_snapshot_clears_a_live_worker_without_rearming_the_dm(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    _stub_agents(monkeypatch, {"a-live": "claude-code"})
    w = _drained_worker("a-live")
    w.runtime.health = "drained"
    w.runtime.error = "spent"
    w._drained_notification_sent = True
    _stub_live_workers(monkeypatch, {"a-live": w})

    apply_drained_health({"claude-code": {"session": {"used_pct": 10}}})

    assert w.runtime.health == "ok"
    # Only a successful turn re-arms; a budget blip must not buy a re-DM.
    assert w._drained_notification_sent is True


def test_snapshot_leaves_a_live_workers_other_reds_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    _stub_agents(monkeypatch, {"a-live": "claude-code"})
    stub = _stub_create_task(monkeypatch)
    w = _drained_worker("a-live")
    w.runtime.health = "auth_failed"
    _stub_live_workers(monkeypatch, {"a-live": w})

    apply_drained_health({"claude-code": {"session": {"used_pct": 100}}})
    assert w.runtime.health == "auth_failed"
    assert stub.calls == 0

    # And a healthy worker on a harness with budget is left untouched.
    w.runtime.health = "ok"
    apply_drained_health({"claude-code": {"session": {"used_pct": 10}}})
    assert w.runtime.health == "ok"


def test_snapshot_live_flip_failure_does_not_block_the_rest(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    _stub_agents(monkeypatch, {
        "a-boom": "claude-code",
        "a-disk": "claude-code",
    })
    _stub_live_workers(monkeypatch, {"a-boom": object()})  # no .runtime
    RuntimeState(status="running", health="ok").save("a-disk")

    apply_drained_health({"claude-code": {"session": {"used_pct": 100}}})

    assert RuntimeState.load("a-disk").health == "drained"


def test_live_worker_registry_defaults_to_empty(monkeypatch):
    """No daemon (tests, CLI one-shots) → the disk path, never a crash."""
    from puffo_agent.portal.control import usage_snapshot as us

    monkeypatch.setattr(us, "_live_workers_provider", None)
    assert us._live_workers() == {}
    us.set_live_workers(lambda: {"a": "worker"})
    assert us._live_workers() == {"a": "worker"}

    def _boom():
        raise RuntimeError("daemon gone")

    us.set_live_workers(_boom)
    assert us._live_workers() == {}


def test_predicted_reset_epoch_reads_the_spent_window(monkeypatch):
    from puffo_agent.portal.control.usage_snapshot import predicted_reset_epoch

    async def _snapshot(_home):
        return {"codex": {"weekly": {"used_pct": 100, "resets_at": 1770000000}}}

    monkeypatch.setattr(
        "puffo_agent.portal.control.usage_snapshot.collect_usage_snapshot",
        _snapshot,
    )
    assert asyncio.run(predicted_reset_epoch("codex")) == 1770000000
    # A harness that still has budget predicts nothing.
    assert asyncio.run(predicted_reset_epoch("claude-code")) is None

    async def _empty(_home):
        return None

    monkeypatch.setattr(
        "puffo_agent.portal.control.usage_snapshot.collect_usage_snapshot",
        _empty,
    )
    assert asyncio.run(predicted_reset_epoch("codex")) is None

    async def _boom(_home):
        raise RuntimeError("no CLI on this host")

    monkeypatch.setattr(
        "puffo_agent.portal.control.usage_snapshot.collect_usage_snapshot",
        _boom,
    )
    assert asyncio.run(predicted_reset_epoch("codex")) is None


def test_snapshot_flip_survives_broken_agents(tmp_path, monkeypatch):
    """One agent with an unreadable, missing, or unwritable runtime.json
    must not block the drained flip for the rest of the host."""
    monkeypatch.setenv("PUFFO_HOME", str(tmp_path))
    _stub_agents(monkeypatch, {
        "a-load-raises": "claude-code",
        "a-no-runtime": "claude-code",
        "a-save-raises": "claude-code",
        "a-good": "claude-code",
    })
    RuntimeState(status="running", health="ok").save("a-save-raises")
    RuntimeState(status="running", health="ok").save("a-good")

    real_load, real_save = RuntimeState.load, RuntimeState.save

    def _load(agent_id):
        if agent_id == "a-load-raises":
            raise OSError("runtime.json unreadable")
        return real_load(agent_id)

    def _save(self, agent_id):
        if agent_id == "a-save-raises":
            raise OSError("disk full")
        return real_save(self, agent_id)

    monkeypatch.setattr(RuntimeState, "load", _load)
    monkeypatch.setattr(RuntimeState, "save", _save)

    apply_drained_health({"claude-code": {"session": {"used_pct": 100}}})

    # The broken agents were skipped/logged; the healthy one still flipped.
    assert RuntimeState.load("a-good").health == "drained"
    assert RuntimeState.load("a-save-raises").health == "ok"  # save failed


def test_usage_post_survives_a_drained_flip_failure(monkeypatch):
    """Local health reporting must never block the usage POST to the
    server — a broken state dir downgrades to a warning, not a lost
    snapshot."""
    from puffo_agent.portal.control import client as control_client

    async def _snapshot(_home):
        return {"claude-code": {"session": {"used_pct": 100}}}

    def _boom(_snapshot):
        raise RuntimeError("state dir unavailable")

    posted = []

    class _Response:
        status = 204

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def post(self, url, data=None, headers=None):
            posted.append(url)
            return _Response()

    monkeypatch.setattr(control_client, "collect_usage_snapshot", _snapshot)
    monkeypatch.setattr(control_client, "apply_drained_health", _boom)
    monkeypatch.setattr(
        control_client.machine_auth, "signed_headers",
        lambda machine, method, path, body: {},
    )
    monkeypatch.setattr(
        control_client, "create_remote_http_session", lambda base: _Session(),
    )
    machine = type("Machine", (), {"machine_id": "m-1"})()

    assert asyncio.run(control_client.post_usage_snapshot(machine, "https://s")) is True
    assert posted == ["https://s/v2/machines/m-1/usage"]


# ── Operator-facing copy ───────────────────────────────────────────


def test_drained_dm_never_tells_the_operator_to_log_in():
    """The entire point of the ticket."""
    low = format_drained("agent-1", "Testy", resets_at=1760000000).lower()
    assert "auth login" not in low
    assert "codex login" not in low
    assert "sign-in has expired" not in low
    assert "not a sign-in problem" in low


def test_drained_dm_offers_the_four_real_recoveries():
    low = format_drained("agent-1", "Testy").lower()
    assert "wait for the window to reset" in low
    assert "smaller model" in low
    assert "add credits" in low
    assert "upgrade the plan" in low


def test_drained_dm_asks_for_a_message_instead_of_promising_resume():
    """Nothing re-kicks a held turn on the reset itself — recovery rides
    on the next inbound message. Promising 'I resume on my own' would
    leave the operator waiting on an agent that never wakes."""
    text = format_drained("agent-1", "Testy")
    assert "send me a message" in text.lower()
    assert "resume on my own" not in text.lower()
    assert "发条消息" in text


def test_drained_dm_is_bilingual():
    text = format_drained("agent-1", "Testy")
    assert "额度" in text and "**Your options:**" in text


def test_drained_dm_includes_reset_time_when_known():
    with_reset = format_drained("agent-1", "Testy", resets_at=1760000000)
    without = format_drained("agent-1", "Testy")
    assert "It resets around" in with_reset
    assert "重置" in with_reset
    # No reset time from the provider → no empty/garbled clause.
    assert "It resets around" not in without
    assert "None" not in without


def test_drained_dm_degrades_on_an_unusable_epoch():
    """Detection and time-parsing fail independently, and an epoch the
    provider drifted on must not cost the operator the whole alert."""
    text = format_drained("agent-x", "Agent X", resets_at=10**18)
    assert "It resets around" not in text
    assert "not a sign-in problem" in text.lower()


def test_drained_dm_renders_without_a_display_name():
    """An empty display name must still produce a sendable DM — losing
    the alert to a formatting hole is worse than an ugly label."""
    msg = format_drained("agent-x", "")
    assert msg.strip()
    assert "`agent-x`" in msg
    assert "****" not in msg
    assert "not a sign-in problem" in msg.lower()
    assert format_codex_drained("agent-x", "").strip()


def test_codex_dm_names_codex_not_claude():
    text = format_codex_drained("agent-1", "Testy")
    assert "Codex usage limit" in text
    assert "Claude Code usage limit" not in text
