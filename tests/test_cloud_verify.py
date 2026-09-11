"""Readiness checks for a cloud agent.

Each case is a failure that actually happened during the subscription build-out
on 2026-09-10/11, replayed as facts.
"""

from __future__ import annotations

import pytest

from puffo_agent.agent.cloud_verify import CHECKS, evaluate, summarize

HEALTHY = {
    "state": "running",
    "template_id": "jtlgo2xyo4w5kt31wqrx",
    "expected_template": "jtlgo2xyo4w5kt31wqrx",
    "auth_mode": "subscription",
    "expected_auth_mode": "subscription",
    "api_key": "",
    "child_has_token": True,
    "child_has_base_url": False,
    "child_has_api_key": False,
    "conns_anthropic": 24,
    "conns_gateway": 0,
    "profile_bytes": 12797,
    "profile_has_soul": True,
    "memory_files": 3,
    "log_errors": {},
    "conns_relay": 2,
    "bad_frame_count": 31,
}


def by_name(results, name):
    return next(r for r in results if r.name == name)


def test_a_healthy_agent_passes_everything():
    passed, failed, unknown = summarize(evaluate(HEALTHY))
    assert (failed, unknown) == (0, 0)
    assert passed == len(CHECKS)


def test_known_bad_frame_noise_does_not_fail_the_bridge_check():
    """31 BAD_FRAMEs were logged while the agent completed 68 turns. It is
    `list_invites` noise from a server that never implemented the frame."""
    r = by_name(evaluate(HEALTHY), "bridge connected")
    assert r.ok is True
    assert "BAD_FRAME" in r.detail


class TestTheFailuresThatHappened:
    def test_auth_mode_stuck_on_gateway(self):
        """Two whole builds came up `api-gateway` because the provisioner never
        wrote the field — the single worst bug of the build-out."""
        r = by_name(evaluate({**HEALTHY, "auth_mode": "api-gateway"}), "auth_mode matches")
        assert r.ok is False
        assert "api-gateway" in r.detail

    def test_virtual_key_minted_for_a_subscription_agent(self):
        r = by_name(evaluate({**HEALTHY, "api_key": "sk-abc"}), "no virtual key")
        assert r.ok is False

    def test_leftover_base_url_outranks_the_plan_token(self):
        """ANTHROPIC_BASE_URL beats CLAUDE_CODE_OAUTH_TOKEN in the CLI's own
        precedence, so this bills the gateway while looking correct."""
        facts = {**HEALTHY, "child_has_base_url": True, "child_has_api_key": True}
        assert by_name(evaluate(facts), "credential reaches the CLI").ok is False

    def test_traffic_contradicts_the_configured_mode(self):
        facts = {**HEALTHY, "conns_anthropic": 0, "conns_gateway": 12}
        assert by_name(evaluate(facts), "talks to the right upstream").ok is False

    def test_created_but_never_seeded(self):
        """A 71-byte stub profile — exactly what the Hub writes."""
        r = by_name(evaluate({**HEALTHY, "profile_bytes": 71}), "profile is seeded")
        assert r.ok is False
        assert "stub" in r.detail

    def test_empty_memory(self):
        assert by_name(evaluate({**HEALTHY, "memory_files": 0}), "memory present").ok is False

    def test_credential_refresh_loop(self):
        """The daemon hunting for an operator credential view inside a sandbox."""
        facts = {**HEALTHY, "log_errors": {"credential view-sync incomplete": 7, "CLI exited": 2}}
        r = by_name(evaluate(facts), "no startup errors")
        assert r.ok is False
        assert "credential view-sync" in r.detail

    def test_wrong_template_booted(self):
        facts = {**HEALTHY, "template_id": "im2xq2mnuyalko3f7a44"}
        assert by_name(evaluate(facts), "expected template").ok is False


class TestGatewayAgentsAreJudgedByTheirOwnRules:
    GW = {
        **HEALTHY,
        "auth_mode": "api-gateway",
        "expected_auth_mode": "api-gateway",
        "api_key": "sk-vk-123",
        "child_has_token": False,
        "child_has_api_key": True,
        "conns_anthropic": 0,
        "conns_gateway": 8,
    }

    def test_a_healthy_gateway_agent_passes(self):
        _, failed, unknown = summarize(evaluate(self.GW))
        assert (failed, unknown) == (0, 0)

    def test_a_key_is_correct_for_a_gateway_agent(self):
        assert by_name(evaluate(self.GW), "no virtual key").ok is True


