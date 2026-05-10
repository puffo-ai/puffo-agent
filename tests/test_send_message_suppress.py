"""Reply-routing tests for ``PuffoAgent.handle_message``.

Routing model:

  3a. ``send_message`` invoked at least once -> return None; MCP
      delivered the reply, shell auto-post would duplicate.
  3b. Else if any ``assistant.text`` frame contains the literal
      ``[SILENT]`` token (substring, position-independent) -> return
      None and don't append to the conversation log.
  3c. Else (no send_message AND no [SILENT]) -> assemble every
      ``assistant.text`` frame into a markdown bullet fallback and
      append it to the conversation log.

Adapter-side plumbing (how the metadata gets into ``TurnResult``)
is covered in the adapter suites.
"""

from __future__ import annotations

import asyncio

from puffo_agent.agent.adapters import Adapter, TurnContext, TurnResult
from puffo_agent.agent.core import PuffoAgent


# ── helpers ──────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


class _StubAdapter(Adapter):
    """Returns a canned ``TurnResult`` with chosen metadata so tests
    can drive ``handle_message`` through specific shapes."""

    def __init__(
        self,
        reply: str,
        *,
        tool_names: list[str] | None = None,
        send_message_targets: list[dict] | None = None,
        assistant_text_parts: list[str] | None = None,
    ):
        self._reply = reply
        self._tool_names = tool_names
        self._targets = send_message_targets
        self._parts = assistant_text_parts

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        meta: dict = {}
        if self._tool_names is not None:
            meta["tool_names"] = list(self._tool_names)
        if self._targets is not None:
            meta["send_message_targets"] = [dict(t) for t in self._targets]
        if self._parts is not None:
            meta["assistant_text_parts"] = list(self._parts)
        return TurnResult(reply=self._reply, metadata=meta)


def _agent(reply: str, tmp_path, **meta) -> PuffoAgent:
    return PuffoAgent(
        adapter=_StubAdapter(reply, **meta),
        system_prompt="you are a test bot",
        memory_dir=str(tmp_path),
    )


async def _dispatch(
    agent: PuffoAgent,
    *,
    channel_id: str = "c-main",
    channel_name: str = "general",
    root_id: str = "",
    post_id: str = "p-1",
    text: str = "hi",
) -> str | None:
    return await agent.handle_message(
        channel_id=channel_id,
        channel_name=channel_name,
        sender="u",
        sender_email="u@x",
        text=text,
        post_id=post_id,
        root_id=root_id,
    )


def _assistant_entries(agent: PuffoAgent) -> list[dict]:
    return [e for e in agent.log if e.get("role") == "assistant"]


# ── suppress: send_message landed in the current slot ───────────────────────


