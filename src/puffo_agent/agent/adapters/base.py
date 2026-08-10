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

from ..context_controller import (
    AdmissionCallback,
    CompactionResult,
    ContextCapabilities,
    ContextSnapshot,
    ProviderAdmissionEvent,
    RolloverResult,
    normalize_context_snapshot,
)

logger = logging.getLogger(__name__)


ProgressCallback = Callable[[str], Awaitable[None]]


# Agent marker for "deliberately not replying" — suppresses fallback + report.
SILENT_MARKER = "[SILENT]"

# Cap on text previews in agent.status payloads (the UI truncates further).
STATUS_PREVIEW_CHARS = 200


def is_silent(text: str) -> bool:
    return SILENT_MARKER in text


def anthropic_base_url_env(base_url: str) -> dict[str, str]:
    """Map an Anthropic-compatible LLM base URL to the subprocess env
    override that routes model calls through it.

    Returns ``{"ANTHROPIC_BASE_URL": base_url}`` when ``base_url`` is a
    non-empty string, else ``{}`` — so an absent/empty base URL leaves
    the spawn env untouched (vendor endpoint, today's behavior). Shared
    by the cli-local Driver preparer so the mapping stays unit-testable
    without importing a vendor Python SDK.
    """
    if base_url:
        return {"ANTHROPIC_BASE_URL": base_url}
    return {}


# Refresh when fewer than this many seconds remain on the access
@dataclass
class TurnContext:
    """One turn of input. ``workspace_dir`` / ``claude_dir`` /
    ``memory_dir`` are absolute paths the adapter may bind-mount or
    pass to its runtime.
    """

    system_prompt: str
    messages: list[dict]
    workspace_dir: str = ""
    claude_dir: str = ""
    memory_dir: str = ""
    on_progress: Optional[ProgressCallback] = None
    # Driver-backed runtimes allocate these logical references. Defaults keep
    # every legacy Adapter caller source-compatible.
    session_ref: str = ""
    turn_ref: str = ""
    trusted_context_refs: tuple[str, ...] = ()


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

    # Durable reads become model-visible only after the adapter's provider
    # completion correlation proves that the returned content reached the
    # active Turn. A genuinely in-process adapter may override this with its
    # local tool-return boundary when the handler and model share one process.
    tool_result_admission_boundary = "provider_completion"

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

    async def reload(
        self,
        new_system_prompt: str,
        *,
        with_session: bool = False,
    ) -> None:
        """Drop cached runtime state so the next turn re-reads config
        from disk. ``with_session=True`` also unlinks the session
        sentinel so the next spawn skips ``--resume``. Adapters without
        cached runtime state keep the default no-op."""
        return None

    async def run_retry_turn(
        self,
        kick_text: str,
        fallback_user_message: str,
        ctx: TurnContext,
    ) -> TurnResult:
        """Retry the most recent turn after an ``AgentAPIError``.

        Default: adapters without a resumable session cannot use the kick
        on its own;
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
            session_ref=ctx.session_ref,
            turn_ref=ctx.turn_ref,
            trusted_context_refs=ctx.trusted_context_refs,
        )
        return await self.run_turn(ctx_fallback)

    async def aclose(self) -> None:
        """Release runtime resources (containers, subprocesses, MCP
        servers). Default no-op.
        """
        return None

    async def health_probe(self) -> bool:
        """Verify the runtime can reach its provider after a recovery
        respawn — the worker calls it once post-``warm()`` to reassert
        ``auth_failed`` if the round-trip still fails. Default-True;
        only the Codex override does a real subprocess/thread probe."""
        return True

    async def get_context_snapshot(self) -> ContextSnapshot:
        """Best available context estimate.

        The compatible default is explicitly estimated and uses the package's
        labeled fallback; it does not pretend that a provider measured it.
        """
        return normalize_context_snapshot(
            used_tokens=0,
            estimated_source="adapter_estimated_fallback_200000",
        )

    def get_context_capabilities(self) -> ContextCapabilities:
        return ContextCapabilities(
            diagnostic="adapter has no native context measurement or control",
        )

    async def compact_context(self) -> CompactionResult:
        return CompactionResult(
            completed=False,
            provider_session_id=self.get_provider_session_id(),
            diagnostic="native compaction unsupported",
        )

    async def rollover_context(self) -> RolloverResult:
        return RolloverResult(
            completed=False,
            previous_provider_session_id=self.get_provider_session_id(),
            diagnostic="session rollover unsupported",
        )

    def get_provider_session_id(self) -> str | None:
        return None

    def register_admission_callback(
        self,
        callback: AdmissionCallback | None,
        planning_cycle_key: str = "",
    ) -> None:
        """Register a one-shot callback for the next provider admission."""
        self._context_admission_callback = callback
        self._context_admission_planning_cycle_key = planning_cycle_key

    # More explicit alias used by some integration call sites.
    register_provider_admission_callback = register_admission_callback

    def register_continuation_callback(
        self,
        callback: AdmissionCallback | None,
        planning_cycle_key: str = "",
        *,
        channel_id: str = "",
        tool_names: tuple[str, ...] = (),
        tool_arguments: dict[str, object] | None = None,
        correlation_receipt: str = "",
    ) -> None:
        """Register model-visible admission for a returned tool result.

        Stateful adapters override this to correlate the callback with the
        provider's concrete tool-result event. The default preserves
        compatibility for adapters without live continuation events.
        """
        if tool_names or tool_arguments is not None or correlation_receipt:
            raise RuntimeError(
                "adapter cannot correlate a concrete provider tool result"
            )
        self.register_admission_callback(callback, planning_cycle_key)

    async def _fire_admission_callback(
        self,
        event: ProviderAdmissionEvent,
    ) -> None:
        # Consume before awaiting: duplicate events and callback failures cannot
        # invoke the same callback twice.
        callback = getattr(self, "_context_admission_callback", None)
        self._context_admission_callback = None
        if callback is not None:
            await callback(event)


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