class TestUnknownIsNotPass:
    """"I could not look" must never read as "it is fine" — that turns a broken
    collector into a clean bill of health."""

    def test_missing_facts_report_undetermined(self):
        results = evaluate({})
        assert summarize(results)[1] == 0          # nothing claimed as failed
        assert summarize(results)[2] >= 8          # most undetermined
        assert all(r.ok is not True or r.name == "no virtual key" for r in results)

    def test_a_check_that_raises_is_undetermined_not_passed(self):
        class Exploding(dict):
            def get(self, *a, **k):
                raise RuntimeError("boom")

        assert all(r.ok is None for r in evaluate(Exploding()))


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.name)
def test_every_check_explains_what_it_catches(check):
    """A red box that does not teach the failure mode is a worse tool."""
    assert check.catches and len(check.catches) > 15


class TestTheChecksThatUsedToLieWhenFactsWereMissing:
    """The module's own rule: "I could not look" must never read as "it is fine".
    Two checks broke it by treating an UNKNOWN auth_mode as "not subscription"
    — and an unread agent.yml is exactly the state where auth_mode went
    silently absent for two whole builds."""

    def test_unknown_mode_makes_the_key_check_unanswerable(self):
        facts = {k: v for k, v in HEALTHY.items() if k != "auth_mode"}
        assert by_name(evaluate(facts), "no virtual key").ok is None

    def test_unknown_mode_makes_the_credential_check_unanswerable(self):
        facts = {k: v for k, v in HEALTHY.items() if k != "auth_mode"}
        assert by_name(evaluate(facts), "credential reaches the CLI").ok is None

    def test_unknown_mode_makes_the_upstream_check_unanswerable(self):
        facts = {k: v for k, v in HEALTHY.items() if k != "auth_mode"}
        assert by_name(evaluate(facts), "talks to the right upstream").ok is None


class TestTheBridgeCheckJudgesTheRelayNotAnySocket:
    """Counting every ESTAB reported "connected" for an agent with a live LLM
    connection and a dead bridge — the failure this check exists to catch."""

    def test_llm_sockets_alone_do_not_count_as_a_live_bridge(self):
        facts = {**HEALTHY, "conns_relay": 0, "conns_anthropic": 24}
        r = by_name(evaluate(facts), "bridge connected")
        assert r.ok is False
        assert "no relay socket" in r.detail

    def test_an_unresolvable_relay_is_undetermined_not_failed(self):
        assert by_name(evaluate({**HEALTHY, "conns_relay": -1}), "bridge connected").ok is None


def test_an_unresolvable_upstream_is_undetermined_not_a_verdict():
    """-1 is the probe saying the host would not resolve, not "zero sockets"."""
    facts = {**HEALTHY, "conns_anthropic": -1}
    assert by_name(evaluate(facts), "talks to the right upstream").ok is None


class TestAnIdleAgentIsNotAFailedAgent:
    """A live `--verify` run failed a healthy agent that had simply been
    resumed and not yet spoken: it held no upstream socket at all, and the
    check read "no anthropic traffic" as "wrong upstream". Absence of traffic
    is absence of evidence — this check can only convict traffic it can see."""

    def test_no_sockets_at_all_is_undetermined_on_a_subscription(self):
        facts = {**HEALTHY, "conns_anthropic": 0, "conns_gateway": 0}
        r = by_name(evaluate(facts), "talks to the right upstream")
        assert r.ok is None
        assert "idle" in r.detail

    def test_no_sockets_at_all_is_undetermined_on_the_gateway(self):
        facts = {**HEALTHY, "auth_mode": "api-gateway", "expected_auth_mode": "api-gateway",
                 "api_key": "<present>", "child_has_base_url": True, "child_has_api_key": True,
                 "child_has_token": False, "conns_anthropic": 0, "conns_gateway": 0}
        assert by_name(evaluate(facts), "talks to the right upstream").ok is None

    def test_a_subscription_agent_reaching_the_gateway_still_fails(self):
        """The verdict this check exists for survives the idle carve-out."""
        facts = {**HEALTHY, "conns_anthropic": 24, "conns_gateway": 3}
        assert by_name(evaluate(facts), "talks to the right upstream").ok is False

    def test_traffic_to_anthropic_alone_still_passes(self):
        assert by_name(evaluate(HEALTHY), "talks to the right upstream").ok is True