def test_suppresses_when_send_message_matches_channel_id_top_level(tmp_path):
    """Top-level incoming; agent send_message hits same channel_id
    with empty root_id — would otherwise post twice."""
    agent = _agent(
        "Replied in thread.",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": ""}],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result is None


def test_suppresses_when_send_message_matches_channel_name(tmp_path):
    """Tool accepts a name OR id; ``general`` matches channel_name."""
    agent = _agent(
        "done",
        tmp_path,
        send_message_targets=[{"channel": "general", "root_id": ""}],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result is None


def test_suppresses_when_send_message_matches_thread(tmp_path):
    """Threaded incoming + send_message to same thread root."""
    agent = _agent(
        "ack",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": "thread-abc"}],
    )
    result = _run(_dispatch(
        agent, channel_id="c-main", channel_name="general", root_id="thread-abc"
    ))
    assert result is None


def test_suppression_still_appends_to_agent_log(tmp_path):
    """Suppressed reply still lands in ``agent.log`` so the next turn
    sees it as context — only the outbound post is skipped."""
    agent = _agent(
        "Replied in thread.",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": ""}],
    )
    _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assistants = _assistant_entries(agent)
    assert len(assistants) == 1
    assert "Replied in thread." in assistants[0]["content"]


def test_suppresses_when_multiple_targets_include_current_slot(tmp_path):
    """Fan-out: one send_message elsewhere + one to current slot.
    The current-slot match is enough to suppress."""
    agent = _agent(
        "broadcasting",
        tmp_path,
        send_message_targets=[
            {"channel": "other-channel", "root_id": ""},
            {"channel": "c-main", "root_id": ""},
        ],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result is None


# ── 3a: send_message anywhere → suppress (slot-independent) ─────────────────


def test_suppresses_when_send_message_targets_different_channel(tmp_path):
    """Any send_message call counts — even one targeting a different
    channel. The agent took responsibility for delivering via MCP;
    the shell must not double-post."""
    agent = _agent(
        "FYI, pinged ops.",
        tmp_path,
        send_message_targets=[{"channel": "ops", "root_id": ""}],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result is None


def test_suppresses_when_send_message_targets_different_thread(tmp_path):
    """Different thread root: still suppressed. Slot-matching is gone."""
    agent = _agent(
        "still here.",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": "thread-xyz"}],
    )
    result = _run(_dispatch(
        agent, channel_id="c-main", channel_name="general", root_id="thread-abc"
    ))
    assert result is None


def test_suppresses_top_level_send_message_when_incoming_is_threaded(tmp_path):
    """Top-level send_message + threaded incoming: still suppressed."""
    agent = _agent(
        "replying in thread.",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": ""}],
    )
    result = _run(_dispatch(
        agent, channel_id="c-main", channel_name="general", root_id="thread-abc"
    ))
    assert result is None


def test_suppresses_threaded_send_message_when_incoming_is_top_level(tmp_path):
    """Threaded send_message + top-level incoming: still suppressed."""
    agent = _agent(
        "top-level reply.",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": "thread-xyz"}],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result is None


# ── 3c fallback: no send_message and no [SILENT] → bullet-list reply ────────


def test_fallback_when_no_send_message_called_and_no_silent_marker(tmp_path):
    """No send_message + no [SILENT]: runtime assembles assistant.text
    frames into the fallback reply."""
    agent = _agent(
        "42",
        tmp_path,
        tool_names=["Read", "Bash"],
        send_message_targets=[],
    )
    result = _run(_dispatch(agent))
    assert result == "42"


def test_does_not_suppress_when_metadata_missing(tmp_path):
    """Legacy adapters (e.g. ChatOnlyAdapter) don't populate metadata
    — fall through to the post-it path."""
    agent = _agent("Hello!", tmp_path)  # no metadata kwargs
    result = _run(_dispatch(agent))
    assert result == "Hello!"


# ── edge cases ──────────────────────────────────────────────────────────────


def test_empty_channel_target_still_counts_as_send_message(tmp_path):
    """Even a malformed send_message (empty channel) counts toward 3a:
    invoking the MCP tool signals the agent took responsibility for
    posting. Tool-arg validation is the MCP layer's job."""
    agent = _agent(
        "ok",
        tmp_path,
        send_message_targets=[{"channel": "", "root_id": ""}],
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result is None


def test_missing_root_id_in_target_treats_as_top_level(tmp_path):
    """Target without a ``root_id`` key is treated as empty (top-level)."""
    agent = _agent(
        "ok",
        tmp_path,
        send_message_targets=[{"channel": "c-main"}],  # no root_id key
    )
    result = _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id=""))
    assert result is None


# ── interaction with the [SILENT] check ─────────────────────────────────────


def test_send_message_takes_precedence_over_silent_marker(tmp_path):
    """send_message + [SILENT]: 3a wins. Result None either way, but
    3a appends the reply (next turn sees it as context) while 3b
    doesn't. Here the reply is ``[SILENT]`` so the appended entry IS
    the marker."""
    agent = _agent(
        "[SILENT]",
        tmp_path,
        send_message_targets=[{"channel": "c-main", "root_id": ""}],
    )
    assert _run(_dispatch(agent, channel_id="c-main", channel_name="general", root_id="")) is None
    assert len(_assistant_entries(agent)) == 1


# ── 3c fallback shape ───────────────────────────────────────────────────────


def test_fallback_assembles_multiple_assistant_parts_as_bullet_list(tmp_path):
    """No send_message + no [SILENT] + multiple ``assistant.text``
    frames -> markdown bullet reply, one bullet per non-empty frame."""
    agent = _agent(
        "first thought\nsecond thought",
        tmp_path,
        assistant_text_parts=["first thought", "second thought"],
    )
    result = _run(_dispatch(agent))
    assert result == "- first thought\n- second thought"


def test_fallback_single_part_returns_unwrapped(tmp_path):
    """Single frame: no bullet — return the frame as-is."""
    agent = _agent(
        "just one thing",
        tmp_path,
        assistant_text_parts=["just one thing"],
    )
    result = _run(_dispatch(agent))
    assert result == "just one thing"
