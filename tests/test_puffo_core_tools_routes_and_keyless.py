"""Outgoing-root and keyless transport regression coverage."""

import tempfile

import pytest

from puffo_agent.agent._visibility import resolve_visibility
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.primitives import KemKeyPair
from puffo_agent.mcp.puffo_core_tools import _resolve_outgoing_root

from test_puffo_core_tools import (
    _SpyKeyStore,
    _build_tools,
    _call,
    _call_structured,
    _keyless_sends,
    _now_ms,
    _setup,
    _setup_keyless,
)


@pytest.mark.asyncio
async def test_send_message_with_attachments_requires_workspace():
    """Without ``cfg.workspace``, send_message_with_attachments refuses
    rather than silently dropping into a "no agent dir" hole. Real
    upload path is exercised end-to-end against a live daemon."""
    cfg, _, _ = _setup()
    # Fixture leaves cfg.workspace as None.
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc_info:
        await _call(
            mcp,
            "send_message_with_attachments",
            {"paths": ["test.txt"], "channel": "ch_1", "visibility_level": "human"},
        )
    assert "workspace" in str(exc_info.value).lower()


# Send-side root resolution: walk + scope rejection + system-envelope rules.


class _FakeDataClient:
    """Stand-in for ``DataClient`` — seed thread_root_id values and
    inject lookup failures without touching SQLite."""

    def __init__(self):
        self.messages: dict[str, object] = {}
        self.exc: Exception | None = None
        self.calls: list[str] = []

    def add(
        self,
        envelope_id: str,
        thread_root_id: str | None,
        *,
        channel_id: str | None = None,
        space_id: str | None = None,
        sender_slug: str = "alice-0001",
        envelope_kind: str = "channel",
        recipient_slug: str | None = None,
    ) -> None:
        class _Msg:
            pass
        m = _Msg()
        m.envelope_id = envelope_id
        m.thread_root_id = thread_root_id
        m.channel_id = channel_id
        m.space_id = space_id
        m.sender_slug = sender_slug
        m.envelope_kind = envelope_kind
        m.recipient_slug = recipient_slug
        self.messages[envelope_id] = m

    async def get_message_by_envelope(self, envelope_id: str):
        self.calls.append(envelope_id)
        if self.exc is not None:
            raise self.exc
        return self.messages.get(envelope_id)

    async def get_thread_messages(self, root_id: str, limit: int = 50):
        if self.exc is not None:
            raise self.exc
        return [
            m for m in self.messages.values()
            if m.envelope_id == root_id or m.thread_root_id == root_id
        ][:limit]


async def _resolve(dc, root_id, **kw):
    defaults = dict(self_slug="agent-0001", channel_id=None, space_id=None, dm_peer=None)
    defaults.update(kw)
    return await _resolve_outgoing_root(root_id, dc, **defaults)


@pytest.mark.asyncio
async def test_outgoing_root_empty_skips_lookup():
    dc = _FakeDataClient()
    resolved, note = await _resolve(dc, "")
    assert resolved is None and note == "" and dc.calls == []
    resolved, note = await _resolve(dc, "   ")
    assert resolved is None and note == "" and dc.calls == []


@pytest.mark.asyncio
async def test_outgoing_root_true_root_unchanged():
    dc = _FakeDataClient()
    dc.add("msg_root", thread_root_id=None)
    resolved, note = await _resolve(dc, "msg_root")
    assert resolved == "msg_root" and note == ""


@pytest.mark.asyncio
async def test_outgoing_root_reply_auto_corrected():
    dc = _FakeDataClient()
    dc.add("msg_root", thread_root_id=None)
    dc.add("msg_reply", thread_root_id="msg_root")
    resolved, note = await _resolve(dc, "msg_reply")
    assert resolved == "msg_root"
    assert "auto-corrected" in note


@pytest.mark.asyncio
async def test_outgoing_root_depth_two_chain_walks_to_root():
    dc = _FakeDataClient()
    dc.add("msg_root", thread_root_id=None)
    dc.add("msg_mid", thread_root_id="msg_root")
    dc.add("msg_leaf", thread_root_id="msg_mid")
    resolved, note = await _resolve(dc, "msg_leaf")
    assert resolved == "msg_root"
    assert dc.calls == ["msg_leaf", "msg_mid", "msg_root"]


@pytest.mark.asyncio
async def test_outgoing_root_lookup_miss_wipes_to_none():
    dc = _FakeDataClient()
    resolved, note = await _resolve(dc, "msg_unknown")
    assert resolved is None
    assert "not in local cache" in note


@pytest.mark.asyncio
async def test_outgoing_root_missing_but_claimed_is_kept():
    dc = _FakeDataClient()
    dc.add("msg_reply", thread_root_id="ghost-root", channel_id="ch_1")
    resolved, note = await _resolve(dc, "ghost-root", channel_id="ch_1")
    assert resolved == "ghost-root" and note == ""


@pytest.mark.asyncio
async def test_outgoing_root_missing_claimant_wrong_channel_rejects():
    dc = _FakeDataClient()
    dc.add("msg_reply", thread_root_id="ghost-root", channel_id="ch_OTHER")
    with pytest.raises(RuntimeError):
        await _resolve(dc, "ghost-root", channel_id="ch_1")


@pytest.mark.asyncio
async def test_outgoing_root_missing_but_claimed_dm_is_kept():
    dc = _FakeDataClient()
    dc.add(
        "msg_reply", thread_root_id="ghost-root",
        envelope_kind="dm", sender_slug="peer-0001",
        recipient_slug="agent-0001",
    )
    resolved, note = await _resolve(dc, "ghost-root", dm_peer="peer-0001")
    assert resolved == "ghost-root" and note == ""


@pytest.mark.asyncio
async def test_outgoing_root_data_not_found_treated_as_miss():
    from puffo_agent.agent.message_store import DataNotFound
    dc = _FakeDataClient()
    dc.exc = DataNotFound("msg_only_on_server")
    resolved, note = await _resolve(dc, "msg_only_on_server")
    assert resolved is None
    assert "not in local cache" in note


@pytest.mark.asyncio
async def test_outgoing_root_transport_error_wipes_to_none():
    dc = _FakeDataClient()
    dc.exc = RuntimeError("simulated transport blip")
    resolved, note = await _resolve(dc, "msg_anything")
    assert resolved is None
    assert "could not be verified" in note


