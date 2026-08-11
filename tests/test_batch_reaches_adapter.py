"""A thread batch must reach the adapter whole. CLI adapters send only
``ctx.messages[-1]`` per turn (the resume-based session holds prior
history), so ``handle_message_batch`` must fold the batch into ONE user
log entry — per-message entries silently drop all but the last message.
"""

from __future__ import annotations

import asyncio

from puffo_agent.agent.adapters.base import TurnContext, TurnResult
from puffo_agent.agent.core import PuffoAgent


class _RecordingAdapter:
    """Captures what a CLI adapter would transmit: the last user entry."""

    def __init__(self):
        self.sent: list[str] = []

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        self.sent.append(ctx.messages[-1]["content"] if ctx.messages else "")
        return TurnResult(reply="[SILENT]", metadata={})


def _make_agent(tmp_path) -> tuple[PuffoAgent, _RecordingAdapter]:
    adapter = _RecordingAdapter()
    agent = PuffoAgent(
        adapter=adapter,
        system_prompt="sys",
        memory_dir=str(tmp_path / "memory"),
        agent_id="test-agent",
    )
    return agent, adapter


def _msg(envelope_id: str, sender: str, text: str, sent_at: int) -> dict:
    return {
        "envelope_id": envelope_id,
        "sender_slug": sender,
        "sender_email": "",
        "text": text,
        "attachments": [],
        "sender_is_agent": False,
        "mentions": [],
        "sent_at": sent_at,
        "is_dm": False,
        "sender_display_name": sender.title(),
        "is_visible_to_human": True,
    }


_META = {
    "channel_id": "ch_1",
    "channel_name": "general",
    "space_id": "sp_1",
    "space_name": "Space",
}


def test_every_batch_message_reaches_the_adapter(tmp_path):
    agent, adapter = _make_agent(tmp_path)
    batch = [
        _msg("msg_1", "alice", "first message", 100),
        _msg("msg_2", "bob", "second message", 200),
        _msg("msg_3", "alice", "third message", 300),
    ]
    asyncio.run(agent.handle_message_batch("msg_1", batch, _META))

    assert len(adapter.sent) == 1
    sent = adapter.sent[0]
    assert "first message" in sent
    assert "second message" in sent
    assert "third message" in sent
    # In arrival order.
    assert sent.index("first message") < sent.index("second message") < sent.index("third message")
    # Per-message metadata is intact for each block.
    assert sent.count("- sender_slug: alice") == 2
    assert sent.count("- sender_slug: bob") == 1
    assert "- post_id: msg_2" in sent


def test_batch_is_one_log_entry(tmp_path):
    agent, _ = _make_agent(tmp_path)
    batch = [
        _msg("msg_1", "alice", "first", 100),
        _msg("msg_2", "bob", "second", 200),
    ]
    asyncio.run(agent.handle_message_batch("msg_1", batch, _META))

    user_entries = [e for e in agent.log if e["role"] == "user"]
    assert len(user_entries) == 1
    assert "first" in user_entries[0]["content"]
    assert "second" in user_entries[0]["content"]


def test_single_message_batch_matches_old_shape(tmp_path):
    agent, adapter = _make_agent(tmp_path)
    asyncio.run(agent.handle_message_batch(
        "msg_1", [_msg("msg_1", "alice", "hello", 100)], _META,
    ))
    assert len(adapter.sent) == 1
    assert "- message: hello" in adapter.sent[0]
    assert len([e for e in agent.log if e["role"] == "user"]) == 1


def test_empty_batch_is_noop(tmp_path):
    agent, adapter = _make_agent(tmp_path)
    out = asyncio.run(agent.handle_message_batch("msg_1", [], _META))
    assert out is None
    assert adapter.sent == []
    assert agent.log == []
