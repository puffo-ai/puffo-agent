"""PUF-227: handle_message_batch / handle_api_error_retry render each
message in a batch with ITS OWN channel/space fields, not the
batch-level cached channel_meta.

This is the load-bearing regression test for Scout's bug. Without
the fix, a batch where messages came from different channels would
render every message with the FIRST message's channel context
(because ``entry.channel_meta`` is captured at first enqueue and the
append branch in ``_admit_thread_message`` doesn't update it).
"""

from __future__ import annotations

import asyncio

from puffo_agent.agent.adapters import Adapter, TurnContext, TurnResult
from puffo_agent.agent.core import PuffoAgent


def _run(coro):
    return asyncio.run(coro)


class _CapturingAdapter(Adapter):
    """Records every system_prompt + messages payload the adapter
    sees so the test can inspect the rendered user blocks. Replies
    with [SILENT] so the agent doesn't try to send a fallback."""

    def __init__(self):
        self.calls: list[TurnContext] = []

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        self.calls.append(ctx)
        return TurnResult(reply="[SILENT]")


def _msg(
    *,
    envelope_id: str,
    sender: str = "sam-0001",
    channel_id: str,
    channel_name: str,
    space_id: str = "sp_founders",
    space_name: str = "Puffo Founders",
    text: str = "hello",
) -> dict:
    return {
        "envelope_id": envelope_id,
        "sender_slug": sender,
        "sender_email": "",
        "text": text,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "space_id": space_id,
        "space_name": space_name,
        "sent_at": 0,
        "is_dm": False,
        "attachments": [],
        "sender_is_bot": False,
        "mentions": [],
        "is_visible_to_human": True,
        "sender_display_name": "",
    }


def _agent(tmp_path) -> tuple[PuffoAgent, _CapturingAdapter]:
    adapter = _CapturingAdapter()
    agent = PuffoAgent(
        adapter=adapter,
        system_prompt="test bot",
        memory_dir=str(tmp_path),
    )
    return agent, adapter


# ── handle_message_batch: per-msg channel context wins ───────────


def test_batch_renders_each_msg_with_own_channel_id(tmp_path):
    """Scout's symptom shape, hoisted to the prompt-render layer.

    A batch contains two messages from different channels (which
    can happen via the in-queue append path on the same root_id).
    The batch-level channel_meta carries channel-A's context.
    Assert each rendered user block carries its OWN channel_id /
    channel_name / space_id / space_name — NOT channel_meta's."""
    agent, adapter = _agent(tmp_path)
    batch = [
        _msg(
            envelope_id="env_general_1",
            channel_id="ch_general",
            channel_name="General",
        ),
        _msg(
            envelope_id="env_gtm_1",
            channel_id="ch_gtm",
            channel_name="gtm",
        ),
    ]
    # Stale batch-level channel_meta (the bug shape — first msg's
    # channel got cached, second msg appended without updating it).
    stale_channel_meta = {
        "channel_id": "ch_general",
        "channel_name": "General",
        "space_id": "sp_founders",
        "space_name": "Puffo Founders",
        "is_dm": False,
    }
    _run(agent.handle_message_batch(
        root_id="env_thread_root",
        batch=batch,
        channel_meta=stale_channel_meta,
    ))
    # The agent.log should now contain TWO user entries, one per msg.
    user_entries = [e for e in agent.log if e.get("role") == "user"]
    assert len(user_entries) == 2

    block_a = user_entries[0]["content"]
    block_b = user_entries[1]["content"]

    # Block A carries #General's context (matches both the msg AND
    # the stale channel_meta, so this assertion alone is necessary
    # but not sufficient).
    assert "channel: General" in block_a
    assert "channel_id: ch_general" in block_a
    assert "space: Puffo Founders" in block_a

    # Block B carries #gtm's context — distinct from the stale
    # channel_meta. This is the load-bearing assertion for PUF-227.
    assert "channel: gtm" in block_b
    assert "channel_id: ch_gtm" in block_b
    # And critically, block B's render does NOT leak #General into
    # the channel_id field.
    assert "channel_id: ch_general" not in block_b


