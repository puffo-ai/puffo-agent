"""Sticky-note MCP tools: add_note / get_channel_notes / get_thread_notes."""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.mcp.core_note_tools import (
    NOTE_PRESETS,
    _format_note,
    _parse_note,
)

from tests.test_puffo_core_tools import _build_tools, _call, _setup


def _now_ms():
    return int(time.time() * 1000)


# ---- wire format ----------------------------------------------------------


def test_format_and_parse_note_roundtrip():
    color, label = NOTE_PRESETS["waiting"]
    text = _format_note(color, label, "review PR #238", ["bob-0002", "carol-3"])
    assert text.splitlines()[0] == "/note "
    parsed = _parse_note(text)
    assert parsed["label"] == "Waiting"
    assert parsed["message"] == "review PR #238"
    assert parsed["mentions"] == ["bob-0002", "carol-3"]


def test_parse_note_rejects_non_note():
    assert _parse_note("hello world") is None
    # ``/note`` buried mid-message is not a note.
    assert _parse_note("hi\n/note\nlabel: X") is None


def test_parse_note_requires_the_marker_space():
    assert _parse_note("/note\ncolor: #fff") is None
    assert _parse_note("/notebook entry") is None
    assert _parse_note("/note hello") is not None


def test_format_note_omits_empty_message_and_mentions():
    text = _format_note("#c9f748", "Complete", "", [])
    assert text == "/note \ncolor: #c9f748\nlabel: Complete"


# ---- add_note -------------------------------------------------------------


async def _seed_note_root(ms, http):
    """Root post in ch_abc + the send-path plumbing add_note needs."""
    from puffo_agent.crypto.encoding import base64url_encode
    from puffo_agent.crypto.primitives import KemKeyPair

    recipient_kem = KemKeyPair.generate()
    await ms.mark_channel_space("ch_abc", "sp_test")
    await ms.store({
        "envelope_id": "msg_root",
        "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "channel_id": "ch_abc",
        "space_id": "sp_test",
        "content_type": "text/plain",
        "content": "root post",
        "sent_at": _now_ms(),
    })
    http.responses["/spaces/sp_test/channels/ch_abc/members"] = {
        "members": [{"slug": "alice-0001", "role": "owner"}],
    }
    http.responses["/certs/sync?slugs=alice-0001"] = {
        "entries": [{
            "seq": 1, "kind": "device_cert", "slug": "alice-0001",
            "cert": {
                "device_id": "dev_recipient_1",
                "kem_public_key": base64url_encode(recipient_kem.public_key_bytes()),
            },
        }],
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_add_note_posts_into_thread():
    cfg, http, ms = _setup()
    await _seed_note_root(ms, http)
    mcp = _build_tools(cfg)
    result = await _call(mcp, "add_note", {
        "root_id": "msg_root", "preset": "waiting",
        "message": "review PR", "mentions": ["bob-0002"],
    })
    assert "posted" in result
    post_calls = [(p, b) for m, p, b in http.calls if m == "POST"]
    assert len(post_calls) == 1
    path, body = post_calls[0]
    assert path == "/v2/agent-runtime/messages:send"
    # add_note resolves the channel from the root and reuses the send
    # path; thread-root threading is covered by the send_message tests.
    assert body["envelope"]["channel_id"] == "ch_abc"


@pytest.mark.asyncio
async def test_add_note_rejects_bad_preset():
    cfg, http, ms = _setup()
    await _seed_note_root(ms, http)
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc:
        await _call(mcp, "add_note", {"root_id": "msg_root", "preset": "bogus"})
    assert "preset" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_add_note_unknown_root_errors():
    cfg, http, ms = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc:
        await _call(mcp, "add_note", {"root_id": "msg_nope", "preset": "waiting"})
    assert "root" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_add_note_processing_rejects_mentions():
    cfg, http, ms = _setup()
    await _seed_note_root(ms, http)
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc:
        await _call(mcp, "add_note", {
            "root_id": "msg_root", "preset": "processing",
            "mentions": ["bob-0002"],
        })
    assert "self-report" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_add_note_processing_without_mentions_posts():
    cfg, http, ms = _setup()
    await _seed_note_root(ms, http)
    mcp = _build_tools(cfg)
    result = await _call(mcp, "add_note", {
        "root_id": "msg_root", "preset": "processing", "message": "on it",
    })
    assert "posted" in result


@pytest.mark.asyncio
async def test_add_note_custom_color_posts():
    cfg, http, ms = _setup()
    await _seed_note_root(ms, http)
    mcp = _build_tools(cfg)
    result = await _call(mcp, "add_note", {
        "root_id": "msg_root", "color": "#38bdf8", "label": "Blocked",
        "message": "waiting on infra", "mentions": ["bob-0002"],
    })
    assert "posted" in result


@pytest.mark.asyncio
async def test_add_note_preset_and_color_conflict():
    cfg, http, ms = _setup()
    await _seed_note_root(ms, http)
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc:
        await _call(mcp, "add_note", {
            "root_id": "msg_root", "preset": "waiting", "color": "#38bdf8",
        })
    assert "mutually exclusive" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_add_note_custom_color_requires_label():
    cfg, http, ms = _setup()
    await _seed_note_root(ms, http)
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc:
        await _call(mcp, "add_note", {"root_id": "msg_root", "color": "#38bdf8"})
    assert "label" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_add_note_label_without_color_errors():
    cfg, http, ms = _setup()
    await _seed_note_root(ms, http)
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc:
        await _call(mcp, "add_note", {"root_id": "msg_root", "label": "Blocked"})
    assert "label" in str(exc.value).lower()


# ---- note read tools ------------------------------------------------------


@pytest.mark.asyncio
async def test_get_channel_notes_tool_formats_active_note():
    cfg, http, ms = _setup()
    base = _now_ms()
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": base,
    })
    await ms.store({
        "envelope_id": "msg_note", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": (
            "/note \ncolor: #db4cac\nlabel: Waiting\n"
            "message: do it\nmentions: @bob-0002"
        ),
        "sent_at": base + 100, "thread_root_id": "msg_root",
    })
    mcp = _build_tools(cfg)
    out = await _call(mcp, "get_channel_notes", {"channel": "ch_abc"})
    assert "note:msg_note" in out
    assert "[Waiting]" in out
    assert "for @bob-0002" in out


