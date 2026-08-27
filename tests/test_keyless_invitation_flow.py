"""Focused contract tests for the keyless invitation decision flow."""

from __future__ import annotations

import logging

import pytest

from puffo_agent.agent.keyless_invitation_flow import KeylessInvitationFlow
from puffo_agent.agent.keyless_invitation_state import (
    load_keyless_invitations,
    save_keyless_invitations,
)
from _portal_support import isolated_home

log = logging.getLogger("keyless-invitation-test")


class _Dms:
    """Fake keyless DM prompt lane. Snapshots the durable state file at the
    moment a prompt is about to be sent so ordering is observable."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.send_calls: list[dict] = []
        self.file_at_send: list[dict] = []
        self.send_error: Exception | None = None

    async def send_dm(self, text: str, *, client_ref: str) -> dict:
        self.file_at_send.append(dict(load_keyless_invitations(self.slug)))
        if self.send_error is not None:
            raise self.send_error
        self.send_calls.append({"text": text, "client_ref": client_ref})
        return {
            "type": "ack",
            "client_ref": client_ref,
            "envelope_id": f"env_{client_ref}",
            "thread_root_id": f"thread_{client_ref}",
        }


class _Bridge:
    """Fake Server bridge decision lane. Snapshots the durable state file
    at the moment a decision frame is about to be sent."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.decision_calls: list[dict] = []
        self.file_at_decide: list[dict] = []
        self.decide_error: Exception | None = None
        self.decide_outcomes: dict[str, str] = {}

    async def send_decide_invitation(self, **kwargs) -> dict:
        self.file_at_decide.append(dict(load_keyless_invitations(self.slug)))
        if self.decide_error is not None:
            raise self.decide_error
        self.decision_calls.append(kwargs)
        event_id = kwargs["invitation_event_id"]
        outcome = self.decide_outcomes.get(event_id, "applied")
        return {
            "type": "decide_invitation_result",
            "client_ref": kwargs["client_ref"],
            "invitation_event_id": event_id,
            "decision": kwargs["decision"],
            "outcome": outcome,
        }


def _flow(
    slug: str,
    *,
    operator: str = "operator-1",
    auto_accept: bool = False,
    bridge: _Bridge | None = None,
    dms: _Dms | None = None,
) -> tuple[KeylessInvitationFlow, _Bridge, _Dms]:
    bridge = bridge or _Bridge(slug)
    dms = dms or _Dms(slug)
    flow = KeylessInvitationFlow(
        slug=slug,
        bridge=bridge,
        operator_slug=operator,
        send_dm=dms.send_dm,
        auto_accept_space_invitations=auto_accept,
        log=log,
    )
    return flow, bridge, dms


def _invite(event_id: str, *, scope: str, space_id: str, inviter: str) -> dict:
    invite: dict = {
        "invitation_event_id": event_id,
        "scope": scope,
        "space_id": space_id,
        "inviter_slug": inviter,
    }
    if scope == "channel":
        invite["channel_id"] = f"ch_{event_id}"
    return invite