@pytest.mark.asyncio
async def test_outgoing_root_cycle_preserves_root_id_with_warning():
    dc = _FakeDataClient()
    dc.add("msg_a", thread_root_id="msg_b")
    dc.add("msg_b", thread_root_id="msg_a")
    resolved, note = await _resolve(dc, "msg_a")
    assert resolved == "msg_a"
    assert "cycle detected" in note


@pytest.mark.asyncio
async def test_outgoing_root_depth_cap_preserves_root_id_with_warning():
    dc = _FakeDataClient()
    for i in range(9):
        dc.add(f"msg_l{i}", thread_root_id=f"msg_l{i + 1}")
    dc.add("msg_l9", thread_root_id=None)
    resolved, note = await _resolve(dc, "msg_l0")
    assert resolved == "msg_l0"
    assert "deeper than" in note


@pytest.mark.asyncio
async def test_outgoing_root_system_sender_wiped_to_new_root():
    """Rule 1: daemon-minted system envelope -> send as a new top-level."""
    dc = _FakeDataClient()
    dc.add("intro-prompt-1", thread_root_id="intro-prompt-1", sender_slug="system")
    resolved, note = await _resolve(dc, "intro-prompt-1")
    assert resolved is None
    assert "system message" in note


@pytest.mark.asyncio
async def test_outgoing_root_self_reference_wiped_to_new_root():
    """Rule 2: self-referencing root (non-system sender) -> same wipe."""
    dc = _FakeDataClient()
    dc.add("msg_weird", thread_root_id="msg_weird")
    resolved, note = await _resolve(dc, "msg_weird")
    assert resolved is None
    assert "system message" in note


@pytest.mark.asyncio
async def test_outgoing_root_walk_into_system_root_wiped():
    """Rule 1 via rule 4: a reply whose chain tops out at a system
    envelope wipes instead of auto-correcting to a dangling id."""
    dc = _FakeDataClient()
    dc.add("intro-prompt-1", thread_root_id="intro-prompt-1", sender_slug="system")
    dc.add("msg_reply", thread_root_id="intro-prompt-1")
    resolved, note = await _resolve(dc, "msg_reply")
    assert resolved is None
    assert "system message" in note


@pytest.mark.asyncio
async def test_outgoing_root_cross_channel_rejected():
    """Rule 3: cross-channel reference is an agent error -> reject."""
    dc = _FakeDataClient()
    dc.add("msg_root", thread_root_id=None, channel_id="ch_general", space_id="sp_1")
    with pytest.raises(RuntimeError) as ei:
        await _resolve(dc, "msg_root", channel_id="ch_gtm", space_id="sp_1")
    assert "ch_general" in str(ei.value)
    assert "ch_gtm" in str(ei.value)


@pytest.mark.asyncio
async def test_outgoing_root_cross_space_rejected():
    dc = _FakeDataClient()
    dc.add("msg_root", thread_root_id=None, channel_id="ch_x", space_id="sp_OTHER")
    with pytest.raises(RuntimeError):
        await _resolve(dc, "msg_root", channel_id="ch_x", space_id="sp_1")


@pytest.mark.asyncio
async def test_outgoing_root_same_channel_passes_scope():
    dc = _FakeDataClient()
    dc.add("msg_root", thread_root_id=None, channel_id="ch_x", space_id="sp_1")
    resolved, note = await _resolve(dc, "msg_root", channel_id="ch_x", space_id="sp_1")
    assert resolved == "msg_root" and note == ""


@pytest.mark.asyncio
async def test_outgoing_root_dm_target_rejects_channel_message():
    dc = _FakeDataClient()
    dc.add("msg_root", thread_root_id=None, channel_id="ch_x")
    with pytest.raises(RuntimeError) as ei:
        await _resolve(dc, "msg_root", dm_peer="alice-0001")
    assert "DM" in str(ei.value)


@pytest.mark.asyncio
async def test_outgoing_root_dm_target_rejects_wrong_peer():
    dc = _FakeDataClient()
    dc.add(
        "msg_root", thread_root_id=None, envelope_kind="dm",
        sender_slug="bob-0002", recipient_slug="agent-0001",
    )
    with pytest.raises(RuntimeError):
        await _resolve(dc, "msg_root", dm_peer="alice-0001")


@pytest.mark.asyncio
async def test_outgoing_root_dm_target_accepts_both_directions():
    dc = _FakeDataClient()
    dc.add(
        "msg_in", thread_root_id=None, envelope_kind="dm",
        sender_slug="alice-0001", recipient_slug="agent-0001",
    )
    dc.add(
        "msg_out", thread_root_id=None, envelope_kind="dm",
        sender_slug="agent-0001", recipient_slug="alice-0001",
    )
    for mid in ("msg_in", "msg_out"):
        resolved, note = await _resolve(dc, mid, dm_peer="alice-0001")
        assert resolved == mid and note == ""


@pytest.mark.asyncio
async def test_outgoing_root_cross_scope_rejects_before_system_wipe():
    """A system envelope from ANOTHER channel must reject (rule 3), not
    silently become a top-level post in the wrong channel (rule 1)."""
    dc = _FakeDataClient()
    dc.add(
        "intro-prompt-1", thread_root_id="intro-prompt-1",
        sender_slug="system", channel_id="ch_other",
    )
    with pytest.raises(RuntimeError):
        await _resolve(dc, "intro-prompt-1", channel_id="ch_x")


def _spy_encrypt_input(monkeypatch):
    """Capture the EncryptInput so tests can assert on the payload's
    thread_root_id at the daemon-owned send boundary."""
    import puffo_agent.agent.send_coordinator as coordinator_module

    captured: dict = {}
    real_with_key = coordinator_module.encrypt_message_with_content_key

    def spy_with_key(inp, signing_key, **kw):
        captured["inp"] = inp
        return real_with_key(inp, signing_key, **kw)

    monkeypatch.setattr(
        coordinator_module, "encrypt_message_with_content_key", spy_with_key,
    )
    return captured


def _seed_recipient(http, recipient_slug: str):
    recipient_kem = KemKeyPair.generate()
    http.responses[f"/certs/sync?slugs={recipient_slug}"] = {
        "entries": [{
            "seq": 1, "kind": "device_cert", "slug": recipient_slug,
            "cert": {
                "device_id": f"dev_{recipient_slug}",
                "kem_public_key": base64url_encode(
                    recipient_kem.public_key_bytes()
                ),
            },
        }],
        "has_more": False,
    }


