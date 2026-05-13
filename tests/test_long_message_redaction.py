"""Long-message redaction at turn-context build time + the
``get_post_segment`` MCP tool that pages the full body back in
chunks.

Background: an operator pasted a long code block into a DM; the
agent's adapter hit Anthropic's ``Prompt is too long`` and the
existing rate-limit retry loop bounced the same payload through
claude-code's ``--resume`` repeatedly without progress. Restart
didn't help because the failed batch's cursor stays put (by
design — we never silently drop a user message). The fix is at
the source: redact oversize bodies before they ever reach the
LLM, store the original verbatim in messages.db, and give the
agent a paging tool to fetch it back when it needs to.

These tests pin:
  * the redact helper's threshold + placeholder shape
  * the MCP tool's slice math, error paths, and dict-content
    extraction (attachments-content envelopes still segment)
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.puffo_core_client import _maybe_redact_long_text


# ─── _maybe_redact_long_text ──────────────────────────────────────


def test_redact_passthrough_when_under_threshold():
    """Messages at or below the threshold must come through
    unchanged — the placeholder is only for messages we'd otherwise
    blow the context budget on."""
    short = "Hello, how's it going?"
    out = _maybe_redact_long_text(
        short,
        envelope_id="env_short",
        sender_slug="alice",
        sender_display_name="",
        max_inline_chars=4000,
        segment_chars=2000,
        agent_slug="d2d2",
    )
    assert out == short


def test_redact_passthrough_at_exact_threshold():
    """``len(text) == max_inline_chars`` is still allowed (the
    cutoff is strict ``>``). Boundary pins ensure tuning the
    threshold doesn't accidentally swap < for ≤ / vice versa."""
    payload = "x" * 4000
    out = _maybe_redact_long_text(
        payload,
        envelope_id="env_boundary",
        sender_slug="alice",
        sender_display_name="",
        max_inline_chars=4000,
        segment_chars=2000,
        agent_slug="d2d2",
    )
    assert out == payload


def test_redact_emits_placeholder_with_envelope_id_and_segments():
    """The placeholder must carry every datum the agent needs to
    decide whether — and how — to page the body back:
      * the marker prefix the primer recognises
      * envelope_id (so the tool call resolves)
      * total_chars + segment count + per-segment size
      * sender (so the agent has provenance without a fetch)
      * a preview (so the agent can skip fetching trivial content)
      * the tool-call recipe."""
    payload = "A" * 5000
    out = _maybe_redact_long_text(
        payload,
        envelope_id="env_long",
        sender_slug="alice",
        sender_display_name="Alice",
        max_inline_chars=4000,
        segment_chars=2000,
        agent_slug="d2d2",
    )
    assert "[puffo-agent system message]" in out
    assert "envelope_id: env_long" in out
    assert "total_chars: 5000" in out
    # ceil(5000/2000) == 3, indexed 0..2.
    assert "segments: 3 (0-indexed, up to 2000 chars each)" in out
    assert "sender: @Alice (alice)" in out
    assert "mcp__puffo__get_post_segment" in out
    assert "segment=N) where N runs 0..2" in out
    # The placeholder itself shouldn't be longer than the
    # original by an order of magnitude (sanity — a buggy preview
    # that includes the whole payload would defeat the redaction).
    assert len(out) < 1500


def test_redact_preview_truncated_with_ellipsis():
    """The preview must be short enough not to itself blow the
    context budget — and it must mark the cut so the agent knows
    it's looking at a head, not the full body."""
    payload = "abcdefghij" * 1000  # 10k chars
    out = _maybe_redact_long_text(
        payload,
        envelope_id="env_preview",
        sender_slug="alice",
        sender_display_name="",
        max_inline_chars=4000,
        segment_chars=2000,
        agent_slug="d2d2",
    )
    # Preview is the first 240 chars of the payload + ellipsis.
    expected_preview_head = "abcdefghij" * 24
    assert expected_preview_head in out
    assert "…" in out


