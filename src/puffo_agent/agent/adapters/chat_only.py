"""Chat-only adapter.

Wraps the message-completion providers (Anthropic/OpenAI) so existing
agents keep working without the SDK or CLI runtimes. Does not touch
the filesystem and ignores workspace/claude dirs, but can run the
puffo_core send tools when the worker injects ``tool_dispatch`` and
the provider supports structured tool use (AnthropicProvider) — the
model's ``send_message(...)`` then posts for real instead of leaking
as plain text through the core turn-router's fallback.
"""

from __future__ import annotations

import asyncio
import logging

from .base import Adapter, TurnContext, TurnResult

logger = logging.getLogger(__name__)

# Ceiling on one in-turn tool execution; a hung network call surfaces
# as an is_error tool_result instead of wedging the turn thread.
_TOOL_CALL_TIMEOUT_SECONDS = 120.0

# Tool names whose successful call means "the reply was posted via
# puffo_core" — the core turn-router skips its raw-text fallback when
# metadata carries a target for one of these.
_SEND_TOOL_NAMES = ("send_message", "send_message_with_attachments")

# Anthropic tool schemas for the puffo_core send tools, mirroring the
# ``mcp.puffo_core_tools`` handler signatures + docstrings. Only tools
# present in the injected dispatch are advertised to the model.
CHAT_TOOL_SCHEMAS: dict[str, dict] = {
    "send_message": {
        "name": "send_message",
        "description": (
            "Post a message to a Puffo.ai channel or DM a user. This is "
            "how you reply — call it instead of writing the reply as "
            "plain text. channel: '@<slug>' for a DM (e.g. '@alice-1234') "
            "or a raw channel id (e.g. 'ch_<uuid>'); '#name' shortcuts "
            "are not supported."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": (
                        "'@<slug>' for a DM, or a raw channel id "
                        "('ch_<uuid>')."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Message body. Markdown preserved verbatim.",
                },
                "root_id": {
                    "type": "string",
                    "description": (
                        "Optional — reply inside a thread; pass the "
                        "envelope_id of the message you're replying to."
                    ),
                },
                "visibility_level": {
                    "type": "string",
                    "enum": ["human", "default", "agent_only"],
                    "description": (
                        "'human' for anything a person should read, "
                        "'default' for agent-to-agent chatter (folded in "
                        "human clients), 'agent_only' to skip the "
                        "DM/@-mention visibility safety net."
                    ),
                },
            },
            "required": ["channel", "text"],
        },
    },
    "send_message_with_attachments": {
        "name": "send_message_with_attachments",
        "description": (
            "Send a message carrying one or more workspace files to a "
            "channel or DM. All files ride in a single envelope."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Workspace-relative file paths. '..' and absolute "
                        "paths are rejected."
                    ),
                },
                "channel": {
                    "type": "string",
                    "description": (
                        "Same syntax as send_message ('@<slug>' or a raw "
                        "channel id)."
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": "Optional text alongside the files.",
                },
                "root_id": {
                    "type": "string",
                    "description": (
                        "Optional thread reply, same semantics as "
                        "send_message's root_id."
                    ),
                },
                "visibility_level": {
                    "type": "string",
                    "enum": ["human", "default", "agent_only"],
                    "description": "Same semantics as send_message.",
                },
            },
            "required": ["paths", "channel"],
        },
    },
}


class ChatOnlyAdapter(Adapter):
    def __init__(self, provider):
        # ``provider`` exposes blocking ``complete(...)``. Legacy
        # providers (OpenAI) return ``(str, int, int)``; tool-capable
        # providers (AnthropicProvider, ``supports_tools = True``)
        # accept ``tools``/``dispatch`` kwargs and return a
        # ``CompletionResult``.
        self._provider = provider
        # ``{tool_name: async handler}`` injected by worker.py when
        # the agent has a puffo_core block. None → legacy text-only
        # turns, byte-identical to the pre-tool behavior.
        self.tool_dispatch: dict | None = None

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        tools = self._advertised_tools()
        if tools:
            dispatch = self._make_dispatch(asyncio.get_running_loop())
            result = await asyncio.to_thread(
                self._provider.complete,
                ctx.system_prompt,
                ctx.messages,
                tools,
                dispatch,
            )
        else:
            result = await asyncio.to_thread(
                self._provider.complete, ctx.system_prompt, ctx.messages,
            )

        # Legacy provider shape (OpenAI, or a test double): plain
        # 3-tuple, no tool metadata — unchanged behavior.
        if isinstance(result, tuple):
            reply, input_tokens, output_tokens = result
            return TurnResult(
                reply=reply,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                tool_calls=0,
            )

        # CompletionResult: mirror SDKAdapter's metadata contract so
        # the core turn-router (core.py reply routing) recognizes a
        # send_message call and skips the raw-text fallback.
        tool_names_used: list[str] = []
        send_message_targets: list[dict] = []
        for call in result.tool_calls:
            name = call.get("name", "")
            tool_names_used.append(name)
            if name in _SEND_TOOL_NAMES and not call.get("is_error"):
                tool_input = call.get("input") or {}
                send_message_targets.append({
                    "channel": str(tool_input.get("channel", "")),
                    "root_id": str(tool_input.get("root_id", "")),
                })
        return TurnResult(
            reply=result.reply_text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            tool_calls=len(result.tool_calls),
            metadata={
                "tool_names": tool_names_used,
                "send_message_targets": send_message_targets,
                "assistant_text_parts": list(result.assistant_text_parts),
            },
        )

    def _advertised_tools(self) -> list[dict] | None:
        """Anthropic tool schemas to advertise this turn, or None when
        the turn should stay a plain completion (no dispatch injected,
        or the provider can't do structured tool use)."""
        if not self.tool_dispatch:
            return None
        if not getattr(self._provider, "supports_tools", False):
            return None
        tools = [
            CHAT_TOOL_SCHEMAS[name]
            for name in CHAT_TOOL_SCHEMAS
            if name in self.tool_dispatch
        ]
        return tools or None

    def _make_dispatch(self, loop: asyncio.AbstractEventLoop):
        """Blocking ``dispatch(name, input) -> str`` for the provider's
        agentic loop. ``complete()`` runs in a worker thread (via
        ``asyncio.to_thread``), so the async puffo_core handlers are
        scheduled back onto the daemon event loop and awaited from the
        thread. Exceptions propagate to the provider, which feeds them
        back to the model as ``is_error`` tool_results.
        """
        handlers = dict(self.tool_dispatch or {})

        def dispatch(tool_name: str, tool_input: dict) -> str:
            handler = handlers.get(tool_name)
            if handler is None:
                raise RuntimeError(
                    f"tool {tool_name!r} is not available on this agent"
                )
            future = asyncio.run_coroutine_threadsafe(
                handler(**tool_input), loop,
            )
            return str(future.result(timeout=_TOOL_CALL_TIMEOUT_SECONDS))

        return dispatch
