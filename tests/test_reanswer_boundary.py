"""Regression: the conversation boundary that stops the agent re-answering
already-handled messages.

Bug: a stateless turn replays the whole ``self.log`` to the model; with no
marker separating already-answered history from the new inbound message, the
model intermittently fires an extra ``send_message`` re-answering an older
question (observed: 4+4 re-answered when asked "who is Bobby Axelrod?"). Core
now prepends ``_NEW_MESSAGE_BOUNDARY`` to the newest inbound message(s) at
turn-construction time — on a per-turn copy, never persisted.
"""
from __future__ import annotations

import asyncio

from puffo_agent.agent.adapters import Adapter, TurnContext, TurnResult
from puffo_agent.agent.core import PuffoAgent, _NEW_MESSAGE_BOUNDARY


class _CapturingAdapter(Adapter):
    """Records the messages of the most recent turn; simulates a real reply
    (``send_message`` called) so the assistant turn gets appended to the log."""

    def __init__(self, reply: str = "ok"):
        self._reply = reply
        self.last_messages: list[dict] | None = None

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        self.last_messages = [dict(m) for m in ctx.messages]
        return TurnResult(
            reply=self._reply,
            metadata={"send_message_targets": [{"slug": "u"}]},
        )


def _agent(adapter, tmp_path) -> PuffoAgent:
    return PuffoAgent(adapter=adapter, system_prompt="test", memory_dir=str(tmp_path))


def _send(agent: PuffoAgent, text: str, post_id: str):
    return asyncio.run(
        agent.handle_message(
            channel_id="c", channel_name="general", sender="alice",
            sender_email="a@x", text=text, post_id=post_id,
        )
    )


def test_first_message_has_no_boundary(tmp_path):
    ad = _CapturingAdapter(); ag = _agent(ad, tmp_path)
    _send(ag, "what is 2+2?", "p1")
    assert len(ad.last_messages) == 1
    assert _NEW_MESSAGE_BOUNDARY not in ad.last_messages[0]["content"]


def test_second_message_marks_only_the_new_one(tmp_path):
    ad = _CapturingAdapter(); ag = _agent(ad, tmp_path)
    _send(ag, "what is 2+2?", "p1")   # turn 1 → log: [userA, assistant]
    _send(ag, "who are you?", "p2")   # turn 2 → log: [userA, assistant, userB]
    msgs = ad.last_messages
    assert _NEW_MESSAGE_BOUNDARY in msgs[-1]["content"]      # newest (userB) marked
    assert "who are you?" in msgs[-1]["content"]
    assert _NEW_MESSAGE_BOUNDARY not in msgs[0]["content"]   # prior (userA) untouched
    assert "2+2" in msgs[0]["content"]


def test_boundary_is_not_persisted(tmp_path):
    ad = _CapturingAdapter(); ag = _agent(ad, tmp_path)
    _send(ag, "q1", "p1"); _send(ag, "q2", "p2")
    user_entries = [m for m in ag.log if m["role"] == "user"]
    assert _NEW_MESSAGE_BOUNDARY not in user_entries[-1]["content"]


def test_batch_marks_the_new_batch_not_prior_history(tmp_path):
    ad = _CapturingAdapter(); ag = _agent(ad, tmp_path)
    _send(ag, "prior question", "p0")   # establishes history
    batch = [
        {"sender_slug": "alice", "text": "batch msg 1", "envelope_id": "e1", "sent_at": 1},
        {"sender_slug": "alice", "text": "batch msg 2", "envelope_id": "e2", "sent_at": 2},
    ]
    asyncio.run(ag.handle_message_batch(
        root_id="", batch=batch, channel_meta={"channel_name": "general"},
    ))
    msgs = ad.last_messages
    b1 = next(m for m in msgs if "batch msg 1" in m["content"])
    prior = next(m for m in msgs if "prior question" in m["content"])
    assert _NEW_MESSAGE_BOUNDARY in b1["content"]        # first new batch msg marked
    assert _NEW_MESSAGE_BOUNDARY not in prior["content"] # prior history untouched