def test_redact_normalises_newlines_in_preview():
    """Preview is single-line so the placeholder stays a tidy
    block in the agent's prompt; original newlines reach the
    agent via the segment tool when it actually pages the body."""
    payload = "line1\nline2\nline3\n" + ("x" * 5000)
    out = _maybe_redact_long_text(
        payload,
        envelope_id="env_lines",
        sender_slug="alice",
        sender_display_name="",
        max_inline_chars=4000,
        segment_chars=2000,
        agent_slug="d2d2",
    )
    # The preview shouldn't break the placeholder block by
    # injecting raw newlines.
    preview_line = [
        line for line in out.split("\n") if line.startswith("  preview: ")
    ]
    assert len(preview_line) == 1
    assert "line1 line2 line3" in preview_line[0]


def test_redact_empty_text_returns_empty():
    """No body, no placeholder — caller handles the "no text"
    case (e.g. attachments-only envelopes) separately."""
    out = _maybe_redact_long_text(
        "",
        envelope_id="env_empty",
        sender_slug="alice",
        sender_display_name="",
        max_inline_chars=4000,
        segment_chars=2000,
        agent_slug="d2d2",
    )
    assert out == ""


def test_redact_sender_label_falls_back_to_slug():
    """When the profile cache hasn't hydrated a display_name we
    show the bare slug — better than ``@ (alice)`` with an empty
    name in parens."""
    payload = "z" * 5000
    out = _maybe_redact_long_text(
        payload,
        envelope_id="env_nameless",
        sender_slug="alice",
        sender_display_name="",
        max_inline_chars=4000,
        segment_chars=2000,
        agent_slug="d2d2",
    )
    assert "sender: @alice" in out
    assert "@ (alice)" not in out


# ─── get_post_segment MCP tool ────────────────────────────────────
#
# The tool body lives inside ``register_core_tools``'s closure, so
# we exercise it by stubbing a minimal ``data_client`` + ``cfg``,
# capturing the tool via a fake ``mcp.tool()`` decorator, and
# calling it directly.


class _StubDataClient:
    def __init__(self, message=None):
        self._message = message

    async def get_message_by_envelope(self, envelope_id: str):
        return self._message


class _StubMessage:
    """Mirrors enough of ``StoredMessageDict`` for the tool's
    assertions about ``content`` shape."""
    def __init__(self, envelope_id="env_x", sender_slug="alice",
                 envelope_kind="dm", channel_id=None,
                 thread_root_id=None, sent_at=1_700_000_000_000,
                 content=""):
        self.envelope_id = envelope_id
        self.sender_slug = sender_slug
        self.envelope_kind = envelope_kind
        self.channel_id = channel_id
        self.thread_root_id = thread_root_id
        self.sent_at = sent_at
        self.content = content


def _collect_tool(name: str, data_client):
    """Build the ``get_post_segment`` closure by running
    ``register_core_tools`` against a fake FastMCP that captures
    every ``@mcp.tool()`` decoration into a dict."""
    from puffo_agent.mcp.puffo_core_tools import (
        PuffoCoreToolsConfig, register_core_tools,
    )

    captured: dict[str, callable] = {}

    class _FakeMcp:
        def tool(self, *args, **kwargs):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn
            return decorator

    cfg = PuffoCoreToolsConfig(
        slug="d2d2",
        device_id="dev_x",
        keystore=types.SimpleNamespace(),  # unused by this tool
        http_client=types.SimpleNamespace(),  # unused by this tool
        data_client=data_client,
        space_id=None,
        workspace=None,
    )
    register_core_tools(_FakeMcp(), cfg)
    return captured[name]


@pytest.mark.asyncio
async def test_segment_returns_chunk_with_range_metadata():
    """Happy path: segment 1 of a 5000-char body at size 2000.

    Indices: 2000..3999 (inclusive), 2000 chars long. The
    response header gives the agent enough context (segment x/y,
    char range, total) to chain fetches sensibly."""
    payload = "".join(str(i % 10) for i in range(5000))  # 5000 chars
    tool = _collect_tool("get_post_segment", _StubDataClient(
        _StubMessage(envelope_id="env_5k", content=payload),
    ))
    out = await tool(envelope_id="env_5k", segment=1, segment_size=2000)
    assert out.startswith("segment 1/2 (chars 2000..3999 of 5000):\n")
    assert payload[2000:4000] in out


