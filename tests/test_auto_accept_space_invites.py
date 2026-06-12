"""Hidden ``auto_accept_space_invitations`` flag. When on, a space
invite from a non-operator is auto-accepted and the operator is DM'd a
report — instead of the usual y/n approval prompt. Channel invites are
unaffected, and the operator-inviter path stays silent.
"""

from __future__ import annotations

import logging

import pytest

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


def _make_client(*, flag: bool, operator_slug: str = "op-1"):
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.operator_slug = operator_slug
    client.auto_accept_space_invitations = flag
    client._processed_invite_ids = set()
    client._pending_invite_dms = {}
    client._log = logging.getLogger("auto-accept-space-test")

    accepts: list[tuple] = []
    notifies: list[dict] = []
    reports: list[dict] = []

    async def _stub_inviter_is_operator(inviter_slug):
        return inviter_slug == operator_slug

    async def _stub_accept(kind, eid, space_id, channel_id):
        accepts.append((kind, eid, space_id, channel_id))

    async def _stub_notify(**kw):
        notifies.append(kw)

    async def _stub_report(*, inviter_slug, space_id, space_name):
        reports.append({"inviter": inviter_slug, "space_id": space_id, "space_name": space_name})

    client._inviter_is_operator = _stub_inviter_is_operator  # type: ignore[assignment]
    client._accept_invite = _stub_accept  # type: ignore[assignment]
    client._notify_operator_of_invite = _stub_notify  # type: ignore[assignment]
    client._report_auto_accepted_space_invite = _stub_report  # type: ignore[assignment]
    client._accepts = accepts  # type: ignore[attr-defined]
    client._notifies = notifies  # type: ignore[attr-defined]
    client._reports = reports  # type: ignore[attr-defined]
    return client


def _kw(kind="invite_to_space", inviter="alice-0001", **over):
    base = {
        "kind": kind,
        "invitation_event_id": "ev_1",
        "inviter_slug": inviter,
        "space_id": "sp_1",
        "channel_id": "ch_1" if kind == "invite_to_channel" else "",
        "space_name": "Team",
        "channel_name": "general" if kind == "invite_to_channel" else None,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_flag_on_auto_accepts_nonoperator_space_invite_and_reports():
    client = _make_client(flag=True)
    await client._process_invite(**_kw())
    assert client._accepts == [("invite_to_space", "ev_1", "sp_1", "")]
    assert client._notifies == []  # no y/n prompt
    assert len(client._reports) == 1
    assert client._reports[0]["space_id"] == "sp_1"
    assert "ev_1" in client._processed_invite_ids


@pytest.mark.asyncio
async def test_flag_off_still_prompts_operator():
    client = _make_client(flag=False)
    await client._process_invite(**_kw())
    assert client._accepts == []
    assert len(client._notifies) == 1
    assert client._reports == []


@pytest.mark.asyncio
async def test_flag_does_not_touch_channel_invites():
    client = _make_client(flag=True)
    await client._process_invite(**_kw(kind="invite_to_channel"))
    # Channel invites still go through the y/n prompt even with the flag.
    assert client._accepts == []
    assert len(client._notifies) == 1
    assert client._reports == []


@pytest.mark.asyncio
async def test_operator_inviter_auto_accepts_silently_even_with_flag():
    client = _make_client(flag=True)
    await client._process_invite(**_kw(inviter="op-1"))
    assert client._accepts == [("invite_to_space", "ev_1", "sp_1", "")]
    assert client._reports == []  # operator initiated → no report
    assert client._notifies == []


@pytest.mark.asyncio
async def test_failed_auto_accept_is_not_marked_processed_and_no_report():
    client = _make_client(flag=True)

    async def _boom(kind, eid, space_id, channel_id):
        raise RuntimeError("server rejected")

    client._accept_invite = _boom  # type: ignore[assignment]
    await client._process_invite(**_kw())
    assert "ev_1" not in client._processed_invite_ids  # retries next poll
    assert client._reports == []
