"""Message transport interface for agent workers.

The worker owns the LLM runtime loop; message clients own chat
transport details and feed thread batches into that loop.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine, Protocol


BatchCallback = Callable[[str, list[dict], dict], Coroutine[Any, Any, Any]]
RetryCallback = Callable[[str, list[dict], dict], Coroutine[Any, Any, Any]]
AbandonCallback = Callable[[str, list[dict], dict, int], Coroutine[Any, Any, Any]]


class MessageClient(Protocol):
    async def listen(
        self,
        on_message: BatchCallback,
        on_api_error_retry: RetryCallback | None = None,
        on_api_error_abandon: AbandonCallback | None = None,
        on_turn_success: BatchCallback | None = None,
    ) -> None:
        """Run the transport listener until stopped."""

    async def send_fallback_message(
        self, channel_id: str, text: str, root_id: str = "",
    ) -> None:
        """Post a fallback reply when the agent did not use a send tool."""

    async def stop(self) -> None:
        """Release transport resources."""
