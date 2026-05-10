"""Adapter interface.

Adapters translate ``TurnContext`` into a runtime-native invocation,
forward output back as a ``TurnResult``, and manage the runtime
instance's lifecycle. The runtime owns the agentic loop and tool
catalog.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


logger = logging.getLogger(__name__)


ProgressCallback = Callable[[str], Awaitable[None]]


# Refresh when fewer than this many seconds remain on the access
# token. Anthropic's OAuth endpoint refuses to rotate a token that's
# more than ~10 min from expiry (it returns the existing token
# unchanged), so 5 min lands inside the accept window while still
# giving the next worker tick room to retry.
CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS = 5 * 60


# Daemon-wide mutex across every Adapter instance. OAuth uses rotating
# refresh tokens — concurrent refreshes race and one gets
# ``invalid_grant``. The first agent to grab the lock refreshes; late
# arrivals SKIP (don't queue) and pick up the new file on their next
# tick.
_REFRESH_LOCK = asyncio.Lock()


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

    # Health from the most recent refresh-ping/smoke-test probe.
    # ``None`` = never checked, ``True`` = OK, ``False`` = auth failure
    # (401 / authentication_error). Read by the worker to surface
    # ``auth_failed`` in status output.
    auth_healthy: bool | None = None

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

    async def refresh_ping(self) -> None:
        """Force an OAuth round-trip so Anthropic's rotating refresh
        token gets exchanged before the access token dies. Guarded by
        a daemon-wide mutex so concurrent agents don't dogpile the
        endpoint — first wins, others skip. Subclass hooks:
        ``_credentials_expires_in_seconds`` (TTL probe) and
        ``_run_refresh_oneshot`` (actual refresh). SDK / chat-only
        adapters short-circuit via the default ``None`` TTL.
        """
        expires_in_before = self._credentials_expires_in_seconds()
        if expires_in_before is None:
            return
        if expires_in_before > CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS:
            logger.debug(
                "credentials fresh (expires in %ds), skipping refresh ping",
                expires_in_before,
            )
            return

        # Don't queue behind an in-flight refresh — next tick will
        # see the freshly-written file.
        if _REFRESH_LOCK.locked():
            logger.debug(
                "another agent is refreshing; skipping this tick "
                "(expires in %ds; next tick will see fresh file)",
                expires_in_before,
            )
            return

        async with _REFRESH_LOCK:
            # Re-check after acquiring; another agent may have just
            # finished refreshing.
            expires_in_recheck = self._credentials_expires_in_seconds()
            if expires_in_recheck is None:
                logger.warning(
                    "refresh_ping: credentials file disappeared "
                    "between threshold check and lock acquire"
                )
                return
            if expires_in_recheck > CREDENTIAL_REFRESH_BEFORE_EXPIRY_SECONDS:
                logger.info(
                    "credentials refreshed by another agent "
                    "(expires in %ds); skipping", expires_in_recheck,
                )
                return

            logger.info(
                "credentials expire in %ds — running refresh ping",
                expires_in_recheck,
            )
            try:
                await self._run_refresh_oneshot()
            except Exception as exc:
                logger.warning("refresh_ping failed: %s", exc)
                return

            expires_in_after = self._credentials_expires_in_seconds()
            if expires_in_after is None:
                logger.warning(
                    "refresh_ping ran but credentials file is no "
                    "longer readable (was expiring in %ds)",
                    expires_in_recheck,
                )
                return
            logger.info(
                "credentials refreshed: expires in %ds (was %ds)",
                expires_in_after, expires_in_recheck,
            )
            if expires_in_after <= expires_in_recheck:
                logger.warning(
                    "refresh_ping ran but token expiry didn't advance "
                    "— claude may not be rewriting the credentials "
                    "file; check OAuth state"
                )

    def _credentials_expires_in_seconds(self) -> int | None:
        """Seconds until the OAuth access token expires (negative if
        already past). ``None`` means "not OAuth" (SDK / chat-only)
        or "file unreadable", both of which short-circuit
        ``refresh_ping``. Subclass hook.
        """
        return None

    async def _run_refresh_oneshot(self) -> None:
        """Spawn a short-lived claude invocation that forces an auth
        round-trip and writes a refreshed token back to
        ``.credentials.json``. Must NOT reuse the long-lived session
        — the credentials-write path only fires on process exit.
        Subclass hook; default no-op for SDK / chat-only.
        """
        return None

    async def aclose(self) -> None:
        """Release runtime resources (containers, subprocesses, MCP
        servers). Default no-op.
        """
        return None


# Case-insensitive substrings that mark a claude CLI output as an
# auth failure rather than a real reply. Kept deliberately strong so
# a user asking about HTTP auth doesn't flip the health flag.
_AUTH_FAILURE_SIGNATURES = (
    "api error: 401",
    "invalid authentication credentials",
    '"type":"authentication_error"',
    "authentication_error",
    "invalid_grant",
    "please run /login",
    "please run `claude /login`",
    "run `claude login`",
)


def looks_like_auth_failure(*parts: str) -> bool:
    """True if any string contains a claude auth-failure signature.
    Case-insensitive.
    """
    for p in parts:
        if not p:
            continue
        low = p.lower()
        if any(sig in low for sig in _AUTH_FAILURE_SIGNATURES):
            return True
    return False


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
