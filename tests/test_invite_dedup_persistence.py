"""PUF-240: invite dedup must survive across simulated reconnect.

The bug: ``self._processed_invite_ids = set()`` lived inside
``listen()`` (cleared on every WS (re)connect). The 30s
``_invite_poll_loop`` then re-emits the operator-confirm DM for
each still-pending invite, so a daemon that reconnects N times
between the original invite and the operator's ✓ / ✗ yields N
duplicate prompts in the operator's confirm thread (~10× on Sam's
host per Tier-1 screenshot evidence).

We don't drive the real ``listen()`` here — it spins up a WS
connection + priority queue + a consumer task — but we can pin the
invariant the fix preserves: ``_processed_invite_ids`` initialised
on ``__init__`` survives subsequent listen-style operations, and
``_poll_pending_invites`` doesn't re-emit a DM for an already-
processed invite even when fed the same server response twice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


def _make_client(
    operator_slug: str = "op-1",
    workspace: str = "",
) -> tuple[PuffoCoreMessageClient, list[dict]]:
    """Bare client harness mirroring ``test_membership_events.py``.

    Stubs ``_send_dm`` (captures into ``sent``), the name-resolution
    helpers (canonical labels), ``_fetch_inviter_root_pubkey`` (so
    ``_inviter_is_operator`` returns False without a /certs/sync
    round-trip — keeps every test on the operator-DM branch), and
    ``http.get`` for ``/invites?direction=received`` so the poll
    loop returns whatever the test sets via ``_invites_response``.

    ``workspace`` lets a test point the client at a real on-disk
    dir so ``_persist_processed_invite`` / ``_load_processed_invites``
    exercise the sidecar. Defaults to ``""`` — persistence becomes a
    no-op (``_processed_invites_path`` returns ``None``) and only
    the in-memory dedup is tested.
    """
    import logging

    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "agent-1"
    client.operator_slug = operator_slug
    client.workspace = workspace
    client._log = logging.getLogger("test-puf-240")
    client._processed_invite_ids = set()
    client._processed_invite_ids.update(client._load_processed_invites())
    client._pending_invite_dms = {}
    client._operator_root_pubkey = b"op-root-pk"
    client._inviter_root_cache = {}
    client._invites_response: dict = {"invites": []}

    sent: list[dict] = []

    async def _stub_send_dm(recipient_slug: str, text: str, root_id: str) -> dict | None:
        sent.append({"to": recipient_slug, "text": text, "root_id": root_id})
        return {"envelope_id": f"env_dm_{len(sent)}"}

    async def _stub_space_name(space_id: str) -> str:
        return space_id

    async def _stub_channel_name(*, space_id: str, channel_id: str) -> str:
        return channel_id

    async def _stub_display_name(slug: str) -> str:
        return ""

    async def _stub_inviter_pk(slug: str) -> bytes | None:
        # Return a DIFFERENT key from the operator's so the dedup
        # path lands on the operator-DM branch every time.
        return b"non-operator-pk"

    client._send_dm = _stub_send_dm  # type: ignore[assignment]
    client._resolve_space_name = _stub_space_name  # type: ignore[assignment]
    client._resolve_channel_name = _stub_channel_name  # type: ignore[assignment]
    client._fetch_display_name = _stub_display_name  # type: ignore[assignment]
    client._fetch_inviter_root_pubkey = _stub_inviter_pk  # type: ignore[assignment]

    class _StubHttp:
        async def get(self, path: str):
            if path.startswith("/invites"):
                return client._invites_response
            return {}

    client.http = _StubHttp()

    return client, sent


def _invite_row(event_id: str = "ev_invite_1") -> dict:
    return {
        "invitation_event_id": event_id,
        "scope": "channel",
        "space_id": "sp_1",
        "channel_id": "ch_1",
        "inviter_slug": "non-op",
        "space_name": "Team",
        "channel_name": "general",
    }


@pytest.mark.asyncio
async def test_dedup_survives_simulated_reconnect_within_one_session():
    """Two consecutive ``_poll_pending_invites`` calls over the same
    still-pending invite must emit exactly one operator DM."""
    client, sent = _make_client()
    client._invites_response = {"invites": [_invite_row()]}

    await client._poll_pending_invites()
    await client._poll_pending_invites()

    assert len(sent) == 1
    assert "channel" in sent[0]["text"].lower()
    assert sent[0]["to"] == "op-1"


@pytest.mark.asyncio
async def test_dedup_survives_reconnect_when_set_persisted():
    """PUF-240 invariant. The fix keeps ``_processed_invite_ids``
    in ``__init__`` (it was reset inside ``listen()``). Simulating
    a reconnect = a new ``listen()`` body must NOT wipe the set.
    We exercise this by polling once, then NOT resetting the set
    before polling again, and verifying no second DM fires."""
    client, sent = _make_client()
    client._invites_response = {"invites": [_invite_row("ev_persist")]}

    # First poll — operator gets the prompt.
    await client._poll_pending_invites()
    assert len(sent) == 1
    assert "ev_persist" in client._processed_invite_ids

    # Simulate a WS reconnect. After PUF-240 the listen() body no
    # longer runs ``self._processed_invite_ids = set()``; the set
    # carries over. The next poll on the same still-pending row
    # must NOT re-emit.
    await client._poll_pending_invites()
    await client._poll_pending_invites()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_distinct_invites_each_get_one_prompt():
    """Distinct invitation_event_ids dedup independently — two
    real invites that arrive in the same poll cycle must each fire
    one DM, but a re-poll of the same two must add zero more."""
    client, sent = _make_client()
    client._invites_response = {
        "invites": [
            _invite_row("ev_a"),
            {**_invite_row("ev_b"), "channel_id": "ch_2", "channel_name": "secrets"},
        ],
    }

    await client._poll_pending_invites()
    assert len(sent) == 2

    # Repeat the poll — same two still pending; dedup catches both.
    await client._poll_pending_invites()
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_dedup_persists_across_failed_dm_attempt():
    """Even if the operator DM raises, the dedup must still record
    the attempt — re-polling shouldn't retry the DM. (The existing
    line 1744 ``finally`` already does this; pin it so PUF-240's
    refactor doesn't accidentally undo it.)"""
    client, sent = _make_client()
    client._invites_response = {"invites": [_invite_row("ev_failing")]}

    async def _failing_send_dm(*args, **kwargs):
        raise RuntimeError("simulated DM transport failure")

    client._send_dm = _failing_send_dm  # type: ignore[assignment]

    await client._poll_pending_invites()
    # The DM raised; dedup still records the attempt.
    assert "ev_failing" in client._processed_invite_ids

    # Re-polling MUST NOT retry the DM. Swap the stub back in to
    # capture any new attempt — none should happen.
    captured: list[dict] = []

    async def _capturing_send_dm(recipient_slug: str, text: str, root_id: str):
        captured.append({"to": recipient_slug, "text": text, "root_id": root_id})
        return {"envelope_id": "env_post_recovery"}

    client._send_dm = _capturing_send_dm  # type: ignore[assignment]
    await client._poll_pending_invites()
    assert captured == []


