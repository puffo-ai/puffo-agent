"""Chat-only adapter.

Wraps the message-completion providers (Anthropic/OpenAI) so existing
agents keep working without the SDK or CLI runtimes. Does not run
tools, does not touch the filesystem, ignores workspace/claude dirs.
"""

from __future__ import annotations

import asyncio

from .base import Adapter, TurnContext, TurnResult


class ChatOnlyAdapter(Adapter):
    def __init__(self, provider):
        # ``provider`` exposes blocking
        # ``complete(system_prompt, messages) -> (str, int, int)``.
        self._provider = provider

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        reply, input_tokens, output_tokens = await asyncio.to_thread(
            self._provider.complete, ctx.system_prompt, ctx.messages,
        )
        return TurnResult(
            reply=reply,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=0,
        )