async def _seed_channel(ms, http, channel_id: str, space_id: str,
                        recipient_slug: str):
    await ms.mark_channel_space(channel_id, space_id)
    http.responses[f"/spaces/{space_id}/channels/{channel_id}/members"] = {
        "members": [{"slug": recipient_slug, "role": "owner"}],
    }
    _seed_recipient(http, recipient_slug)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong_post_id, real_root_id, scenario",
    [
        # The two live failures we hit on 2026-05-18 with operator
        # mingvase-8795 — the clone-report send (post_id of the
        # operator's "please clone" message used as root_id) and
        # the build-test report send (post_id of the operator's
        # "ensure you can build/test" message used as root_id).
        ("msg_38364760-cd04-408a-9daf-aad66a2487fc",
         "msg_610fec10-122f-4fff-8dcb-498770809c84",
         "clone-report-live-failure"),
        ("msg_9e8f1a83-05ff-4775-8e07-b90999c61d53",
         "msg_610fec10-122f-4fff-8dcb-498770809c84",
         "build-test-report-live-failure"),
    ],
)
async def test_send_message_auto_corrects_real_live_failures(
    monkeypatch, wrong_post_id, real_root_id, scenario,
):
    """Each parameter is one of the two real failures we observed
    on 2026-05-18 — operator's post is the thread root, the message
    Calculation incorrectly passed as root_id is a reply in that
    same thread. After the fix the EncryptInput must carry the real
    root and the response must include the correction note."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    await ms.store({
        "envelope_id": real_root_id, "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "real root", "sent_at": _now_ms(),
        "thread_root_id": None,
    })
    await ms.store({
        "envelope_id": wrong_post_id, "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": f"reply in thread ({scenario})", "sent_at": _now_ms(),
        "thread_root_id": real_root_id,
    })
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc",
        "text": f"replaying {scenario}",
        "visibility_level": "default",
        "root_id": wrong_post_id,
    })

    assert "posted" in result
    assert "auto-corrected" in result
    assert wrong_post_id in result and real_root_id in result
    assert captured["inp"].thread_root_id == real_root_id


@pytest.mark.asyncio
async def test_send_message_keeps_real_root_id_unchanged(monkeypatch):
    """Happy path: agent passes a real root id; no correction note,
    EncryptInput's thread_root_id is the supplied id."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root post", "sent_at": _now_ms(),
        "thread_root_id": None,
    })
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc",
        "text": "correctly threaded reply",
        "visibility_level": "default",
        "root_id": "msg_root",
    })

    assert "posted" in result
    assert "auto-corrected" not in result
    assert "could not verify" not in result
    assert captured["inp"].thread_root_id == "msg_root"


@pytest.mark.asyncio
async def test_send_message_unknown_root_id_wiped_to_null_with_warning(monkeypatch):
    """PUF-227-A: strict cache-validation invariant. An unknown
    root_id (not in this agent's local store) gets WIPED to null
    before the envelope ships — the operator locked Q1(a) "client
    should only see thread_root_id that's in its local cache." The
    tool response carries a warning so the agent self-corrects on
    its next compose. Replaces PUF-200's "fall through with the
    original id" behavior, which was the permissive shape PUF-227-A
    explicitly overrides."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc",
        "text": "racing the inbound write",
        "visibility_level": "default",
        "root_id": "msg_never_seen",
    })

    assert "posted" in result
    assert "not in local cache" in result
    assert "wiped to null" in result or "sent as top-level" in result
    # PUF-227-A strict: invalid id wiped to None, NOT carried into
    # the payload.
    assert captured["inp"].thread_root_id is None


@pytest.mark.asyncio
async def test_send_message_root_level_send_skips_resolve(monkeypatch):
    """No root_id → no lookup attempted, EncryptInput's thread_root_id
    is None, no resolve-style note in the response."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc", "text": "top-level", "visibility_level": "human",
    })

    assert "posted" in result
    assert "auto-corrected" not in result
    assert "could not verify" not in result
    assert captured["inp"].thread_root_id is None


@pytest.mark.asyncio
async def test_send_message_with_attachments_auto_corrects_reply_as_root_id(
    monkeypatch, tmp_path,
):
    """Same auto-correction behaviour on the attachments path."""
    cfg, http, ms = _setup()
    cfg.workspace = tmp_path
    (tmp_path / "hello.txt").write_bytes(b"hello attachments")
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    http.responses["/blobs/upload"] = {"blob_id": "blob_xyz"}
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(),
        "thread_root_id": None,
    })
    await ms.store({
        "envelope_id": "msg_reply", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "reply", "sent_at": _now_ms(),
        "thread_root_id": "msg_root",
    })
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message_with_attachments", {
        "paths": ["hello.txt"],
        "channel": "ch_abc",
        "visibility_level": "default",
        "root_id": "msg_reply",
        "caption": "files",
    })

    assert "uploaded" in result
    assert "auto-corrected" in result
    # Display string reflects the *resolved* thread, not the wrong id.
    assert "auto-corrected to msg_root" in result
    assert captured["inp"].thread_root_id == "msg_root"


@pytest.mark.asyncio
async def test_core_tools_registered():
    cfg, _, _ = _setup()
    mcp = _build_tools(cfg)
    tool_names = {t.name for t in await mcp.list_tools()}
    expected = {
        "whoami", "send_message", "read_inbox", "read_history",
        "list_spaces", "list_channels_in_all_spaces",
        "list_channels_in_space", "list_channel_members",
        "get_user_info", "get_post", "send_message_with_attachments",
    }
    assert expected.issubset(tool_names)





# resolve_visibility — one entry point that combines level parsing,
# root-level coerce, DM/@-mention detection, and the per-level note
# wording.


class _VisHttp:
    """Stub for ``/identities/profiles?slugs=<csv>``."""

    def __init__(
        self,
        types: dict[str, str] | None = None,
        *,
        raise_error: bool = False,
    ):
        self.types = types or {}
        self.raise_error = raise_error
        self.calls: list[str] = []

    async def get(self, path: str):
        self.calls.append(path)
        if self.raise_error:
            raise RuntimeError("simulated transport failure")
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(path).query)
        slugs = (qs.get("slugs", [""])[0]).split(",") if qs.get("slugs") else []
        profiles = [
            {"slug": s, "identity_type": self.types.get(s, "human")}
            for s in slugs if s
        ]
        return {"profiles": profiles}


