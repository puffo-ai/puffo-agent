"""chat-local structured tool use (AnthropicProvider + ChatOnlyAdapter).

The fat cloud agent on ``runtime.kind: chat-local`` must emit
``send_message`` as a structured ``tool_use`` (executed via the
injected puffo_core dispatch) rather than plain text — otherwise the
core turn-router falls back to posting the raw assistant prose
(leaking ``send_message(channel=..., ...)`` into the channel).

Covers:
  - the provider's agentic loop (tools advertised, tool_use parsed,
    dispatch executed, tool_result echoed back, usage summed);
  - the adapter's SDKAdapter-mirroring metadata contract
    (``send_message_targets`` + ``assistant_text_parts``);
  - the core turn-router NOT taking the raw-text fallback when a
    send_message tool call landed;
  - plain-text (no tool) responses still returning normally.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from puffo_agent.agent.adapters.base import TurnContext
from puffo_agent.agent.adapters.chat_only import ChatOnlyAdapter
from puffo_agent.agent.core import PuffoAgent
from puffo_agent.agent.providers.anthropic_provider import AnthropicProvider


# ── fakes ────────────────────────────────────────────────────────────────────


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(block_id: str, name: str, tool_input: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)


def _response(content: list, stop_reason: str, in_tok: int = 10, out_tok: int = 5):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


class _FakeMessages:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeAnthropicClient:
    def __init__(self, responses: list):
        self.messages = _FakeMessages(responses)


def _provider(responses: list) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test-key", model="claude-test")
    provider.client = _FakeAnthropicClient(responses)
    return provider


def _adapter_with_dispatch(responses: list, sent_calls: list) -> ChatOnlyAdapter:
    """ChatOnlyAdapter wired the way worker.py wires chat-local:
    tool-capable provider + injected puffo_core send dispatch."""

    async def fake_send_message(
        channel: str,
        text: str,
        root_id: str = "",
        visibility_level: str = "default",
    ) -> str:
        sent_calls.append({
            "channel": channel,
            "text": text,
            "root_id": root_id,
            "visibility_level": visibility_level,
        })
        return "message sent (envelope_id: env_123)"

    adapter = ChatOnlyAdapter(_provider(responses))
    adapter.tool_dispatch = {"send_message": fake_send_message}
    return adapter


def _ctx() -> TurnContext:
    return TurnContext(
        system_prompt="you are a test bot",
        messages=[{"role": "user", "content": "hi"}],
    )


_TOOL_USE_TURN = lambda: [  # noqa: E731 — fresh responses per test
    _response(
        [
            _text_block("Replying via send_message."),
            _tool_use_block(
                "toolu_1",
                "send_message",
                {"channel": "ch_abc", "text": "hello!", "root_id": "r-9"},
            ),
        ],
        stop_reason="tool_use",
    ),
    _response([_text_block("Posted.")], stop_reason="end_turn"),
]


# ── tool_use path: parsed + dispatched + metadata populated ─────────────────


def test_tool_use_parsed_dispatched_and_metadata_populated():
    sent: list[dict] = []
    adapter = _adapter_with_dispatch(_TOOL_USE_TURN(), sent)

    result = asyncio.run(adapter.run_turn(_ctx()))

    # The tool call was executed through the dispatch.
    assert sent == [{
        "channel": "ch_abc",
        "text": "hello!",
        "root_id": "r-9",
        "visibility_level": "default",
    }]
    # Metadata mirrors SDKAdapter's contract → core skips fallback.
    assert result.metadata["send_message_targets"] == [
        {"channel": "ch_abc", "root_id": "r-9"},
    ]
    assert result.metadata["tool_names"] == ["send_message"]
    assert result.metadata["assistant_text_parts"] == [
        "Replying via send_message.",
        "Posted.",
    ]
    assert result.tool_calls == 1
    # Usage summed across both API round-trips.
    assert result.input_tokens == 20
    assert result.output_tokens == 10

    # Wire shape: first call advertised tools; second call echoed the
    # assistant turn and fed the tool_result back under the same id.
    messages_api = adapter._provider.client.messages
    assert len(messages_api.calls) == 2
    first, second = messages_api.calls
    assert [t["name"] for t in first["tools"]] == ["send_message"]
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1"
    assert tool_result["is_error"] is False


def test_core_fallback_not_taken_when_send_message_tool_used(tmp_path):
    """End-to-end through the core turn-router: a structured
    send_message call must suppress the raw-text fallback post
    (handle_message returns None — nothing leaks to the channel)."""
    sent: list[dict] = []
    agent = PuffoAgent(
        adapter=_adapter_with_dispatch(_TOOL_USE_TURN(), sent),
        system_prompt="you are a test bot",
        memory_dir=str(tmp_path),
    )
    result = asyncio.run(agent.handle_message(
        channel_id="ch_abc",
        channel_name="general",
        sender="u",
        sender_email="u@x",
        text="hi",
        post_id="p-1",
        root_id="",
    ))
    assert result is None  # fallback NOT taken
    assert len(sent) == 1  # the reply went out via the tool


def test_dispatch_error_feeds_is_error_tool_result_and_no_target():
    """A failing tool call is fed back as an is_error tool_result and
    does NOT count as a landed send — the fallback stays available."""

    async def broken_send(**kwargs) -> str:
        raise RuntimeError("channel not found")

    adapter = ChatOnlyAdapter(_provider(_TOOL_USE_TURN()))
    adapter.tool_dispatch = {"send_message": broken_send}

    result = asyncio.run(adapter.run_turn(_ctx()))

    assert result.metadata["send_message_targets"] == []
    assert result.metadata["tool_names"] == ["send_message"]
    second_call = adapter._provider.client.messages.calls[1]
    tool_result = second_call["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "channel not found" in tool_result["content"]


# ── plain-text path: no tool → normal return ────────────────────────────────


def test_plain_text_response_returns_normally():
    """No tool_use in the response: single API call, reply returned,
    no send targets — the core's normal routing applies."""
    sent: list[dict] = []
    adapter = _adapter_with_dispatch(
        [_response([_text_block("Hello!")], stop_reason="end_turn")], sent,
    )

    result = asyncio.run(adapter.run_turn(_ctx()))

    assert result.reply == "Hello!"
    assert result.metadata["send_message_targets"] == []
    assert result.metadata["assistant_text_parts"] == ["Hello!"]
    assert sent == []
    assert len(adapter._provider.client.messages.calls) == 1


def test_no_dispatch_injected_keeps_legacy_single_completion():
    """Without an injected dispatch (e.g. tests, misconfigured agent)
    the provider is called with NO tools — legacy behavior."""
    adapter = ChatOnlyAdapter(
        _provider([_response([_text_block("plain")], stop_reason="end_turn")]),
    )

    result = asyncio.run(adapter.run_turn(_ctx()))

    assert result.reply == "plain"
    assert "tools" not in adapter._provider.client.messages.calls[0]


def test_legacy_tuple_provider_still_supported():
    """OpenAI-style providers return (reply, in, out) and don't take
    tool kwargs — the adapter must keep them working unchanged."""

    class _TupleProvider:
        def complete(self, system_prompt, messages):
            return "legacy reply", 3, 4

    adapter = ChatOnlyAdapter(_TupleProvider())
    adapter.tool_dispatch = {"send_message": None}  # ignored: no supports_tools

    result = asyncio.run(adapter.run_turn(_ctx()))

    assert result.reply == "legacy reply"
    assert result.input_tokens == 3
    assert result.output_tokens == 4
    assert result.metadata == {}