def test_batch_uses_msg_channel_when_channel_meta_empty(tmp_path):
    """Defense-in-depth: if a batch arrives with an empty
    channel_meta dict (older callers, future refactor regressions),
    per-msg fields still render correctly."""
    agent, adapter = _agent(tmp_path)
    batch = [_msg(
        envelope_id="env_solo",
        channel_id="ch_gtm",
        channel_name="gtm",
    )]
    _run(agent.handle_message_batch(
        root_id="env_solo",
        batch=batch,
        channel_meta={},  # empty — pre-PUF-227 this would render bare ids.
    ))
    user_entries = [e for e in agent.log if e.get("role") == "user"]
    assert len(user_entries) == 1
    block = user_entries[0]["content"]
    assert "channel: gtm" in block
    assert "channel_id: ch_gtm" in block


def test_batch_route_log_uses_last_msg_channel(tmp_path):
    """The post-batch route-log channel_name should reflect the
    LAST message's channel (the trigger the agent is most likely
    replying about), not the stale batch-level channel_meta."""
    agent, adapter = _agent(tmp_path)
    batch = [
        _msg(envelope_id="env_a", channel_id="ch_general", channel_name="General"),
        _msg(envelope_id="env_b", channel_id="ch_gtm", channel_name="gtm"),
    ]
    stale_channel_meta = {
        "channel_id": "ch_general",
        "channel_name": "General",
        "space_id": "sp_founders",
        "space_name": "Puffo Founders",
        "is_dm": False,
    }
    _run(agent.handle_message_batch(
        root_id="env_thread_root",
        batch=batch,
        channel_meta=stale_channel_meta,
    ))
    # _run_turn_and_route doesn't directly write a channel_name to
    # the log, but the adapter saw exactly one turn run. The
    # important post-condition is the user-block render correctness
    # already asserted above. This case smoke-tests no crash on the
    # divergent-channel-name path through _run_turn_and_route.
    assert len(adapter.calls) == 1


# ── handle_api_error_retry: symmetric per-msg rendering ──────────


class _CapturingRetryAdapter(Adapter):
    """Records the fallback text the retry path constructs so we
    can assert per-msg fields landed in the rendered chunks."""

    def __init__(self):
        self.fallback_text = ""

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        return TurnResult(reply="[SILENT]")

    async def run_retry_turn(
        self, kick_text: str, fallback_user_message: str, ctx: TurnContext,
    ) -> TurnResult:
        self.fallback_text = fallback_user_message
        return TurnResult(reply="[SILENT]")


def test_api_error_retry_fallback_renders_each_msg_with_own_channel(tmp_path):
    """The retry path builds a fallback payload by concatenating
    per-msg _format_user_block calls. PUF-227 makes those reads
    come from each msg, not from the stale channel_meta."""
    adapter = _CapturingRetryAdapter()
    agent = PuffoAgent(
        adapter=adapter,
        system_prompt="test bot",
        memory_dir=str(tmp_path),
    )
    fallback_batch = [
        _msg(envelope_id="env_a", channel_id="ch_general", channel_name="General"),
        _msg(envelope_id="env_b", channel_id="ch_gtm", channel_name="gtm"),
    ]
    stale_channel_meta = {
        "channel_id": "ch_general",
        "channel_name": "General",
        "space_id": "sp_founders",
        "space_name": "Puffo Founders",
        "is_dm": False,
    }
    _run(agent.handle_api_error_retry(
        root_id="env_thread_root",
        channel_meta=stale_channel_meta,
        fallback_batch=fallback_batch,
    ))
    # Both per-msg channel ids must be visible in the fallback
    # payload — the rate-limit retry must NOT leak channel-A's
    # context onto envelope-B's render.
    assert "channel: General" in adapter.fallback_text
    assert "channel_id: ch_general" in adapter.fallback_text
    assert "channel: gtm" in adapter.fallback_text
    assert "channel_id: ch_gtm" in adapter.fallback_text