# ── level="human" ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_human_returns_visible_no_note_no_lookup():
    http = _VisHttp({"alice-1234": "human"})
    visible, note = await resolve_visibility(
        "human", "@alice-1234", "@alice-1234 hi", "msg_root", http,
    )
    assert visible is True
    assert note == ""
    assert http.calls == []


# ── level="default" ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_default_dm_coerces_and_nudges_human():
    http = _VisHttp()
    visible, note = await resolve_visibility(
        "default", "@alice-1234", "hi", "msg_root", http,
    )
    assert visible is True
    assert "sent visible" in note
    assert "DM" in note
    assert "'human'" in note
    assert http.calls == []


@pytest.mark.asyncio
async def test_resolve_default_mention_human_coerces():
    http = _VisHttp({"alice-1234": "human"})
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "@alice-1234 here's the answer", "msg_root", http,
    )
    assert visible is True
    assert "@-mentions a human" in note
    assert "'human'" in note
    assert http.calls and "alice-1234" in http.calls[0]


@pytest.mark.asyncio
async def test_resolve_default_mention_agent_only_stays_hidden_but_nudges():
    http = _VisHttp({"scout-5678": "agent"})
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "@scout-5678 pipeline done", "msg_root", http,
    )
    assert visible is False
    assert "sent hidden" in note
    assert "'human'" in note and "'agent_only'" in note


@pytest.mark.asyncio
async def test_resolve_default_no_signal_nudges_explicit():
    http = _VisHttp()
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "internal retry", "msg_root", http,
    )
    assert visible is False
    assert "sent hidden" in note
    assert "'human'" in note and "'agent_only'" in note


@pytest.mark.asyncio
async def test_resolve_default_root_level_always_coerces():
    """No root_id → can't fold → always sent visible regardless of
    DM / @-mention signals."""
    http = _VisHttp()
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "top-level chatter", "", http,
    )
    assert visible is True
    assert "root-level messages can't fold" in note
    assert http.calls == []


@pytest.mark.asyncio
async def test_resolve_default_mixed_mentions_any_human_wins():
    http = _VisHttp({"alice-1234": "human", "scout-5678": "agent"})
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "@scout-5678 @alice-1234 status", "msg_root", http,
    )
    assert visible is True
    assert "@-mentions a human" in note


@pytest.mark.asyncio
async def test_resolve_default_profile_error_soft_fails_to_hidden():
    """Transport error on profile fetch can't flip an intentional
    hidden send — nudge fires, no coerce."""
    http = _VisHttp({"alice-1234": "human"}, raise_error=True)
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "@alice-1234 hi", "msg_root", http,
    )
    assert visible is False
    assert "sent hidden" in note


@pytest.mark.asyncio
async def test_resolve_default_email_not_mistaken_for_mention():
    http = _VisHttp({"alice-1234": "human"})
    visible, note = await resolve_visibility(
        "default", "ch_abcd", "see contact@alice-1234 for details",
        "msg_root", http,
    )
    assert visible is False
    assert "sent hidden" in note
    assert http.calls == []


# ── level="agent_only" ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_agent_only_dm_stays_hidden_but_warns():
    http = _VisHttp()
    visible, note = await resolve_visibility(
        "agent_only", "@alice-1234", "hi", "msg_root", http,
    )
    assert visible is False
    assert "sent hidden per" in note
    assert "DM" in note
    assert "Double-check" in note


@pytest.mark.asyncio
async def test_resolve_agent_only_mention_human_stays_hidden_but_warns():
    http = _VisHttp({"alice-1234": "human"})
    visible, note = await resolve_visibility(
        "agent_only", "ch_abcd", "@alice-1234 fyi", "msg_root", http,
    )
    assert visible is False
    assert "@-mentions a human" in note
    assert "Double-check" in note


@pytest.mark.asyncio
async def test_resolve_agent_only_mention_agent_no_note():
    http = _VisHttp({"scout-5678": "agent"})
    visible, note = await resolve_visibility(
        "agent_only", "ch_abcd", "@scout-5678 done", "msg_root", http,
    )
    assert visible is False
    assert note == ""


@pytest.mark.asyncio
async def test_resolve_agent_only_root_level_still_coerces():
    """agent_only doesn't override the root-level constraint — the UI
    can't fold root-level so it goes out visible."""
    http = _VisHttp()
    visible, note = await resolve_visibility(
        "agent_only", "ch_abcd", "top-level", "", http,
    )
    assert visible is True
    assert "root-level messages can't fold" in note


# ── validation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_rejects_unknown_level():
    http = _VisHttp()
    with pytest.raises(RuntimeError, match="visibility_level"):
        await resolve_visibility("visible", "ch_x", "hi", "msg_root", http)
    with pytest.raises(RuntimeError):
        await resolve_visibility("", "ch_x", "hi", "msg_root", http)


async def _store_msg(ms, eid, *, is_encrypted, channel_id="ch_1", thread_root_id=None):
    await ms.store({
        "envelope_id": eid,
        "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "channel_id": channel_id,
        "space_id": "sp_test",
        "content_type": "text/plain",
        "content": f"body {eid}",
        "sent_at": _now_ms(),
        "thread_root_id": thread_root_id,
        "is_encrypted": is_encrypted,
    })


@pytest.mark.asyncio
async def test_get_post_shows_is_encrypted():
    cfg, _, ms = _setup()
    await _store_msg(ms, "msg_plain", is_encrypted=False)
    result = await _call(_build_tools(cfg), "get_post", {"post_ref": "msg_plain"})
    assert "encrypted=false" in result


@pytest.mark.asyncio
async def test_read_history_channel_tags_encryption():
    cfg, _, ms = _setup()
    await _store_msg(ms, "msg_enc", is_encrypted=True)
    await _store_msg(ms, "msg_plain", is_encrypted=False)
    args = {"target": "channel:sp_test:ch_1"}
    result = await _call(_build_tools(cfg), "read_history", args)
    assert "encrypted=true" in result and "encrypted=false" in result
    page = await _call(
        _build_tools(cfg), "read_history", {**args, "limit": 1}
    )
    assert 'returned_count=1 has_older=true has_newer=false' in page