@pytest.mark.asyncio
async def test_accept_lifecycle_is_durable_resumable_and_terminal():
    """Guarded regression: no prompt or Server decision may be sent before
    its state transition is durable; reconnect/replay reuses the stable
    prompt and decision client refs instead of a second logical prompt or
    decision; applied/already_applied leave the record terminal with no
    further send on another resume; an operator-sent invite auto-accepts
    without prompting."""
    isolated_home()
    slug = "agent-1"
    flow, bridge, dms = _flow(slug)
    iv_1 = _invite("iv_1", scope="space", space_id="sp_1", inviter="alice-1")
    iv_2 = _invite("iv_2", scope="channel", space_id="sp_1", inviter="bob-1")
    iv_3 = _invite("iv_3", scope="space", space_id="sp_3", inviter="carol-1")
    full = [iv_1, iv_2, iv_3]

    await flow.reconcile([iv_1, iv_2])
    assert len(dms.send_calls) == 2
    # The prompt is sent only after the record was durable (unsent at send).
    assert dms.file_at_send[0]["iv_1"]["phase"] == "unsent"
    state = load_keyless_invitations(slug)
    assert state["iv_1"]["phase"] == "awaiting_reply"
    ref_1 = state["iv_1"]["prompt_client_ref"]
    ref_2 = state["iv_2"]["prompt_client_ref"]

    # A prompt that fails leaves the record durable-unsent for resume; a
    # re-reconcile of the full authoritative set never prompts a known invite.
    dms.send_error = OSError("network down")
    await flow.reconcile(full)
    dms.send_error = None
    assert load_keyless_invitations(slug)["iv_3"]["phase"] == "unsent"
    ref_3 = load_keyless_invitations(slug)["iv_3"]["prompt_client_ref"]
    await flow.reconcile(full)
    assert len(dms.send_calls) == 2

    # Operator-sent invites auto-accept without prompting the operator.
    await flow.reconcile([*full, _invite("iv_op", scope="channel", space_id="sp_op", inviter="operator-1")])
    assert len(dms.send_calls) == 2
    assert load_keyless_invitations(slug)["iv_op"]["phase"] == "terminal"

    # Reboot + resume: known prompts are left awaiting their exact reply,
    # and the unsent prompt is re-sent with the SAME stable ref.
    reboot, bridge_r, dms_r = _flow(slug)
    await reboot.resume()
    assert len(dms_r.send_calls) == 1
    assert dms_r.send_calls[0]["client_ref"] == ref_3
    assert bridge_r.decision_calls == []

    # Only an exact threaded y/yes/n/no reply is consumed.
    assert await reboot.handle_operator_reply(thread_root_id=f"thread_{ref_1}", text="maybe") is False
    assert await reboot.handle_operator_reply(thread_root_id="unknown-thread", text="y") is False

    # Operator accepts iv_1; the decision (and its ref) is durable before
    # the decision frame; a dropped decision send keeps it decided.
    bridge_r.decide_error = RuntimeError("connection dropped")
    assert await reboot.handle_operator_reply(thread_root_id=f"thread_{ref_1}", text="  YES ") is True
    assert bridge_r.decision_calls == []
    assert bridge_r.file_at_decide[0]["iv_1"]["phase"] == "decided"
    decision_ref_1 = load_keyless_invitations(slug)["iv_1"]["decision_client_ref"]
    assert bridge_r.file_at_decide[0]["iv_1"]["decision_client_ref"] == decision_ref_1

    # Reboot + resume retries the decided record with the SAME decision ref
    # and reaches terminal applied with no duplicate prompt.
    replay, bridge_r2, dms_r2 = _flow(slug)
    await replay.resume()
    assert len(bridge_r2.decision_calls) == 1
    assert bridge_r2.decision_calls[0]["client_ref"] == decision_ref_1
    assert dms_r2.send_calls == []
    assert load_keyless_invitations(slug)["iv_1"]["phase"] == "terminal"

    # A channel invite accepted to already_applied is terminal too; a
    # contradictory retry cannot reverse the durable decision.
    bridge_r2.decide_outcomes["iv_2"] = "already_applied"
    assert await replay.handle_operator_reply(thread_root_id=f"thread_{ref_2}", text="yes") is True
    assert await replay.handle_operator_reply(thread_root_id=f"thread_{ref_2}", text="n") is True
    state = load_keyless_invitations(slug)
    assert state["iv_2"]["phase"] == "terminal"
    assert state["iv_2"]["outcome"] == "already_applied"
    assert state["iv_2"]["decision"] == "accept"

    # Another resume sends nothing: terminal records stay put.
    again, bridge_r3, dms_r3 = _flow(slug)
    await again.resume()
    assert bridge_r3.decision_calls == []
    assert dms_r3.send_calls == []
    assert {k: v["phase"] for k, v in load_keyless_invitations(slug).items()} == {
        "iv_1": "terminal",
        "iv_2": "terminal",
        "iv_3": "awaiting_reply",
        "iv_op": "terminal",
    }


