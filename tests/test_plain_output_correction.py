"""PUF-400 — a produced answer must never be binned in silence.

A channel message reaches an agent as a global-inbox notice, and that path sets
``allow_plain_fallback=False`` because a multi-target notice has no single place
to post loose prose. Until this fix, a model that answered in prose instead of
calling ``send_message`` had its answer logged at INFO and dropped — from the
channel, indistinguishable from an agent ignoring the room, and
indistinguishable from PUF-393 before that was fixed.

Observed on staging 2026-09-17: one question in a three-agent channel,
``wake PutEvents delivered woken=3``, all three woke, read the roster, wrote
channel cursors and ran a turn. One answer reached the human.

These live in their own module rather than ``test_global_inbox_runtime.py``:
that file is at the repo's 2000-line structural limit, and this behaviour reads
better named than buried.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from puffo_agent.agent.adapters.base import TurnResult
from puffo_agent.agent.core import PuffoAgent

# ── PUF-400: a produced answer must not be binned in silence ────────────────
#
# A channel message reaches the agent as a global-inbox notice, which sets
# ``allow_plain_fallback=False`` because a multi-target notice has no single
# place to post loose prose. When the model answered in prose instead of
# calling ``send_message``, the router logged ``[no-send]`` at INFO and threw
# the answer away — indistinguishable, from the channel, from an agent that
# ignored the room. Observed on staging 2026-09-17: three agents woken, three
# turns run, one answer delivered.


def _planned_one_channel():
    return SimpleNamespace(
        provider_input="<exact-global-input>",
        targets=(("sp_axe", "ch_general"),),
    )


@pytest.mark.asyncio
async def test_plain_global_output_is_corrected_and_then_delivered(tmp_path):
    """The model answers in prose; one corrective ask makes it use the tool."""

    class CorrectableAdapter:
        def __init__(self):
            self.kicks: list[str] = []

        async def run_turn(self, _ctx):
            return TurnResult(
                reply="Tokyo.",
                metadata={"assistant_text_parts": ["Tokyo."]},
            )

        async def run_retry_turn(self, kick, _fallback, _ctx):
            self.kicks.append(kick)
            return TurnResult(
                reply="sent",
                metadata={"send_message_targets": ["ch_general"]},
            )

    adapter = CorrectableAdapter()
    agent = PuffoAgent(
        adapter=adapter,
        system_prompt="system",
        memory_dir=str(tmp_path / "memory"),
    )

    assert await agent.handle_global_inbox_turn(_planned_one_channel()) is None
    assert len(adapter.kicks) == 1, "exactly one corrective ask, never a loop"
    kick = adapter.kicks[0]
    assert "send_message" in kick, "the correction must name the tool"
    assert "ch_general" in kick, "and the target it should go to"
    # The delivered reply is retained; the dropped prose never becomes a post.
    assert [e["role"] for e in agent.log if e["role"] == "assistant"] == ["assistant"]


@pytest.mark.asyncio
async def test_plain_global_output_twice_is_surfaced_not_swallowed(tmp_path, caplog):
    """If the correction fails too, the give-up is loud and leaves a trace."""

    class StubbornAdapter:
        def __init__(self):
            self.retries = 0

        async def run_turn(self, _ctx):
            return TurnResult(
                reply="Tokyo.",
                metadata={"assistant_text_parts": ["Tokyo."]},
            )

        async def run_retry_turn(self, _kick, _fallback, _ctx):
            self.retries += 1
            return TurnResult(
                reply="Tokyo, again.",
                metadata={"assistant_text_parts": ["Tokyo, again."]},
            )

    adapter = StubbornAdapter()
    agent = PuffoAgent(
        adapter=adapter,
        system_prompt="system",
        memory_dir=str(tmp_path / "memory"),
    )

    with caplog.at_level(logging.WARNING):
        assert await agent.handle_global_inbox_turn(_planned_one_channel()) is None

    assert adapter.retries == 1, "corrected once, not repeatedly"
    assert any("plain_output_undelivered" in r.getMessage() for r in caplog.records), (
        "an answer that could not be routed must never be dropped without a trace"
    )
    assert all(e["role"] != "assistant" for e in agent.log)


@pytest.mark.asyncio
async def test_silence_on_the_corrective_ask_is_respected(tmp_path, caplog):
    """[SILENT] on the second ask is a decision, not a failure."""

    class SilentOnRetryAdapter:
        async def run_turn(self, _ctx):
            return TurnResult(
                reply="thinking out loud",
                metadata={"assistant_text_parts": ["thinking out loud"]},
            )

        async def run_retry_turn(self, _kick, _fallback, _ctx):
            return TurnResult(reply="[SILENT]", metadata={})

    agent = PuffoAgent(
        adapter=SilentOnRetryAdapter(),
        system_prompt="system",
        memory_dir=str(tmp_path / "memory"),
    )

    with caplog.at_level(logging.WARNING):
        assert await agent.handle_global_inbox_turn(_planned_one_channel()) is None

    assert not any(
        "plain_output_undelivered" in r.getMessage() for r in caplog.records
    ), "choosing silence is not an undelivered answer"


@pytest.mark.asyncio
async def test_a_turn_that_calls_send_message_is_never_corrected(tmp_path):
    """The happy path must not pay for a second turn."""

    class ToolCallingAdapter:
        def __init__(self):
            self.retries = 0

        async def run_turn(self, _ctx):
            return TurnResult(
                reply="sent",
                metadata={"send_message_targets": ["ch_general"]},
            )

        async def run_retry_turn(self, _kick, _fallback, _ctx):
            self.retries += 1
            return TurnResult(reply="", metadata={})

    adapter = ToolCallingAdapter()
    agent = PuffoAgent(
        adapter=adapter,
        system_prompt="system",
        memory_dir=str(tmp_path / "memory"),
    )

    assert await agent.handle_global_inbox_turn(_planned_one_channel()) is None
    assert adapter.retries == 0