@pytest.mark.asyncio
async def test_read_history_thread_tags_encryption():
    cfg, _, ms = _setup()
    await _store_msg(ms, "msg_root", is_encrypted=True)
    await _store_msg(ms, "msg_reply", is_encrypted=False, thread_root_id="msg_root")
    result = await _call(_build_tools(cfg), "read_history", {
        "target": "channel:sp_test:ch_1:thread:msg_root",
    })
    assert "encrypted=false" in result


@pytest.mark.asyncio
async def test_read_history_presents_root_and_replies_under_one_thread_target():
    cfg, _, ms = _setup()
    await _store_msg(ms, "msg_root", is_encrypted=True)
    await _store_msg(ms, "msg_reply", is_encrypted=False, thread_root_id="msg_root")
    root = await ms.get_message_by_envelope("msg_root")
    assert root is not None and not root.thread_root_id
    result = await _call(_build_tools(cfg), "read_history", {
        "target": "channel:sp_test:ch_1:thread:msg_root",
    })
    header = 'target_ref="channel:sp_test:ch_1:thread:msg_root"'
    assert result.count(header) == 1
    assert 'message_id="msg_root"' in result
    assert 'message_id="msg_reply"' in result

# Send-side thread-root rules, end to end through the send_message tool.


@pytest.mark.asyncio
async def test_send_message_system_root_ships_as_new_top_level(monkeypatch):
    """Replying to a daemon-minted intro nudge (self-referencing system
    envelope, no server row) ships as a new top-level message."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    _seed_recipient(http, "alice-0001")
    await ms.store({
        "envelope_id": "intro-prompt-xyz", "envelope_kind": "channel",
        "sender_slug": "system", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "welcome!", "sent_at": _now_ms(),
        "thread_root_id": "intro-prompt-xyz",
    })
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc",
        "text": "hello, thanks for the intro",
        "visibility_level": "human",
        "root_id": "intro-prompt-xyz",
    })

    assert "posted" in result
    assert "system message" in result
    assert captured["inp"].thread_root_id is None


@pytest.mark.asyncio
async def test_send_message_cross_channel_root_rejected():
    """A root from another channel rejects the send outright — the
    agent gets a correctable error instead of a misfiled message."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    _seed_recipient(http, "alice-0001")
    await ms.store({
        "envelope_id": "msg_elsewhere", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_OTHER",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(),
        "thread_root_id": None,
    })

    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(mcp, "send_message", {
            "channel": "ch_abc",
            "text": "misdirected reply",
            "root_id": "msg_elsewhere",
        })
    assert "ch_OTHER" in str(excinfo.value)
    assert "ch_abc" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_message_wiped_root_forces_visible_as_root_level(monkeypatch):
    """Ordering contract: root resolution runs BEFORE the visibility
    floors. A wiped root makes the message root-level, which cannot fold
    in the UI — 'default' must coerce it visible instead of shipping an
    invisible hidden top-level post."""
    cfg, http, ms = _setup()
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    _seed_recipient(http, "alice-0001")
    captured = _spy_encrypt_input(monkeypatch)

    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc",
        "text": "agent chatter",
        "visibility_level": "default",
        "root_id": "msg_never_recorded",
    })

    assert "posted" in result
    assert "not in local cache" in result
    assert captured["inp"].thread_root_id is None
    assert captured["inp"].is_visible_to_human is True

@pytest.mark.asyncio
async def test_send_message_dm_rejects_channel_root():
    """DM sends scope-check too: a channel message as root rejects."""
    cfg, http, ms = _setup()
    _seed_recipient(http, "alice-0001")
    http.responses["/certs/sync?slugs=alice-0001"] = (
        http.responses["/certs/sync?slugs=alice-0001"]
    )
    await ms.store({
        "envelope_id": "msg_channel_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(),
        "thread_root_id": None,
    })

    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(mcp, "send_message", {
            "channel": "@alice-0001",
            "text": "dm reply",
            "root_id": "msg_channel_root",
        })
    assert "DM" in str(excinfo.value)
    assert "alice-0001" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_message_with_attachments_cross_channel_root_rejected(tmp_path):
    """The attachments send path wires the same scope rejection."""
    cfg, http, ms = _setup()
    cfg.workspace = tmp_path
    (tmp_path / "hello.txt").write_bytes(b"hello attachments")
    await _seed_channel(ms, http, "ch_abc", "sp_test", "alice-0001")
    _seed_recipient(http, "alice-0001")
    http.responses["/blobs/upload"] = {"blob_id": "blob_xyz"}
    await ms.store({
        "envelope_id": "msg_elsewhere", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_OTHER",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(),
        "thread_root_id": None,
    })

    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as excinfo:
        await _call(mcp, "send_message_with_attachments", {
            "channel": "ch_abc",
            "caption": "misdirected attachment reply",
            "paths": ["hello.txt"],
            "root_id": "msg_elsewhere",
        })
    assert "ch_OTHER" in str(excinfo.value)


# ── F4 / F5: keyless-transport send preconditions ───────────────────
#
# Under the T23 keyless transport the two send tools POST plaintext to
# ``/v2/cloud-agents/messages`` (blobs to ``/v2/cloud-agents/blobs/
# upload``) instead of driving the bridge WS. F4/F5 semantics are
# preserved on that HTTP seam:
#
# F4: reply_to_id must be dropped when _validate_root_same_channel wipes
#     the resolved root (no dangling parent ref).
# F5: send_message_with_attachments must run EVERY precondition
#     (destination resolve, root validate, all per-file size checks)
#     before the first blob upload, so a rejected route or an oversized
#     later file raises with no orphaned blobs.


class _RecordingBridge:
    """A bridge stub used only to PROVE the keyless send path bypasses
    the WS: if it's ever touched (``sent``/``uploaded`` non-empty) the
    keyless branch wrongly fell back to the bridge."""

    def __init__(self):
        self.sent: list[dict] = []
        self.uploaded: list[bytes] = []
        self._seq = 0

    async def upload_blob(self, data: bytes) -> dict:
        self._seq += 1
        self.uploaded.append(data)
        return {"blob_id": f"blob_{self._seq:04d}", "size_bytes": len(data)}

    async def send_send(self, *, plaintext, recipient_slug=None,
                        space_id=None, channel_id=None, reply_to_id=None,
                        thread_root_id=None, attachments=None,
                        timeout: float = 30.0) -> dict:
        self.sent.append({
            "plaintext": plaintext, "recipient_slug": recipient_slug,
            "space_id": space_id, "channel_id": channel_id,
            "reply_to_id": reply_to_id, "thread_root_id": thread_root_id,
            "attachments": attachments,
        })
        return {"type": "ack", "envelope_id": "msg_rec"}


