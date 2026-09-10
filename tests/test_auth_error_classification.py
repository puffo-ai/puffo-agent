"""Auth errors must be classified as auth (not rate-limit), so the
consumer skips the pointless kick-retries and the worker flips
auth_failed + DMs the operator.

Regression for: a ``401 Invalid authentication credentials`` reply was
seen as a generic ``API Error`` rate-limit, kick-retried 3×, abandoned,
and never DMed.
"""

from __future__ import annotations

import pytest

from puffo_agent.agent._auth_markers import looks_like_auth_error
from puffo_agent.agent.core import AgentAPIError
from puffo_agent.portal.worker import Worker


# ── shared detector ────────────────────────────────────────────────


@pytest.mark.parametrize("reply", [
    "Failed to authenticate. API Error: 401 Invalid authentication credentials",
    "API Error: 401",
    "Invalid API key · Please run /login",
    "invalid_grant",
    "OAuth token revoked",
    "This organization has been disabled",
    "authentication failed",
    "credentials expired",
    '{"type":"authentication_error"}',
])
def test_detector_flags_auth(reply):
    assert looks_like_auth_error(reply) is True


@pytest.mark.parametrize("reply", [
    "API Error: Request rejected (429)",
    "API Error: Server is temporarily limiting requests",
    "I hit a 401 earlier but it cleared up",   # bare 401 in prose → not auth
    "Let me explain how unauthorized access works",
    "",
])
def test_detector_does_not_flag_non_auth(reply):
    assert looks_like_auth_error(reply) is False


def test_agent_api_error_carries_is_auth():
    assert AgentAPIError("x", is_auth=True).is_auth is True
    assert AgentAPIError("y").is_auth is False


@pytest.mark.asyncio
async def test_core_tags_is_auth_on_raise(tmp_path):
    """A turn whose output contains an auth-class `API Error` raises
    AgentAPIError(is_auth=True); a rate-limit one stays is_auth=False."""
    from puffo_agent.agent.adapters import Adapter, TurnContext, TurnResult
    from puffo_agent.agent.core import PuffoAgent

    class _StubAdapter(Adapter):
        def __init__(self, reply):
            self._reply = reply

        async def run_turn(self, ctx: TurnContext) -> TurnResult:
            return TurnResult(reply=self._reply, metadata={})

    async def _raise_for(reply):
        agent = PuffoAgent(
            adapter=_StubAdapter(reply),
            system_prompt="t",
            memory_dir=str(tmp_path),
        )
        try:
            await agent.handle_message(
                channel_id="c", channel_name="g", sender="u",
                sender_email="u@x", text="hi", post_id="p", root_id="",
            )
        except AgentAPIError as exc:
            return exc
        return None

    auth = await _raise_for(
        "Failed to authenticate. API Error: 401 Invalid authentication credentials"
    )
    assert auth is not None and auth.is_auth is True

    rate = await _raise_for("API Error: Request rejected (429)")
    assert rate is not None and rate.is_auth is False

    # Same tagging on the retry path (handle_api_error_retry).
    async def _retry_raise_for(reply):
        agent = PuffoAgent(
            adapter=_StubAdapter(reply),
            system_prompt="t",
            memory_dir=str(tmp_path),
        )
        try:
            await agent.handle_api_error_retry(
                root_id="r",
                channel_meta={"channel_name": "g"},
                fallback_batch=[{"text": "hi"}],
            )
        except AgentAPIError as exc:
            return exc
        return None

    auth_retry = await _retry_raise_for(
        "Failed to authenticate. API Error: 401 Invalid authentication credentials"
    )
    assert auth_retry is not None and auth_retry.is_auth is True


# ── worker: _enter_auth_failed edge ────────────────────────────────


class _RT:
    def __init__(self, health: str):
        self.health = health
        self.error = ""

    def save(self, agent_id: str) -> None:
        pass


def _stub_worker(health: str):
    class _W:
        pass

    w = _W()
    w.runtime = _RT(health)
    w.dm_fired: list[int] = []
    w.refresh_fired: list[int] = []
    # Stub the DM-enter + refresher-kick so the edge logic is exercised
    # without the async DM machinery.
    w._on_auth_failed_enter = lambda: w.dm_fired.append(1)
    w._notify_refresh_needed = lambda: w.refresh_fired.append(1)
    return w


def test_enter_auth_failed_fires_dm_on_edge():
    w = _stub_worker("ok")
    Worker._enter_auth_failed(w, "t-agent")
    assert w.runtime.health == "auth_failed"
    assert w.refresh_fired == [1]   # refresher kicked
    assert w.dm_fired == [1]        # DM fired on the ok→auth_failed edge