@pytest.mark.asyncio
async def test_get_thread_notes_tool_limit_one():
    cfg, http, ms = _setup()
    base = _now_ms()
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": base,
    })
    for i, label in enumerate(["Waiting", "Complete"]):
        await ms.store({
            "envelope_id": f"msg_note_{i}", "envelope_kind": "channel",
            "sender_slug": "alice-0001", "channel_id": "ch_abc",
            "space_id": "sp_test", "content_type": "text/plain",
            "content": f"/note \ncolor: #db4cac\nlabel: {label}",
            "sent_at": base + 100 + i, "thread_root_id": "msg_root",
        })
    mcp = _build_tools(cfg)
    out = await _call(mcp, "get_thread_notes", {"root_id": "msg_root", "limit": 1})
    assert "msg_note_1" in out       # Complete is newest → in effect
    assert "msg_note_0" not in out


# ---- read-tool edge branches ----------------------------------------------


@pytest.mark.asyncio
async def test_get_channel_notes_rejects_hash_addressing():
    cfg, http, ms = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc:
        await _call(mcp, "get_channel_notes", {"channel": "#general"})
    assert "channel id" in str(exc.value)


@pytest.mark.asyncio
async def test_get_channel_notes_non_channel_ref_resolves_loudly():
    cfg, http, ms = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc:
        await _call(mcp, "get_channel_notes", {"channel": "some-slug"})
    assert "not a channel id" in str(exc.value)


@pytest.mark.asyncio
async def test_get_channel_notes_unknown_channel_reports():
    cfg, http, ms = _setup()
    mcp = _build_tools(cfg)
    out = await _call(mcp, "get_channel_notes", {"channel": "ch_missing"})
    assert "no such channel" in out


@pytest.mark.asyncio
async def test_get_channel_notes_empty_channel_reports():
    cfg, http, ms = _setup()
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(),
    })
    mcp = _build_tools(cfg)
    out = await _call(mcp, "get_channel_notes", {"channel": "ch_abc"})
    assert "no active notes" in out