def _keyless_ws_setup(bridge=None):
    """A keyless tools config with a workspace dir so the send tools'
    attachments path runs. An optional ``bridge`` is attached only to
    prove the keyless branch never touches it. Returns
    ``(cfg, http, ms, workspace_dir)``."""
    cfg, http, ms = _setup_keyless()
    ws = tempfile.mkdtemp()
    cfg.workspace = ws
    if bridge is not None:
        cfg.bridge_client = bridge
    return cfg, http, ms, ws


def _write_ws_file(ws: str, name: str, data: bytes = b"x") -> str:
    from pathlib import Path
    (Path(ws) / name).write_bytes(data)
    return name


@pytest.mark.asyncio
async def test_f4_keyless_reply_to_dropped_when_root_wiped():
    """An unknown root_id gets wiped by _validate_root_same_channel; the
    keyless send body must carry NEITHER thread_root_id NOR reply_to_id
    (F4: no dangling parent ref)."""
    cfg, http, ms, _ = _keyless_ws_setup()
    await ms.mark_channel_space("ch_abc", "sp_test")
    mcp = _build_tools(cfg)

    result = await _call(mcp, "send_message", {
        "channel": "ch_abc", "text": "reply", "root_id": "msg_never_seen",
    })

    sends = _keyless_sends(http)
    assert len(sends) == 1
    body = sends[0]
    assert "thread_root_id" not in body
    assert "reply_to_id" not in body  # F4: dropped alongside the wiped root
    assert "posted" in result


@pytest.mark.asyncio
async def test_f4_keyless_reply_to_kept_when_root_valid():
    """A valid same-channel root is preserved: both thread_root_id and
    reply_to_id ride the keyless send body."""
    cfg, http, ms, _ = _keyless_ws_setup()
    await ms.mark_channel_space("ch_abc", "sp_test")
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root post", "sent_at": _now_ms(),
        "thread_root_id": None,
    })
    mcp = _build_tools(cfg)

    result = await _call(mcp, "send_message", {
        "channel": "ch_abc", "text": "reply", "root_id": "msg_root",
    })

    body = _keyless_sends(http)[0]
    assert body["thread_root_id"] == "msg_root"
    assert body["reply_to_id"] == "msg_root"
    assert "posted" in result


@pytest.mark.asyncio
async def test_f4_keyless_attachments_reply_to_dropped_when_root_wiped():
    """The same F4 gate applies to the keyless attachments send path."""
    cfg, http, ms, ws = _keyless_ws_setup()
    await ms.mark_channel_space("ch_abc", "sp_test")
    _write_ws_file(ws, "a.txt", b"aaa")
    mcp = _build_tools(cfg)

    await _call(mcp, "send_message_with_attachments", {
        "paths": ["a.txt"], "channel": "ch_abc", "caption": "cap",
        "root_id": "msg_never_seen",
    })

    body = _keyless_sends(http)[0]
    assert "thread_root_id" not in body
    assert "reply_to_id" not in body


@pytest.mark.asyncio
async def test_f5_keyless_attachments_bare_at_dm_raises_before_upload():
    """A bare ``@`` destination is rejected before any blob is uploaded."""
    cfg, http, ms, ws = _keyless_ws_setup()
    _write_ws_file(ws, "a.txt", b"aaa")
    mcp = _build_tools(cfg)

    with pytest.raises(Exception) as exc:
        await _call(mcp, "send_message_with_attachments", {
            "paths": ["a.txt"], "channel": "@", "caption": "x",
        })
    assert "DM recipient" in str(exc.value)
    assert http.uploaded == []  # F5: no orphaned blobs


@pytest.mark.asyncio
async def test_f5_keyless_attachments_stale_channel_raises_before_upload():
    """A stale/unknown ``ch_`` id (not in the cache) raises via
    _resolve_channel_space before any upload."""
    cfg, http, ms, ws = _keyless_ws_setup()
    _write_ws_file(ws, "a.txt", b"aaa")
    mcp = _build_tools(cfg)

    with pytest.raises(Exception) as exc:
        await _call(mcp, "send_message_with_attachments", {
            "paths": ["a.txt"], "channel": "ch_stale", "caption": "x",
        })
    assert "no record of channel" in str(exc.value)
    assert http.uploaded == []
    await ms.close()


@pytest.mark.asyncio
async def test_f5_keyless_attachments_oversized_later_file_orphans_nothing():
    """An oversized SECOND file makes the whole send raise before ANY
    upload — the earlier valid file must not be orphaned on the server."""
    cfg, http, ms, ws = _keyless_ws_setup()
    await ms.mark_channel_space("ch_abc", "sp_test")
    _write_ws_file(ws, "small.txt", b"small")
    _write_ws_file(ws, "big.bin", b"x" * (8 * 1024 * 1024 + 1))
    mcp = _build_tools(cfg)

    with pytest.raises(Exception) as exc:
        await _call(mcp, "send_message_with_attachments", {
            "paths": ["small.txt", "big.bin"], "channel": "ch_abc",
            "caption": "x",
        })
    assert "8 MiB" in str(exc.value)
    assert http.uploaded == []  # F5: the earlier small file wasn't uploaded
    await ms.close()