@pytest.mark.asyncio
async def test_fail_closed_and_terminal_resolution(caplog):
    """Guarded regressions: with no operator an invite stays pending with
    no prompt/decision; a failed persist restores in-memory state and sends
    nothing; malformed/key-mismatched rows are skipped with a warning;
    conflict/not_found resolve terminal and are logged without a retry loop;
    and auto_accept_space_invitations decides only space-scope records."""
    isolated_home()

    # A) Missing operator -> invite retained pending, nothing is sent.
    flow_none, bridge_none, dms_none = _flow("agent-none", operator="")
    await flow_none.reconcile([_invite("iv_miss", scope="space", space_id="sp_m", inviter="alice-1")])
    assert dms_none.send_calls == []
    assert bridge_none.decision_calls == []
    state = load_keyless_invitations("agent-none")
    assert state["iv_miss"]["decision"] == "pending"
    assert state["iv_miss"]["phase"] == "unsent"
    # A later authoritative list keeps it pending (not retired).
    await flow_none.reconcile([_invite("iv_miss", scope="space", space_id="sp_m", inviter="alice-1")])
    assert "iv_miss" in load_keyless_invitations("agent-none")

    # B) Write failure -> nothing prompted, in-memory mirror restored.
    flow_fail, bridge_fail, dms_fail = _flow("agent-fail")
    flow_fail._save_state = lambda slug, pending: (_ for _ in ()).throw(OSError("disk full"))
    await flow_fail.reconcile([_invite("iv_fail", scope="channel", space_id="sp_f", inviter="bob-1")])
    assert dms_fail.send_calls == []
    assert bridge_fail.decision_calls == []
    assert "iv_fail" not in flow_fail._pending

    # C) Malformed and key-mismatched rows are skipped with a warning.
    save_keyless_invitations("agent-mal", {
        "iv_ok": {
            "kind": "keyless_invitation",
            "invitation_event_id": "iv_ok",
            "invite": {"invitation_event_id": "iv_ok", "scope": "space", "space_id": "sp_o"},
            "prompt_client_ref": "p_ok",
            "prompt_envelope_id": None,
            "prompt_thread_id": None,
            "decision": "pending",
            "decision_client_ref": None,
            "phase": "unsent",
            "outcome": None,
        },
        "iv_bad": {"kind": "keyless_invitation", "invitation_event_id": "iv_bad"},
        "iv_key": {
            "kind": "keyless_invitation",
            "invitation_event_id": "OTHER",
            "invite": {"invitation_event_id": "OTHER", "scope": "space", "space_id": "sp_x"},
            "prompt_client_ref": "p_k",
            "prompt_envelope_id": None,
            "prompt_thread_id": None,
            "decision": "pending",
            "decision_client_ref": None,
            "phase": "unsent",
            "outcome": None,
        },
    })
    with caplog.at_level(logging.WARNING, logger="puffo_agent.agent.keyless_invitation_state"):
        loaded = load_keyless_invitations("agent-mal")
    assert set(loaded) == {"iv_ok"}
    assert any("skipping malformed record" in record.message for record in caplog.records)

    # D) conflict/not_found resolve terminal, logged, with no retry loop.
    flow_c, bridge_c, dms_c = _flow("agent-term")
    bridge_c.decide_outcomes = {"iv_conf": "conflict", "iv_nf": "not_found"}
    await flow_c.reconcile([
        _invite("iv_conf", scope="space", space_id="sp_c", inviter="carol-1"),
        _invite("iv_nf", scope="channel", space_id="sp_c", inviter="dave-1"),
    ])
    refs = load_keyless_invitations("agent-term")
    assert await flow_c.handle_operator_reply(
        thread_root_id=f"thread_{refs['iv_conf']['prompt_client_ref']}", text="y",
    ) is True
    assert await flow_c.handle_operator_reply(
        thread_root_id=f"thread_{refs['iv_nf']['prompt_client_ref']}", text="y",
    ) is True
    state = load_keyless_invitations("agent-term")
    assert state["iv_conf"]["phase"] == "terminal"
    assert state["iv_conf"]["outcome"] == "conflict"
    assert state["iv_nf"]["phase"] == "terminal"
    assert state["iv_nf"]["outcome"] == "not_found"
    replay, bridge_r, dms_r = _flow("agent-term", bridge=_Bridge("agent-term"))
    await replay.resume()
    assert bridge_r.decision_calls == []
    assert dms_r.send_calls == []

    # E) The auto-accept flag decides only space-scope records; a channel
    # invite gains no new default and is still prompted.
    flow_aa, bridge_aa, dms_aa = _flow("agent-aa", auto_accept=True)
    await flow_aa.reconcile([
        _invite("iv_sp", scope="space", space_id="sp_a", inviter="eve-1"),
        _invite("iv_ch", scope="channel", space_id="sp_a", inviter="frank-1"),
    ])
    state = load_keyless_invitations("agent-aa")
    assert state["iv_sp"]["phase"] == "terminal"
    assert state["iv_sp"]["decision"] == "accept"
    assert state["iv_ch"]["phase"] == "awaiting_reply"
    assert len(dms_aa.send_calls) == 1
    assert len(bridge_aa.decision_calls) == 1
    assert bridge_aa.decision_calls[0]["invitation_event_id"] == "iv_sp"