@pytest.mark.asyncio
async def test_get_thread_notes_requires_root_id():
    cfg, http, ms = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc:
        await _call(mcp, "get_thread_notes", {"root_id": "  "})
    assert "root_id" in str(exc.value)


@pytest.mark.asyncio
async def test_get_thread_notes_unknown_root_reports():
    cfg, http, ms = _setup()
    mcp = _build_tools(cfg)
    out = await _call(mcp, "get_thread_notes", {"root_id": "msg_missing"})
    assert "no such thread" in out


@pytest.mark.asyncio
async def test_get_thread_notes_empty_thread_reports():
    cfg, http, ms = _setup()
    await ms.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content_type": "text/plain",
        "content": "root", "sent_at": _now_ms(),
    })
    mcp = _build_tools(cfg)
    out = await _call(mcp, "get_thread_notes", {"root_id": "msg_root"})
    assert "no notes on this thread" in out


# ---- add_note destination resolution --------------------------------------


@pytest.mark.asyncio
async def test_add_note_requires_root_id():
    cfg, http, ms = _setup()
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc:
        await _call(mcp, "add_note", {"root_id": "  ", "preset": "waiting"})
    assert "root_id required" in str(exc.value)


async def _seed_dm_certs(http, peer="alice-0001"):
    from puffo_agent.crypto.encoding import base64url_encode
    from puffo_agent.crypto.primitives import KemKeyPair

    http.responses[f"/certs/sync?slugs={peer}"] = {
        "entries": [
            {
                "seq": 1, "kind": "device_cert", "slug": "agent-0001",
                "cert": {
                    "device_id": "dev_test",
                    "kem_public_key": base64url_encode(
                        KemKeyPair.generate().public_key_bytes()
                    ),
                },
            },
            {
                "seq": 2, "kind": "device_cert", "slug": peer,
                "cert": {
                    "device_id": "dev_alice",
                    "kem_public_key": base64url_encode(
                        KemKeyPair.generate().public_key_bytes()
                    ),
                },
            },
        ],
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_add_note_on_inbound_dm_root_targets_the_sender():
    cfg, http, ms = _setup()
    await ms.store({
        "envelope_id": "msg_dm_root", "envelope_kind": "dm",
        "sender_slug": "alice-0001", "recipient_slug": "agent-0001",
        "content_type": "text/plain", "content": "hi",
        "sent_at": _now_ms(),
    })
    await _seed_dm_certs(http)
    mcp = _build_tools(cfg)
    result = await _call(mcp, "add_note", {
        "root_id": "msg_dm_root", "preset": "processing", "message": "on it",
    })
    assert "posted" in result
    post_calls = [(p, b) for m, p, b in http.calls if m == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0][1]["recipient_slug"] == "alice-0001"


@pytest.mark.asyncio
async def test_add_note_on_outbound_dm_root_targets_the_recipient():
    cfg, http, ms = _setup()
    await ms.store({
        "envelope_id": "msg_dm_root", "envelope_kind": "dm",
        "sender_slug": "agent-0001", "recipient_slug": "alice-0001",
        "content_type": "text/plain", "content": "hi",
        "sent_at": _now_ms(),
    })
    await _seed_dm_certs(http)
    mcp = _build_tools(cfg)
    result = await _call(mcp, "add_note", {
        "root_id": "msg_dm_root", "preset": "processing", "message": "on it",
    })
    assert "posted" in result
    post_calls = [(p, b) for m, p, b in http.calls if m == "POST"]
    assert post_calls[0][1]["recipient_slug"] == "alice-0001"


@pytest.mark.asyncio
async def test_add_note_root_without_channel_or_dm_errors():
    cfg, http, ms = _setup()
    await ms.store({
        "envelope_id": "msg_odd", "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "content_type": "text/plain", "content": "odd",
        "sent_at": _now_ms(),
    })
    mcp = _build_tools(cfg)
    with pytest.raises(Exception) as exc:
        await _call(mcp, "add_note", {"root_id": "msg_odd", "preset": "waiting"})
    assert "cannot resolve" in str(exc.value)