@pytest.mark.asyncio
async def test_f5_keyless_attachments_happy_path_uploads_all_and_sends_once():
    """The valid multi-file keyless path uploads every file via
    ``post_bytes_unsigned`` then issues exactly one
    ``post_unsigned`` carrying all blob refs."""
    cfg, http, ms, ws = _keyless_ws_setup()
    await ms.mark_channel_space("ch_abc", "sp_test")
    _write_ws_file(ws, "a.txt", b"aaa")
    _write_ws_file(ws, "b.txt", b"bbbb")
    mcp = _build_tools(cfg)

    result = await _call(mcp, "send_message_with_attachments", {
        "paths": ["a.txt", "b.txt"], "channel": "ch_abc", "caption": "hi",
    })

    # Two unsigned blob uploads, in order, then one unsigned message send.
    upload_paths = [p for m, p, _ in http.calls if m == "POST_BYTES_UNSIGNED"]
    assert upload_paths == [
        "/v2/cloud-agents/blobs/upload", "/v2/cloud-agents/blobs/upload",
    ]
    assert http.uploaded == [b"aaa", b"bbbb"]
    sends = _keyless_sends(http)
    assert len(sends) == 1
    body = sends[0]
    assert body["space_id"] == "sp_test"
    assert body["channel_id"] == "ch_abc"
    assert body["plaintext"] == "hi"
    assert [r["filename"] for r in body["attachments"]] == ["a.txt", "b.txt"]
    assert [r["blob_id"] for r in body["attachments"]] == ["blob_0001", "blob_0002"]
    assert "msg_keyless" in result


# ── keyless reads → /v2/cloud-agents/* (unsigned) ───────────────────


@pytest.mark.asyncio
async def test_keyless_list_spaces_hits_cloud_agents_route():
    cfg, http, ms = _setup_keyless()
    http.responses["/v2/cloud-agents/spaces"] = {
        "spaces": [{"space_id": "sp_team", "name": "Team"}],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_spaces")
    assert "sp_team" in result and "Team" in result
    assert ("GET_UNSIGNED", "/v2/cloud-agents/spaces", None) in http.calls


@pytest.mark.asyncio
async def test_keyless_list_channels_in_space_hits_cloud_agents_route():
    cfg, http, ms = _setup_keyless()
    http.responses["/v2/cloud-agents/spaces/sp_target/channels"] = {
        "channels": [{"channel_id": "ch_g", "name": "general"}],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels_in_space", {"space_id": "sp_target"})
    assert "ch_g" in result and "general" in result
    assert (
        "GET_UNSIGNED", "/v2/cloud-agents/spaces/sp_target/channels", None,
    ) in http.calls


@pytest.mark.asyncio
async def test_keyless_directory_path_encodes_model_selected_space_segment():
    """A space id containing slashes cannot retarget the unsigned route."""
    cfg, http, _ = _setup_keyless()
    encoded = "/v2/cloud-agents/spaces/sp_a%2F..%2Fidentities/channels"
    http.responses[encoded] = {"channels": []}
    mcp = _build_tools(cfg)
    result = await _call_structured(
        mcp,
        "list_channels_in_space",
        {"space_id": "sp_a/../identities"},
    )
    assert result["count"] == 0
    assert ("GET_UNSIGNED", encoded, None) in http.calls


@pytest.mark.asyncio
async def test_keyless_list_channels_in_all_spaces_hits_cloud_agents_routes():
    cfg, http, ms = _setup_keyless()
    http.responses["/v2/cloud-agents/spaces"] = {
        "spaces": [{"space_id": "sp_a", "name": "A"}],
    }
    http.responses["/v2/cloud-agents/spaces/sp_a/channels"] = {
        "channels": [{"channel_id": "ch_x", "name": "general"}],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "list_channels_in_all_spaces")
    assert "sp_a" in result and "ch_x" in result
    assert ("GET_UNSIGNED", "/v2/cloud-agents/spaces", None) in http.calls
    assert (
        "GET_UNSIGNED", "/v2/cloud-agents/spaces/sp_a/channels", None,
    ) in http.calls


@pytest.mark.asyncio
async def test_keyless_list_channel_members_uses_private_channel_roster():
    """Keyless reads the exact private-channel roster, not the space roster."""
    cfg, http, ms = _setup_keyless()
    await ms.mark_channel_space("ch_abc", "sp_test")
    http.responses["/v2/cloud-agents/spaces/sp_test/channels/ch_abc/members"] = {
        "members": [
            {
                "slug": "alice-0001",
                "role": "owner",
                "identity_type": "human",
                "owner_slug": None,
            },
            {
                "slug": "agent-0001",
                "role": "member",
                "identity_type": "agent",
                "owner_slug": "alice-0001",
            },
        ],
    }
    mcp = _build_tools(cfg)
    result = await _call_structured(
        mcp, "list_channel_members", {"channel": "ch_abc"},
    )
    members = {member["identity"]: member for member in result["members"]}
    assert members["@alice-0001"]["role"] == "owner"
    assert members["@alice-0001"]["identity_type"] == "human"
    assert members["@agent-0001"]["role"] == "member"
    assert members["@agent-0001"]["owner_identity"] == "@alice-0001"
    assert "online" not in members["@agent-0001"]
    assert (
        "GET_UNSIGNED", "/v2/cloud-agents/spaces/sp_test/channels/ch_abc/members", None,
    ) in http.calls
    assert not any(p.endswith("/spaces/sp_test/members") for _, p, _ in http.calls)


@pytest.mark.asyncio
async def test_keyless_get_user_info_hits_cloud_agents_route():
    cfg, http, ms = _setup_keyless()
    http.responses["/v2/cloud-agents/identities/profiles?slugs=alice-0001"] = {
        "profiles": [{
            "slug": "alice-0001", "display_name": "Alice", "bio": "A user",
        }],
    }
    mcp = _build_tools(cfg)
    result = await _call(mcp, "get_user_info", {"username": "@alice-0001"})
    assert "alice-0001" in result and "Alice" in result and "A user" in result
    assert (
        "GET_UNSIGNED",
        "/v2/cloud-agents/identities/profiles?slugs=alice-0001",
        None,
    ) in http.calls


# ── keyless whoami: no keystore ─────────────────────────────────────


@pytest.mark.asyncio
async def test_keyless_whoami_needs_no_keystore():
    """Keyless whoami builds identity from cfg + resolves display_name
    over the unsigned profiles route, never loading the keystore."""
    cfg, http, ms = _setup_keyless()
    spy = _SpyKeyStore()
    cfg.keystore = spy
    http.responses["/v2/cloud-agents/identities/profiles?slugs=agent-0001"] = {
        "profiles": [{"slug": "agent-0001", "display_name": "Cloud Bot"}],
    }
    mcp = _build_tools(cfg)
    result = await _call_structured(mcp, "whoami")
    assert result["identity"]["display_name"] == "Cloud Bot"
    assert result["identity"]["identity"] == "@agent-0001"
    assert result["runtime"]["device_id"] == "dev_test"
    assert result["runtime"]["server_url"] == "http://sandbox.local"
    assert result["runtime"]["subkey_management"] == "server"
    assert spy.loads == []                     # keystore never touched


# ── keyless send_message: unsigned POST, no bridge ──────────────────


@pytest.mark.asyncio
async def test_keyless_send_message_dm_posts_unsigned():
    cfg, http, ms = _setup_keyless()
    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "@alice-0001", "text": "hi there",
    })
    assert "posted" in result
    sends = [(p, b) for m, p, b in http.calls if m == "POST_UNSIGNED"]
    assert len(sends) == 1
    path, body = sends[0]
    assert path == "/v2/cloud-agents/messages"
    assert body == {"plaintext": "hi there", "recipient_slug": "alice-0001", "is_visible_to_human": True}