def test_enter_auth_failed_no_dm_on_reentry():
    w = _stub_worker("auth_failed")   # already failed
    Worker._enter_auth_failed(w, "t-agent")
    assert w.runtime.health == "auth_failed"
    assert w.refresh_fired == [1]     # still kicks the refresher
    assert w.dm_fired == []           # but no duplicate DM (was_ok=False)


def test_enter_auth_failed_survives_refresh_kick_raising():
    """A throwing notify_refresh_needed must not break the flip/DM."""
    w = _stub_worker("ok")

    def _boom():
        raise RuntimeError("no loop")

    w._notify_refresh_needed = _boom
    Worker._enter_auth_failed(w, "t-agent")   # no crash
    assert w.runtime.health == "auth_failed"
    assert w.dm_fired == [1]


# ── provider diagnostics: Pi / openai-codex rejected credential ────
#
# Observed on a QA agent whose credential was deliberately invalidated
# (2026-09-09).  Pi answered with this exact sentence and the daemon
# classified it `provider_error`: the operator saw "The provider could
# not complete the turn.", `_enter_auth_failed` never fired, so no
# operator DM was sent and the UI gave no hint that a re-login was
# needed.  The turn just requeued behind an exponential backoff.
#
# Pi builds this message with `createErrorMessage()`, which flattens the
# provider error to `error.message` — no code, no HTTP status survives.
# The text really is all this hop gets, so the fix has to live in the
# marker list rather than in a structured field.

PI_REJECTED_CREDENTIAL = (
    "Could not parse your authentication token. Please try signing in again."
)


def test_pi_rejected_credential_text_is_classified_as_authentication():
    from puffo_agent.agent.provider_failures import (
        classify_provider_failure, provider_failure,
    )

    code = classify_provider_failure(
        status=None, diagnostic=PI_REJECTED_CREDENTIAL
    )
    assert code == "authentication"
    # The whole point of the classification: is_auth is what makes the
    # worker flip auth_failed and DM the operator.
    assert provider_failure(code).is_auth is True


def test_pi_message_end_frame_reports_an_auth_failure_code():
    """The real failing hop, not just the matcher underneath it.

    This is the frame Pi actually emitted; classifying the string
    correctly is worthless if this translation still reports
    `provider_error`.
    """
    from puffo_agent.agent.harness.driver import SessionRef, TurnRef
    from puffo_agent.agent.harness.drivers.pi_protocol import (
        normalize_pi_event,
    )

    events = normalize_pi_event(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "api": "openai-codex-responses",
                "provider": "openai-codex",
                "model": "gpt-5.6-terra",
                "usage": {"input": 0, "output": 0},
                "stopReason": "error",
                "errorMessage": PI_REJECTED_CREDENTIAL,
                "timestamp": 1789002329865,
            },
        },
        session_ref=SessionRef("s"),
        turn_ref=TurnRef("t"),
    )
    codes = [
        e.data.get("failure_code") for e in events
        if e.data.get("code") == "assistant_error"
    ]
    assert codes == ["authentication"]


@pytest.mark.parametrize("diagnostic", [
    # Ordinary parse failures must stay generic: an operator told to
    # "sign in again" over a malformed JSON body would chase the wrong
    # problem, and the agent would be parked in auth_failed for a fault
    # that re-logging in cannot fix.
    "Unexpected token < in JSON at position 0",
    "Could not parse the response body",
    "SyntaxError: Unexpected token } in JSON",
    "Failed to parse token stream from provider",
    # NOTE: "Tokenizer error: invalid token sequence in prompt" also belongs
    # here, but it already classifies as `authentication` on unmodified main
    # via the pre-existing "invalid token" marker.  That false positive
    # predates this fix and narrowing that marker is a behaviour change
    # outside this PR's scope, so it is reported separately rather than
    # asserted here.
])
def test_ordinary_parse_failures_are_not_called_auth(diagnostic):
    from puffo_agent.agent.provider_failures import classify_provider_failure

    assert classify_provider_failure(
        status=None, diagnostic=diagnostic
    ) != "authentication"


def test_the_prose_tier_is_not_widened_by_this_fix():
    """`looks_like_auth_error` runs against free-form agent prose, where
    a substring hit is far more likely to be someone *talking* about
    signing in.  The new markers belong to the provider-diagnostic tier
    only; this pins that split so a later edit cannot quietly move them.
    """
    from puffo_agent.agent._auth_markers import (
        looks_like_auth_error, looks_like_provider_auth_error,
    )

    assert looks_like_provider_auth_error(PI_REJECTED_CREDENTIAL) is True
    assert looks_like_auth_error(PI_REJECTED_CREDENTIAL) is False
    # Prose that merely mentions signing in stays clean on both tiers.
    assert looks_like_auth_error(
        "I can walk you through how signing in again works"
    ) is False
