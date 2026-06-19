"""Adapter interface.

Adapters translate ``TurnContext`` into a runtime-native invocation,
forward output back as a ``TurnResult``, and manage the runtime
instance's lifecycle. The runtime owns the agentic loop and tool
catalog.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


logger = logging.getLogger(__name__)


ProgressCallback = Callable[[str], Awaitable[None]]


# Refresh when fewer than this many seconds remain on the access
@dataclass
class TurnContext:
    """One turn of input. ``workspace_dir`` / ``claude_dir`` /
    ``memory_dir`` are absolute paths the adapter may bind-mount or
    pass to its runtime; chat-only adapters ignore them.
    """
    system_prompt: str
    messages: list[dict]
    workspace_dir: str = ""
    claude_dir: str = ""
    memory_dir: str = ""
    on_progress: Optional[ProgressCallback] = None


@dataclass
class TurnResult:
    """One turn of output. ``reply == ""`` or ``"[SILENT]"`` means
    "don't post" — the shell maps both to no-op.
    """
    reply: str
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    metadata: dict = field(default_factory=dict)


class Adapter(ABC):
    """Base class for all runtime adapters."""

    @abstractmethod
    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        """Execute one turn against the underlying runtime."""

    async def warm(self, system_prompt: str) -> None:
        """Pre-spawn long-lived runtime state so the first turn
        doesn't pay startup latency. Worker calls this after
        construction if the agent has a persisted session to resume.
        Default no-op for stateless adapters.
        """
        return None

    async def reload(self, new_system_prompt: str) -> None:
        """Drop cached runtime state so the next turn re-reads
        CLAUDE.md / profile / memory from disk. Worker calls this
        between turns after a ``reload_system_prompt`` MCP tool call.
        CLI adapters close their long-lived claude subprocess (the
        container stays up); SDK / chat-only adapters pass system
        prompt per turn anyway, so the default no-op is correct.
        """
        return None

    async def run_retry_turn(
        self,
        kick_text: str,
        fallback_user_message: str,
        ctx: TurnContext,
    ) -> TurnResult:
        """Retry the most recent turn after an ``AgentAPIError``.

        Default: stateless adapters (SDK, chat-only) have no
        resumable session, so the kick is meaningless on its own;
        send the full ``fallback_user_message`` as a normal turn.
        cli adapters override this to send the cheap kick on
        ``--resume`` success and fall back to the full payload only
        when ``--resume`` failed.
        """
        ctx_fallback = TurnContext(
            system_prompt=ctx.system_prompt,
            messages=[{"role": "user", "content": fallback_user_message}],
            workspace_dir=ctx.workspace_dir,
            claude_dir=ctx.claude_dir,
            memory_dir=ctx.memory_dir,
            on_progress=ctx.on_progress,
        )
        return await self.run_turn(ctx_fallback)

    async def aclose(self) -> None:
        """Release runtime resources (containers, subprocesses, MCP
        servers). Default no-op.
        """
        return None

    async def health_probe(self) -> bool:
        """Verify the runtime can reach its provider after a recovery
        respawn. Worker calls this once post-``warm()`` so the
        ``on_refresh_success`` eager-clear of ``runtime.health =
        auth_failed`` can be reasserted if the round-trip still
        fails. Default-True for adapters without a meaningful
        round-trip probe — only the Codex (subprocess + thread/start)
        override needs a real probe; see PUF-311.
        """
        return True


def format_history_as_prompt(messages: list[dict]) -> str:
    """Render shell conversation history as a single prompt string.
    Used by the SDK adapter (one-shot per turn); CLI adapters keep a
    long-lived session that owns its own transcript.
    """
    if not messages:
        return ""
    if len(messages) == 1:
        return messages[0]["content"]
    parts = ["<prior_turns>"]
    for m in messages[:-1]:
        parts.append(f"[{m['role']}]\n{m['content']}")
    parts.append("</prior_turns>")
    parts.append(messages[-1]["content"])
    return "\n\n".join(parts)
