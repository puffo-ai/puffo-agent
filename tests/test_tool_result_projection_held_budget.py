"""An oversized held send result must stay inside the inline budget with
its ``[puffo:model-visible-read:...]`` receipt intact — a harness spills
oversized tool results to a file wholesale, the receipt then never enters
the model's context, and ``send_anyway`` becomes refusable-forever in
exactly the busy-channel scenario it exists for."""

from __future__ import annotations

import json

import pytest

from puffo_agent.mcp import tool_result_projection as projection
from puffo_agent.mcp.tool_result_projection import (
    _HELD_INLINE_BUDGET_CHARS,
    format_send_result,
)

_MARKER = "[puffo:model-visible-read:receipt-abc123]"


def _new_context_body(message_count: int, body_chars: int) -> str:
    lines = ["## puffo daemon fix"]
    for index in range(message_count):
        lines.append(
            f'[message context_version=1 seq={100 + index} '
            f'message_id="msg_{index:04d}" sender_identity="@peer-{index}" '
            f'sender_type="agent" self=false]'
        )
        lines.append("content=" + json.dumps(f"m{index}-" + "x" * body_chars))
    return "\n".join(lines)


def _held_result(
    *, draft_chars: int, basis_chars: int, message_count: int, body_chars: int
) -> dict:
    return {
        "state": "held",
        "attempted": True,
        "latest_seq": 120,
        "blocking_seq": 120,
        "tool_result_admission": _MARKER,
        "reconsideration": {
            "context_version": 1,
            "context_ready": True,
            "based_on_through_seq": 99,
            "target": {"target_ref": "channel:sp_test:ch_test"},
            "draft": "D" * draft_chars,
            "visible_draft_basis": _new_context_body(3, basis_chars),
            "new_channel_context": _new_context_body(message_count, body_chars),
            "new_channel_context_count": message_count,
            "guidance": "reconsider against the latest context",
            "participation_snapshot": {
                "current_agent_identity": "@engineer-test",
                "current_agent_has_visible_message": False,
                "other_visible_agent_count": 2,
            },
        },
    }


@pytest.fixture
def spill_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_WORKSPACE", str(tmp_path))
    return tmp_path


def test_oversized_held_result_stays_inline_with_receipt(spill_workspace):
    """Bob's TC scenario: big draft + busy channel must not overflow."""
    result = _held_result(
        draft_chars=30_000, basis_chars=8_000, message_count=6, body_chars=9_000
    )
    text = format_send_result(result)
    assert len(text) <= _HELD_INLINE_BUDGET_CHARS
    assert _MARKER in text
    assert text.index(_MARKER) < text.index("[end_send_result")
    # Every new-context message header survives; bodies are bounded.
    for index in range(6):
        assert f'message_id="msg_{index:04d}"' in text
    assert "truncated; full text in the spill file" in text
    # The full result, draft included, is recoverable from the spill file.
    spill_files = list((spill_workspace / ".puffo" / "held").glob("held-*.txt"))
    assert len(spill_files) == 1
    spilled = spill_files[0].read_text(encoding="utf-8")
    assert "D" * 30_000 in spilled
    assert _MARKER in spilled


def test_small_held_result_is_unchanged_and_never_spilled(spill_workspace):
    result = _held_result(
        draft_chars=200, basis_chars=200, message_count=2, body_chars=100
    )
    text = format_send_result(result)
    assert "D" * 200 in text  # draft still inline verbatim
    assert "spilled_to" not in text
    assert _MARKER in text
    assert not (spill_workspace / ".puffo" / "held").exists()


def test_pathological_flood_still_fits_via_content_floor(spill_workspace):
    """Hundreds of mid-sized messages: the 300-char floor must land it."""
    result = _held_result(
        draft_chars=5_000, basis_chars=1_000, message_count=60, body_chars=1_500
    )
    text = format_send_result(result)
    assert len(text) <= _HELD_INLINE_BUDGET_CHARS
    assert _MARKER in text


def test_message_count_flood_keeps_marker_inline(spill_workspace):
    """Chris's gate boundary: overflow by COUNT, not per-message length —
    hundreds of compact messages must not push the receipt out of line."""
    result = _held_result(
        draft_chars=1_000, basis_chars=500, message_count=400, body_chars=250
    )
    text = format_send_result(result)
    assert len(text) <= _HELD_INLINE_BUDGET_CHARS
    assert _MARKER in text
    assert text.index(_MARKER) < text.index("[end_send_result")
    # Newest messages stay inline; the oldest are counted and spilled.
    assert 'message_id="msg_0399"' in text
    assert 'message_id="msg_0000"' not in text
    assert "omitted_count=" in text
    spill_files = list((spill_workspace / ".puffo" / "held").glob("held-*.txt"))
    assert len(spill_files) == 1
    assert 'message_id="msg_0000"' in spill_files[0].read_text(encoding="utf-8")


def test_headers_only_final_tier_is_bounded(spill_workspace):
    """Last-resort tier: header lines are not content-capped, so messages
    with enormous headers defeat every message-keeping tier — the final
    (floor, 0) tier must still fit with zero inline messages, an omitted
    count, and the receipt marker in place."""
    body_lines = ["## puffo daemon fix"]
    for index in range(80):
        body_lines.append(
            f'[message context_version=1 seq={100 + index} '
            f'message_id="msg_{index:04d}" '
            f'sender_identity="@{"p" * 2_000}-{index}" '
            f'sender_type="agent" self=false]'
        )
        body_lines.append("content=" + json.dumps(f"m{index}"))
    result = _held_result(
        draft_chars=1_000, basis_chars=200, message_count=1, body_chars=100
    )
    result["reconsideration"]["new_channel_context"] = "\n".join(body_lines)
    result["reconsideration"]["new_channel_context_count"] = 80
    text = format_send_result(result)
    assert len(text) <= _HELD_INLINE_BUDGET_CHARS
    assert _MARKER in text
    assert text.index(_MARKER) < text.index("[end_send_result")
    assert "[message " not in text  # zero inline messages in the final tier
    assert "omitted_count=80" in text
    spill_files = list((spill_workspace / ".puffo" / "held").glob("held-*.txt"))
    assert len(spill_files) == 1
    assert 'message_id="msg_0079"' in spill_files[0].read_text(encoding="utf-8")


def test_non_held_results_are_never_spilled(spill_workspace):
    result = {
        "state": "sent",
        "attempted": True,
        "seq": 5,
        "note": "N" * 60_000,
    }
    text = format_send_result(result)
    assert "N" * 60_000 in text
    assert not (spill_workspace / ".puffo" / "held").exists()


def test_spill_dir_defaults_to_cwd_when_env_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("PUFFO_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    result = _held_result(
        draft_chars=50_000, basis_chars=100, message_count=1, body_chars=100
    )
    text = format_send_result(result)
    assert len(text) <= _HELD_INLINE_BUDGET_CHARS
    assert list((tmp_path / ".puffo" / "held").glob("held-*.txt"))
    assert projection._held_spill_dir() == tmp_path / ".puffo" / "held"