@pytest.mark.asyncio
async def test_keyless_send_message_channel_posts_unsigned():
    cfg, http, ms = _setup_keyless()
    await ms.mark_channel_space("ch_abc", "sp_test")
    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc", "text": "hello channel",
    })
    assert "posted" in result
    sends = [(p, b) for m, p, b in http.calls if m == "POST_UNSIGNED"]
    assert len(sends) == 1
    path, body = sends[0]
    assert path == "/v2/cloud-agents/agent-runtime/messages:send"
    assert body["plaintext"] == "hello channel"
    assert body["space_id"] == "sp_test"
    assert body["channel_id"] == "ch_abc"
    assert isinstance(body["client_ref"], str) and body["client_ref"]
    assert "client_request_id" not in body
    assert body["freshness"] == {
        "context_baseline_seq": None,
        "seen_seq": 0,
        "mode": "require_current",
    }


@pytest.mark.asyncio
async def test_keyless_send_message_channel_threaded_carries_ids():
    cfg, http, ms = _setup_keyless()
    await ms.mark_channel_space("ch_abc", "sp_test")
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(), "thread_root_id": None,
    })
    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "ch_abc", "text": "reply", "root_id": "msg_root",
    })
    assert "posted" in result
    body = _keyless_sends(http)[0]
    assert body["space_id"] == "sp_test"
    assert body["channel_id"] == "ch_abc"
    assert body["thread_root_id"] == "msg_root"
    assert body["reply_to_id"] == "msg_root"


@pytest.mark.asyncio
async def test_keyless_send_message_returns_ack_envelope_id():
    cfg, http, ms = _setup_keyless()
    http.responses["/v2/cloud-agents/messages"] = {"envelope_id": "msg_ack99"}
    mcp = _build_tools(cfg)
    result = await _call(mcp, "send_message", {
        "channel": "@alice-0001", "text": "hi",
    })
    assert "msg_ack99" in result


@pytest.mark.asyncio
async def test_keyless_send_message_bypasses_bridge():
    """A bridge is present but the keyless branch must POST over HTTP and
    make ZERO bridge send_send calls."""
    bridge = _RecordingBridge()
    cfg, http, ms, _ = _keyless_ws_setup(bridge)
    mcp = _build_tools(cfg)
    await _call(mcp, "send_message", {"channel": "@bob-0001", "text": "yo"})
    assert bridge.sent == []
    assert bridge.uploaded == []
    assert _keyless_sends(http) == [
        {"plaintext": "yo", "recipient_slug": "bob-0001", "is_visible_to_human": True},
    ]


@pytest.mark.asyncio
async def test_keyless_attachments_bypasses_bridge():
    """Keyless attachments upload via post_bytes_unsigned and never touch
    the bridge's upload_blob/send_send."""
    bridge = _RecordingBridge()
    cfg, http, ms, ws = _keyless_ws_setup(bridge)
    await ms.mark_channel_space("ch_abc", "sp_test")
    _write_ws_file(ws, "a.txt", b"aaa")
    mcp = _build_tools(cfg)
    await _call(mcp, "send_message_with_attachments", {
        "paths": ["a.txt"], "channel": "ch_abc", "caption": "cap",
    })
    assert bridge.sent == []
    assert bridge.uploaded == []
    assert http.uploaded == [b"aaa"]
    assert len(_keyless_sends(http)) == 1
    assert _keyless_sends(http)[0]["client_ref"]
    assert "client_request_id" not in _keyless_sends(http)[0]


# ── build_server(transport="bridge") is keyless-self-sufficient ─────


def test_build_server_bridge_transport_is_keyless(tmp_path, monkeypatch):
    """The subprocess server built with ``transport="bridge"`` gives its
    ``PuffoCoreHttpClient`` ``keyless=True`` and keeps ``bridge_client``
    None (outbound is HTTP, not WS)."""
    import puffo_agent.mcp.puffo_core_server as pcs

    captured = {}
    real = pcs.PuffoCoreHttpClient

    def spy(server_url, ks, slug, keyless=False):
        client = real(server_url, ks, slug, keyless=keyless)
        captured["client"] = client
        return client

    monkeypatch.setattr(pcs, "PuffoCoreHttpClient", spy)
    server = pcs.build_server(
        slug="bot-0001", device_id="dev_test", server_url="http://127.0.0.1:1",
        space_id="", keystore_dir="", workspace=str(tmp_path),
        agent_id="bot-0001", data_service_url="http://127.0.0.1:1",
        transport="bridge",
    )
    from mcp.server.fastmcp import FastMCP
    assert isinstance(server, FastMCP)
    assert captured["client"].keyless is True


def test_build_server_native_transport_is_not_keyless(tmp_path, monkeypatch):
    """A non-bridge build keeps the signed path — ``keyless`` is False."""
    import puffo_agent.mcp.puffo_core_server as pcs

    captured = {}
    real = pcs.PuffoCoreHttpClient

    def spy(server_url, ks, slug, keyless=False):
        client = real(server_url, ks, slug, keyless=keyless)
        captured["client"] = client
        return client

    monkeypatch.setattr(pcs, "PuffoCoreHttpClient", spy)
    pcs.build_server(
        slug="bot-0001", device_id="dev_test", server_url="http://127.0.0.1:1",
        space_id="", keystore_dir=str(tmp_path / "keys"),
        workspace=str(tmp_path), agent_id="bot-0001",
        data_service_url="http://127.0.0.1:1",
    )
    assert captured["client"].keyless is False
