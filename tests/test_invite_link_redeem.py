"""Operator-gated redemption of a group invite link.

A ``/i/<code>`` handed to the agent never auto-joins: it must clear the
operator's y/n approval, fail-closed with no operator, and dedup repeats from
the same source. These cover the pure parser/dedup plus the request → approve →
redeem flow against a fake client.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent import invite_link_redeem as ilr  # noqa: E402


class _FakeHttp:
    def __init__(self, preview: dict | None) -> None:
        self._preview = preview
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    async def get(self, path: str):
        self.gets.append(path)
        return self._preview

    async def post(self, path: str, body: dict | None = None):
        self.posts.append((path, body or {}))
        return {"applied": True}


def _make_client(*, operator_slug: str = "op-1", preview: dict | None = None):
    client = types.SimpleNamespace()
    client.slug = "agent-1"
    client.device_id = "dev-1"
    client.operator_slug = operator_slug
    client.keystore = types.SimpleNamespace(
        load_session=lambda slug: types.SimpleNamespace(
            subkey_id="sk-1", subkey_secret_key="secret"
        )
    )
    client.http = _FakeHttp(
        preview if preview is not None
        else {"space_id": "sp_1", "invite_id": "inv_1", "space_name": "Team"}
    )
    client._pending_redeem_dms = {}
    client._redeem_approval_seen = {}
    client._log = logging.getLogger("invite-link-redeem-test")
    sent: list[dict] = []

    async def _send_dm(recipient_slug, text, root_id=""):
        sent.append({"to": recipient_slug, "text": text, "root_id": root_id})
        return {"envelope_id": f"env_{len(sent)}"}

    client._send_dm = _send_dm
    client._sent_dms = sent
    return client


# ── extract_invite_short_code ────────────────────────────────────────

def test_extract_from_url():
    assert ilr.extract_invite_short_code("join https://p.ai/i/AbC12345 now") == "AbC12345"


def test_extract_none_without_link():
    assert ilr.extract_invite_short_code("no link here") is None
    assert ilr.extract_invite_short_code("") is None


# ── is_bare_invite_link (F1: consume only bare links) ────────────────

def test_bare_link_is_bare():
    assert ilr.is_bare_invite_link("https://p.ai/i/AbC12345") is True
    assert ilr.is_bare_invite_link("  https://p.ai/i/AbC12345 .") is True


def test_link_plus_request_is_not_bare():
    assert ilr.is_bare_invite_link(
        "join /i/AbC12345 and summarize last week's analysis"
    ) is False


# ── recently_asked (dedup check; does not record) ────────────────────

def test_recently_asked_checks_and_prunes_without_recording():
    seen: dict[tuple[str, str], float] = {}
    key = ("AbC12345", "stranger-1")
    # Absent → not recently asked, and the check must not record it.
    assert ilr.recently_asked(seen, key, now=1000.0, window_s=300.0) is False
    assert seen == {}
    # Present within window → True.
    seen[key] = 1000.0
    assert ilr.recently_asked(seen, key, now=1100.0, window_s=300.0) is True
    # Past the window the stale key is pruned → allowed again.
    assert ilr.recently_asked(seen, key, now=1400.0, window_s=300.0) is False
    assert key not in seen


# ── request_link_redeem_approval ─────────────────────────────────────

def test_no_operator_fails_closed():
    client = _make_client(operator_slug="")
    asyncio.run(
        ilr.request_link_redeem_approval(
            client, short_code="AbC12345", source_slug="stranger-1"
        )
    )
    assert client._sent_dms == []           # no DM to anyone
    assert client._pending_redeem_dms == {}  # nothing pending → cannot join
    assert client.http.gets == []            # never even fetched the preview


def test_approval_dms_operator_and_registers_pending():
    client = _make_client()
    asyncio.run(
        ilr.request_link_redeem_approval(
            client, short_code="AbC12345", source_slug="stranger-1"
        )
    )
    assert client.http.gets == ["/v2/invitations/links/AbC12345"]
    assert len(client._sent_dms) == 1
    assert client._sent_dms[0]["to"] == "op-1"
    (meta,) = client._pending_redeem_dms.values()
    assert meta["short_code"] == "AbC12345"
    assert meta["invite_id"] == "inv_1"
    assert meta["space_id"] == "sp_1"


def test_repeat_from_same_source_dedups_no_second_prompt():
    client = _make_client()
    for _ in range(2):
        asyncio.run(
            ilr.request_link_redeem_approval(
                client, short_code="AbC12345", source_slug="stranger-1"
            )
        )
    assert len(client._sent_dms) == 1  # operator prompted once, not twice


def test_preview_without_ids_does_not_prompt():
    client = _make_client(preview={"space_name": "Team"})
    asyncio.run(
        ilr.request_link_redeem_approval(
            client, short_code="AbC12345", source_slug="stranger-1"
        )
    )
    assert client._sent_dms == []
    assert client._pending_redeem_dms == {}


def test_failed_preview_does_not_record_dedup():
    client = _make_client()

    async def _boom(path):
        raise RuntimeError("network down")

    client.http.get = _boom
    asyncio.run(
        ilr.request_link_redeem_approval(
            client, short_code="AbC12345", source_slug="stranger-1"
        )
    )
    # F2: a failed ask must not occupy the dedup slot, so a legit retry works.
    assert client._pending_redeem_dms == {}
    assert client._redeem_approval_seen == {}


# ── handle_redeem_reply ──────────────────────────────────────────────

def _seed_pending(client, root="env_1"):
    client._pending_redeem_dms[root] = {
        "short_code": "AbC12345",
        "invite_id": "inv_1",
        "space_id": "sp_1",
        "space_name": "Team",
        "source_slug": "stranger-1",
    }
    return root


def test_reply_yes_redeems_and_confirms(monkeypatch):
    client = _make_client()
    root = _seed_pending(client)
    calls: list[dict] = []

    async def _stub_redeem(c, *, short_code, invite_id, space_id):
        calls.append({"short_code": short_code, "invite_id": invite_id, "space_id": space_id})

    monkeypatch.setattr(ilr, "redeem_invite_capability", _stub_redeem)
    consumed = asyncio.run(ilr.handle_redeem_reply(client, thread_root_id=root, text="y"))
    assert consumed is True
    assert calls == [{"short_code": "AbC12345", "invite_id": "inv_1", "space_id": "sp_1"}]
    assert root not in client._pending_redeem_dms
    assert client._sent_dms[-1]["to"] == "op-1"  # confirmation DM


def test_reply_yes_redeem_failure_confirms_without_raw_error(monkeypatch):
    client = _make_client()
    root = _seed_pending(client)

    async def _boom(c, *, short_code, invite_id, space_id):
        raise RuntimeError('HTTP 410 {"error":"gone"}')

    monkeypatch.setattr(ilr, "redeem_invite_capability", _boom)
    consumed = asyncio.run(ilr.handle_redeem_reply(client, thread_root_id=root, text="y"))
    assert consumed is True
    assert root not in client._pending_redeem_dms  # pending cleared even on failure
    confirm = client._sent_dms[-1]["text"]
    assert "Couldn't join" in confirm
    # F3: the raw server error stays in the log, never in the operator DM.
    assert "410" not in confirm and "gone" not in confirm


def test_reply_no_skips_redeem(monkeypatch):
    client = _make_client()
    root = _seed_pending(client)
    calls: list[dict] = []
    monkeypatch.setattr(
        ilr, "redeem_invite_capability",
        lambda *a, **k: calls.append(k),  # type: ignore[arg-type]
    )
    consumed = asyncio.run(ilr.handle_redeem_reply(client, thread_root_id=root, text="n"))
    assert consumed is True
    assert calls == []
    assert root not in client._pending_redeem_dms


def test_reply_nonverdict_is_not_consumed():
    client = _make_client()
    root = _seed_pending(client)
    consumed = asyncio.run(
        ilr.handle_redeem_reply(client, thread_root_id=root, text="what group?")
    )
    assert consumed is False
    assert root in client._pending_redeem_dms  # still awaiting a real y/n


def test_reply_unknown_thread_is_not_consumed():
    client = _make_client()
    consumed = asyncio.run(
        ilr.handle_redeem_reply(client, thread_root_id="env_other", text="y")
    )
    assert consumed is False


# ── redeem_invite_capability ─────────────────────────────────────────

def test_redeem_posts_signed_event_to_link_endpoint(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(ilr, "decode_secret", lambda s: b"\x00" * 32)
    monkeypatch.setattr(
        ilr.Ed25519KeyPair, "from_secret_bytes", staticmethod(lambda b: object())
    )
    monkeypatch.setattr(
        ilr, "sign_event",
        lambda **kw: {"kind": kw["kind"], "payload": kw["payload"], "signature": "sig"},
    )
    asyncio.run(
        ilr.redeem_invite_capability(
            client, short_code="AbC12345", invite_id="inv_1", space_id="sp_1"
        )
    )
    assert len(client.http.posts) == 1
    path, body = client.http.posts[0]
    assert path == "/v2/invitations/links/AbC12345/redeem"
    event = body["event"]
    assert event["kind"] == ilr.EventKind.REDEEM_INVITE_CAPABILITY
    payload = event["payload"]
    assert payload["invite_id"] == "inv_1"
    assert payload["space_id"] == "sp_1"
    assert payload["redeemer_slug"] == "agent-1"
    assert payload["redeemer_device_id"] == "dev-1"
    assert payload["redeemer_subkey_id"] == "sk-1"
    assert "capability_signature" not in payload  # omitted for short-code links


# ── invite_link_gate (ingress entry point) ───────────────────────────

from puffo_agent.agent import ingress_policy  # noqa: E402
from puffo_agent.agent.ingress_policy import invite_link_gate  # noqa: E402
from puffo_agent.agent.message_store import ReceiptDisposition  # noqa: E402


def _dm_payload(text: str, *, kind: str = "dm", sender: str = "stranger-1"):
    return types.SimpleNamespace(envelope_kind=kind, sender_slug=sender, content=text)


def test_gate_ignores_non_dm(monkeypatch):
    seen: list = []

    async def _stub(client, *, short_code, source_slug):
        seen.append((short_code, source_slug))

    monkeypatch.setattr(ingress_policy, "request_link_redeem_approval", _stub)
    verdict = asyncio.run(
        invite_link_gate(_make_client(), _dm_payload("/i/AbC12345", kind="channel"),
                          "/i/AbC12345")
    )
    assert verdict is None
    assert seen == []


def test_gate_passes_through_dm_without_link():
    verdict = asyncio.run(
        invite_link_gate(_make_client(), _dm_payload("hi"), "hi")
    )
    assert verdict is None


def test_gate_bare_link_approves_and_terminates(monkeypatch):
    calls: list[dict] = []

    async def _stub(client, *, short_code, source_slug):
        calls.append({"short_code": short_code, "source_slug": source_slug})

    monkeypatch.setattr(ingress_policy, "request_link_redeem_approval", _stub)
    payload = _dm_payload("https://p.ai/i/AbC12345")
    verdict = asyncio.run(invite_link_gate(_make_client(), payload, payload.content))
    assert verdict is not None
    assert verdict.disposition is ReceiptDisposition.TERMINAL
    assert calls == [{"short_code": "AbC12345", "source_slug": "stranger-1"}]


def test_gate_mixed_message_approves_but_reaches_model(monkeypatch):
    """F1: a link + a real request still triggers approval, but is NOT consumed
    (returns None) so the request reaches the model."""
    calls: list[dict] = []

    async def _stub(client, *, short_code, source_slug):
        calls.append({"short_code": short_code})

    monkeypatch.setattr(ingress_policy, "request_link_redeem_approval", _stub)
    payload = _dm_payload("join /i/AbC12345 and summarize last week's analysis")
    verdict = asyncio.run(invite_link_gate(_make_client(), payload, payload.content))
    assert verdict is None          # not swallowed → continues to the model
    assert calls == [{"short_code": "AbC12345"}]  # approval still fired