@pytest.mark.asyncio
async def test_dedup_survives_daemon_restart(tmp_path: Path):
    """PUF-240 daemon-restart class. The in-memory dedup is now also
    sidecar-persisted to ``<workspace>/.puffo-agent/processed_invites.json``
    so a daemon restart between invite-and-act doesn't multi-emit
    either. Simulate the restart by constructing a SECOND client
    pointed at the same workspace — its ``_load_processed_invites``
    should rehydrate the set and the next poll should NOT re-fire
    the operator DM."""
    workspace = str(tmp_path)
    client_a, sent_a = _make_client(workspace=workspace)
    client_a._invites_response = {"invites": [_invite_row("ev_restart")]}

    # First daemon lifetime: emit + persist.
    await client_a._poll_pending_invites()
    assert len(sent_a) == 1
    sidecar = tmp_path / ".puffo-agent" / "processed_invites.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert "ev_restart" in payload["processed_invite_ids"]

    # Simulate daemon restart — fresh PuffoCoreMessageClient instance,
    # same workspace dir. Server-side pending_invites still returns
    # the same row (the operator hasn't acted yet).
    client_b, sent_b = _make_client(workspace=workspace)
    client_b._invites_response = {"invites": [_invite_row("ev_restart")]}

    assert "ev_restart" in client_b._processed_invite_ids
    await client_b._poll_pending_invites()
    assert sent_b == []