@pytest.mark.asyncio
async def test_segment_last_chunk_is_short():
    """The final segment of a body whose length isn't a multiple
    of segment_size carries the leftover characters only — the
    tool must NOT pad or overrun the string slice."""
    payload = "x" * 4500  # 4500 chars; with size 2000 → 3 segments
    tool = _collect_tool("get_post_segment", _StubDataClient(
        _StubMessage(envelope_id="env_4500", content=payload),
    ))
    out = await tool(envelope_id="env_4500", segment=2, segment_size=2000)
    # Segment 2 covers chars 4000..4499 (500 chars).
    assert "segment 2/2 (chars 4000..4499 of 4500):\n" in out
    # Body part of the output is exactly the leftover slice.
    body = out.split("\n", 1)[1]
    assert body == payload[4000:]
    assert len(body) == 500


@pytest.mark.asyncio
async def test_segment_out_of_range_returns_explanation():
    """An overshooting segment index returns a human-readable
    error string the agent can correct from, NOT a raw exception
    or a silent empty body."""
    payload = "y" * 1500
    tool = _collect_tool("get_post_segment", _StubDataClient(
        _StubMessage(envelope_id="env_1500", content=payload),
    ))
    out = await tool(envelope_id="env_1500", segment=99, segment_size=2000)
    assert "out of range" in out
    assert "env_1500" in out


@pytest.mark.asyncio
async def test_segment_unknown_envelope_id_returns_not_found():
    """The tool resolves through the data-service; a 404 from the
    daemon comes back as a string the agent can parse instead of
    an exception."""
    tool = _collect_tool("get_post_segment", _StubDataClient(None))
    out = await tool(envelope_id="env_gone", segment=0, segment_size=2000)
    assert "not found in local storage" in out
    assert "env_gone" in out


@pytest.mark.asyncio
async def test_segment_attachments_content_dict_segments_text():
    """``puffo/message+attachments/v1`` carries content as a dict
    with a ``text`` field and an ``attachments`` list. The
    segmenting tool reads the text portion only — attachments are
    already separately materialised on disk via the inbox path."""
    payload = "z" * 3000
    tool = _collect_tool("get_post_segment", _StubDataClient(
        _StubMessage(envelope_id="env_attach", content={
            "text": payload,
            "attachments": [{"name": "screenshot.png"}],
        }),
    ))
    out = await tool(envelope_id="env_attach", segment=0, segment_size=2000)
    assert "segment 0/1 (chars 0..1999 of 3000):\n" in out
    assert payload[:2000] in out


@pytest.mark.asyncio
async def test_segment_empty_body_message_returns_marker():
    """An envelope whose text body is empty (attachments-only or
    a future control message) returns an explicit marker instead
    of a misleading "segment 0 (chars 0..-1 of 0)" header."""
    tool = _collect_tool("get_post_segment", _StubDataClient(
        _StubMessage(envelope_id="env_blank", content=""),
    ))
    out = await tool(envelope_id="env_blank", segment=0, segment_size=2000)
    assert "no text body" in out


@pytest.mark.asyncio
async def test_segment_rejects_missing_envelope_id():
    """Empty envelope_id is a programmer error, not a not-found —
    surface it as a ``RuntimeError`` so the agent's adapter can
    log + skip rather than burn a fetch on a clearly-bad call."""
    tool = _collect_tool("get_post_segment", _StubDataClient(None))
    with pytest.raises(RuntimeError, match="envelope_id"):
        await tool(envelope_id="", segment=0, segment_size=2000)


@pytest.mark.asyncio
async def test_segment_rejects_negative_segment():
    tool = _collect_tool("get_post_segment", _StubDataClient(None))
    with pytest.raises(RuntimeError, match="segment must be >= 0"):
        await tool(envelope_id="env_x", segment=-1, segment_size=2000)


@pytest.mark.asyncio
async def test_segment_rejects_non_positive_segment_size():
    tool = _collect_tool("get_post_segment", _StubDataClient(None))
    with pytest.raises(RuntimeError, match="segment_size must be > 0"):
        await tool(envelope_id="env_x", segment=0, segment_size=0)
