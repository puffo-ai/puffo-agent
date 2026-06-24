"""``codex app-server`` session — JSON-RPC over stdio.

Lifecycle mirror of :mod:`cli_session.ClaudeSession`, but the wire
protocol is JSON-RPC notifications + requests, not Claude Code's
stream-json. The codex App Server gives us:

  * ``newConversation`` request → returns a ``conversationId`` we
    persist (same on-disk slot as ``ClaudeSession``'s session id, but
    under a separate ``codex_session.json`` filename so the two never
    collide if an operator flips harnesses on the same agent).
  * ``sendUserTurn`` request that streams server-initiated
    notifications back: ``item/started``, ``item/agentMessage/delta``,
    tool-call progress, ``item/completed``, and finally
    ``turn/completed``. We assemble the reply from the
    ``agent_message`` items in order.
  * Server-initiated approval requests (``applyPatchApproval`` /
    ``execCommandApproval``) which we auto-decide based on the agent's
    permission stance (``bypassPermissions`` for v1).

System prompt is delivered out-of-band as ``$CODEX_HOME/AGENTS.md``;
codex reads it on conversation start. The plan doc's Phase 0 #1 leaves
open whether ``newConversation`` (or ``sendUserTurn``) can override
instructions per-turn — we keep a ``current_instructions`` field that
``reload()`` updates and ``run_turn`` passes through on every send,
so if/when the server-side override exists we just plumb it. Until
then, ``reload()`` falls back to "re-write AGENTS.md and restart the
conversation" — losing history on reload, but that's the v1 trade-off
when the server doesn't expose a hot-swap.

This file is *alpha* — the codex App Server JSON-RPC contract is still
pre-1.0 upstream and several method names / payload shapes here are
best-effort guesses keyed off the published codex-rs/app-server
README. The wire glue lives in two small dispatch helpers
(``_send_request``, ``_handle_notification``) so the entire surface
moves in one place when we learn the real shape from the field.
"""

from __future__ import annotations

import re

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .base import TurnResult
from .cli_session import AuditLog

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Wire-protocol constants (Phase 0 verify targets)
# ─────────────────────────────────────────────────────────────────────────────

# Both slash- and dot-separated method names appear in codex docs;
# normalise on first character of separator so we match either.
_NOTIFICATION_PREFIXES = (
    "item/", "item.",
    "turn/", "turn.",
)

# Methods we issue to the App Server. These names are pinned from the
# live server's error response (it helpfully enumerates every method
# it knows about when handed an unknown one) — codex 0.x rejects
# camelCase ``sendUserTurn``/``newConversation`` and ships the
# slash-namespaced ``thread/*`` and ``turn/*`` families instead.
METHOD_INITIALIZE = "initialize"
METHOD_NEW_CONVERSATION = "thread/start"
METHOD_SEND_USER_TURN = "turn/start"
METHOD_RESUME_CONVERSATION = "thread/resume"
METHOD_INTERRUPT_TURN = "turn/interrupt"

# How long to wait for the App Server to acknowledge a request before
# giving up. The first ``newConversation`` after spawn can be slow
# (cold model load); ``sendUserTurn`` should be quick to ACK
# (streaming starts immediately after).
REQUEST_TIMEOUT_SECONDS = 60.0

# Turn-level timeout — wall-clock budget for a single user turn from
# send to ``turn/completed``. Generous; the daemon also caps things
# at the worker level. Mostly defensive against a wedged App Server
# that ACKs the request but never streams.
TURN_TIMEOUT_SECONDS = 600.0

# Reacts fast in a small fleet, absorbs single transient hiccups.
CODEX_THREAD_WEDGED_THRESHOLD = 2

# Tuple shape so a future "thread is dead" surface adds one line.
_CODEX_THREAD_LIMIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"agent thread limit reached", re.IGNORECASE),
)


def _looks_like_codex_thread_limit(err_text: str) -> bool:
    return any(p.search(err_text or "") for p in _CODEX_THREAD_LIMIT_PATTERNS)


# Verbatim Codex auth-failure signals — anchored so we don't auto-flip on
# legitimate model/quota errors. ``invalid thread id ... found 0`` is a
# downstream symptom of an empty conversation_id, not auth — kept out.
# ``invalidated oauth token`` is codex's human-readable form (distinct from
# the ``token_invalidated`` JSON field), observed live on a relogin.
_CODEX_AUTH_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"refresh token (?:was )?revoked", re.IGNORECASE),
    re.compile(r"\btoken_invalidated\b", re.IGNORECASE),
    re.compile(r"invalidated\s+oauth\s+token", re.IGNORECASE),
)

# A 401 in an OAuth/auth context. Codex surfaces a broken token as a 401 on
# its API endpoints (``/responses``, ``/backend-api/codex/...``), so require
# an auth-context marker within the same clause as the ``401`` rather than
# pinning a single path — bounded distance + no sentence break keeps an
# unrelated 401 in a log line from tripping it.
_AUTH_401_CONTEXT = (
    r"(?:oauth|invalidated|auth[\s_]error|identity_edge|/responses|/backend-api/codex)"
)
_CODEX_AUTH_401_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\b401\b[^.;\n]{{0,40}}{_AUTH_401_CONTEXT}", re.IGNORECASE),
    re.compile(rf"{_AUTH_401_CONTEXT}[^.;\n]{{0,40}}\b401\b", re.IGNORECASE),
)


def _looks_like_codex_auth_error(err_text: str) -> bool:
    """True iff ``err_text`` carries a Codex auth signal (``refresh token
    (was) revoked`` / ``token_invalidated`` / ``invalidated oauth token`` /
    a clause-bound 401-in-auth-context). Drives converting a ``turn_failed``
    into an ``AgentAPIError(is_auth=True)`` so the worker's auth_failed
    substrate fires."""
    text = err_text or ""
    if any(p.search(text) for p in _CODEX_AUTH_ERROR_PATTERNS):
        return True
    return any(p.search(text) for p in _CODEX_AUTH_401_PATTERNS)

# Tool names of the puffo MCP server's "this counts as posting a
# reply" family. When the agent invokes one of these and it completes
# successfully, the worker treats the turn as "agent already replied"
# (skipping the [SILENT]-fallback shell auto-post). Names match
# ``mcp.config.PUFFO_CORE_TOOL_NAMES`` — kept inline as a frozenset to
# avoid an import-cycle with the MCP package.
_PUFFO_SEND_MESSAGE_TOOLS = frozenset({
    "send_message",
    "send_message_with_attachments",
})


# StreamReader buffer size for the codex subprocess's stdout. The
# asyncio default is 64 KiB; single notifications from codex
# (mcpServer/startupStatus/updated carrying the full tool catalog,
# thread/started carrying a session snapshot, etc.) routinely exceed
# that, which raises ``LimitOverrunError`` from ``readline()`` and
# wedges the reader loop. 16 MiB matches ClaudeSession's choice —
# bounds per-agent memory while comfortably covering every event size
# seen in practice.
STREAM_READER_LIMIT_BYTES = 16 * 1024 * 1024


# ─────────────────────────────────────────────────────────────────────────────
# Session state machine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _PendingRequest:
    """An in-flight JSON-RPC request awaiting a matching response."""
    future: asyncio.Future
    method: str
    started_at: float


@dataclass
class _PendingTurn:
    """An in-flight ``sendUserTurn`` accumulating streaming items."""
    request_id: int
    started_at: float
    # Reply text accumulated from ``agent_message`` deltas. Final
    # value is the joined assistant output.
    reply_chunks: list[str] = field(default_factory=list)
    # ``tool_use`` events counted for TurnResult.tool_calls metric.
    tool_calls: int = 0
    # When the agent invoked a puffo MCP send-message tool, the worker
    # reads this list off TurnResult.metadata to decide "agent already
    # posted; don't run the [SILENT]-fallback path". Each entry mirrors
    # the claude-code adapter's shape: ``{channel, root_id}``.
    send_message_targets: list[dict] = field(default_factory=list)
    # Usage stats lifted from the final ``turn/completed`` envelope.
    input_tokens: int = 0
    output_tokens: int = 0
    # ``turn/completed`` resolves this; ``turn/failed`` rejects it.
    completed: asyncio.Future = field(default_factory=asyncio.Future)
    # Optional progress callback for in-turn UI updates.
    on_progress: Optional[Callable[[str], Any]] = None


class CodexSession:
    """One ``codex app-server`` process per agent.

    The session owns:
      * the subprocess + its stdio JSON-RPC pump
      * the persisted ``conversationId``
      * the current system-prompt snapshot used for ``newConversation``
      * an event loop reading stdout and dispatching by message type

    Public surface mirrors ``ClaudeSession``::

        cs = CodexSession(...)
        await cs.warm(system_prompt)          # spawn + new/resumeConv
        result = await cs.run_turn(msg, sys)  # sendUserTurn → wait
        await cs.reload(new_sys)              # hot-swap instructions
        await cs.aclose()                     # graceful shutdown
    """

    def __init__(
        self,
        agent_id: str,
        session_file: Path,
        argv: list[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        permission_mode: str = "bypassPermissions",
        sandbox: str = "danger-full-access",
        model: str = "",
        audit: Optional[AuditLog] = None,
    ):
        self.agent_id = agent_id
        self.session_file = session_file
        self.argv = argv
        self.cwd = cwd
        self.env = env
        # ``bypassPermissions`` auto-approves every approval request.
        # The plan's v1 stance — other modes are deferred until the
        # permission-proxy DM flow is ready for codex too.
        self.permission_mode = permission_mode
        self.sandbox = sandbox
        # Codex's thread/start takes ``model`` as a required-ish
        # parameter; empty string means "let codex pick its default".
        self.model = model
        # PUF-324: per-agent NDJSON audit log. Matches the
        # ``ClaudeSession.audit`` contract — each ``assistant.text``
        # streaming delta + ``tool``-use completion gets a row so
        # operators can tail the same ``<workspace>/.puffo-agent/audit.log``
        # regardless of which adapter is driving the agent.
        self.audit = audit

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._next_id: int = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._active_turn: _PendingTurn | None = None
        self._lock = asyncio.Lock()
        self._conversation_id: str = self._load_conversation_id()
        # ``sandbox`` (+ the other thread/start params) aren't re-sent on
        # resume, so a sandbox change would silently keep the old policy.
        # Drop the persisted thread when it was created under a different
        # sandbox; the next start re-applies the current one.
        if self._conversation_id:
            persisted_sandbox = self._load_persisted_sandbox()
            if persisted_sandbox != self.sandbox:
                logger.info(
                    "agent %s: codex sandbox changed (%s → %s); starting a "
                    "fresh thread so the new policy applies",
                    self.agent_id, persisted_sandbox, self.sandbox,
                )
                self._conversation_id = ""
        # The latest system prompt we've been handed. Stored so
        # ``reload`` can detect a no-op vs a real change, and so a
        # respawn can re-issue ``newConversation`` with current
        # instructions when the conversation id is missing or rotted.
        self.current_instructions: str = ""
        # Resets on next success; hits THRESHOLD → rotate (drop the
        # persisted conversation id; next _ensure_running starts fresh).
        self._consecutive_thread_failures: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    async def warm(self, system_prompt: str) -> None:
        """Spawn the App Server + start (or resume) a conversation.
        Idempotent — subsequent calls are no-ops while the process is
        healthy."""
        async with self._lock:
            await self._ensure_running(system_prompt)

    async def run_turn(self, user_message: str, system_prompt: str) -> TurnResult:
        """Send one turn; wait for ``turn/completed``."""
        async with self._lock:
            await self._ensure_running(system_prompt)
            # Defence-in-depth: a non-empty cid is ``_ensure_running``'s
            # contract; never send ``threadId=""`` (a silent wedge) if
            # that ever regresses.
            if not self._conversation_id:
                raise RuntimeError(
                    f"agent {self.agent_id}: codex run_turn aborted — "
                    f"empty conversation_id after _ensure_running"
                )
            turn = _PendingTurn(
                request_id=self._reserve_id(),
                started_at=time.time(),
            )
            self._active_turn = turn

        logger.debug(
            "agent %s: codex turn/start sending (msg_len=%d, thread=%s)",
            self.agent_id, len(user_message), self._conversation_id,
        )
        turn_failed_exc: BaseException | None = None
        rotated_in_branch: bool = False
        try:
            # turn/start params per codex-rs/app-server protocol:
            # ``threadId``, ``input`` (array of structured items —
            # NOT a bare string), plus optional config overrides we
            # leave out for v1.
            turn_response = await self._send_raw_request(
                turn.request_id,
                METHOD_SEND_USER_TURN,
                {
                    "threadId": self._conversation_id,
                    "input": [
                        {"type": "text", "text": user_message},
                    ],
                },
            )
            # Some App Server versions complete the turn synchronously
            # and put the final items in the response payload; others
            # stream item/* + turn/completed notifications and return
            # only ``{turn: {id, status: "running"}}``. Handle both.
            sync_resolved = self._absorb_sync_turn_response(turn, turn_response)
            if sync_resolved:
                # No need to wait — server already gave us everything.
                pass
            else:
                try:
                    await asyncio.wait_for(
                        turn.completed, timeout=TURN_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "agent %s: codex turn timed out after %ds",
                        self.agent_id, TURN_TIMEOUT_SECONDS,
                    )
                    # Best-effort interrupt so the server stops streaming
                    # output we'll never read.
                    try:
                        await self._send_raw_request(
                            self._reserve_id(),
                            METHOD_INTERRUPT_TURN,
                            {
                                "threadId": self._conversation_id,
                                "turnId": turn_response.get("turn", {}).get("id") if isinstance(turn_response, dict) else None,
                            },
                        )
                    except Exception:
                        pass
                    rotated_in_branch = self._propagate_turn_outcome(
                        outcome="timeout",
                    )
                    return TurnResult(
                        reply="",
                        metadata={
                            "codex_turn_timeout": True,
                            "codex_thread_rotated": rotated_in_branch,
                        },
                    )
                except RuntimeError as exc:
                    # Captured for propagation in the finally; re-raised
                    # below so the worker's error path still logs.
                    turn_failed_exc = exc
        finally:
            self._active_turn = None

        if turn_failed_exc is not None:
            err_text = str(turn_failed_exc)
            self._propagate_turn_outcome(
                outcome="turn_failed", err_text=err_text,
            )
            # Codex auth-failures are sticky + operator-actionable (re-run
            # ``codex login``). Convert to AgentAPIError so the worker's
            # auth_failed substrate (state-flip + operator DM + refresher
            # kick) reuses the Claude path.
            if _looks_like_codex_auth_error(err_text):
                from ..core import AgentAPIError
                raise AgentAPIError(
                    f"codex auth failed: {err_text}", is_auth=True,
                ) from turn_failed_exc
            raise turn_failed_exc

        reply = "".join(turn.reply_chunks).strip()
        logger.debug(
            "agent %s: codex turn complete (reply_len=%d, tool_calls=%d, "
            "send_msg_calls=%d, in=%d out=%d)",
            self.agent_id, len(reply), turn.tool_calls,
            len(turn.send_message_targets),
            turn.input_tokens, turn.output_tokens,
        )
        self._propagate_turn_outcome(outcome="success")
        # core.py's reply-routing check: a non-empty
        # ``send_message_targets`` list means "agent already posted via
        # MCP, skip the shell fallback." Mirrors the claude-code adapter
        # shape so the routing logic is harness-agnostic.
        return TurnResult(
            reply=reply,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            tool_calls=turn.tool_calls,
            metadata={
                "harness": "codex",
                "conversation_id": self._conversation_id,
                "send_message_targets": turn.send_message_targets,
            },
        )

    async def reload(self, new_system_prompt: str) -> None:
        """Tear the codex App Server process down so the next turn
        re-spawns it and re-reads config.toml + AGENTS.md."""
        self.current_instructions = new_system_prompt
        async with self._lock:
            await self._teardown_locked()

    async def aclose(self) -> None:
        async with self._lock:
            await self._teardown_locked()

    def has_persisted_session(self) -> bool:
        return bool(self._conversation_id)

    async def health_probe(self) -> bool:
        """Post-respawn round-trip check: ``_ensure_running`` spawns the
        app-server + bootstraps a thread (re-raising an auth error inline
        if the token is still broken), so a non-empty ``_conversation_id``
        afterwards means the handshake + ``thread/start`` succeeded. Any
        exception → False so the worker reasserts ``auth_failed``."""
        try:
            await self._ensure_running(self.current_instructions or "")
        except Exception as exc:
            logger.warning(
                "agent %s: codex health probe failed: %s",
                self.agent_id, exc,
            )
            return False
        return bool(self._conversation_id)

    # ── thread-wedge propagation ──────────────────────────────────────────────

    def _propagate_turn_outcome(
        self,
        *,
        outcome: str,
        err_text: str = "",
    ) -> bool:
        """Returns True iff this call rotated the conversation."""
        if outcome == "success":
            if self._consecutive_thread_failures > 0:
                logger.info(
                    "agent %s: codex turn succeeded after %d non-success tick(s)",
                    self.agent_id, self._consecutive_thread_failures,
                )
            # Unconditional clear guards the daemon-restart-with-stale-disk path.
            self._clear_codex_thread_wedged_health()
            self._consecutive_thread_failures = 0
            return False
        self._consecutive_thread_failures += 1
        immediate = (
            outcome == "turn_failed"
            and _looks_like_codex_thread_limit(err_text or "")
        )
        if immediate or self._consecutive_thread_failures >= CODEX_THREAD_WEDGED_THRESHOLD:
            reason = "thread-limit verbatim" if immediate else (
                f"{self._consecutive_thread_failures} consecutive {outcome}"
            )
            logger.warning(
                "agent %s: rotating codex thread (%s)", self.agent_id, reason,
            )
            self._reset_conversation()
            self._flip_codex_thread_wedged_health(reason)
            # Reset so the fresh thread gets its own THRESHOLD budget.
            self._consecutive_thread_failures = 0
            return True
        return False

    def _reset_conversation(self) -> None:
        """Drop ``conversation_id`` so the next ``_ensure_running`` calls
        ``thread/start``. App Server process stays alive."""
        self._conversation_id = ""
        try:
            self._save_conversation_id("")
        except Exception as exc:
            logger.warning(
                "agent %s: failed to clear persisted conversation_id: %s",
                self.agent_id, exc,
            )

    def _flip_codex_thread_wedged_health(self, reason: str) -> None:
        """``in_progress`` / ``unhandled_error`` are intentionally NOT
        protected — codex_thread_wedged carries more actionable detail."""
        from ...portal.state import RuntimeState
        try:
            rs = RuntimeState.load(self.agent_id)
        except Exception as exc:
            logger.warning(
                "codex_thread_wedged flip: failed to load runtime for "
                "%s: %s", self.agent_id, exc,
            )
            return
        if rs is None:
            return
        if rs.health in ("auth_failed", "api_error_abandoned", "refresh_broken"):
            return
        if rs.health == "codex_thread_wedged":
            return
        rs.health = "codex_thread_wedged"
        rs.error = (
            f"codex thread rotated by daemon ({reason}). Recovery is "
            "automatic on the next inbound message — no operator action "
            "required."
        )
        try:
            rs.save(self.agent_id)
        except Exception as exc:
            logger.warning(
                "codex_thread_wedged flip: failed to save runtime for "
                "%s: %s", self.agent_id, exc,
            )

    def _clear_codex_thread_wedged_health(self) -> None:
        from ...portal.state import RuntimeState
        try:
            rs = RuntimeState.load(self.agent_id)
        except Exception:
            return
        if rs is None or rs.health != "codex_thread_wedged":
            return
        rs.health = "ok"
        rs.error = ""
        try:
            rs.save(self.agent_id)
        except Exception as exc:
            logger.warning(
                "codex_thread_wedged clear: failed to save runtime for "
                "%s: %s", self.agent_id, exc,
            )

    # ── Internals ─────────────────────────────────────────────────────────────

    def _reserve_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def _load_conversation_id(self) -> str:
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(data.get("conversation_id") or "")

    def _load_persisted_sandbox(self) -> str:
        """Sandbox the persisted thread was created under. A missing key
        (pre-sandbox-config session files) defaults to
        ``danger-full-access`` — the old hardcoded value — so agents that
        never changed sandbox don't get reset."""
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "danger-full-access"
        return str(data.get("sandbox") or "danger-full-access")

    def _save_conversation_id(self, cid: str) -> None:
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            self.session_file.write_text(
                json.dumps({"conversation_id": cid, "sandbox": self.sandbox}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "agent %s: couldn't persist codex conversation id: %s",
                self.agent_id, exc,
            )

    async def _ensure_running(self, system_prompt: str) -> None:
        # Snapshot whatever caller hands us; ``run_turn`` re-passes it
        # per turn anyway.
        if system_prompt:
            self.current_instructions = system_prompt
        proc_alive = self._proc is not None and self._proc.returncode is None
        if proc_alive and self._conversation_id:
            return
        # A live proc with an empty cid (corrupt session load + warm
        # race) would make run_turn send ``threadId=""`` and wedge —
        # respawn so _spawn re-establishes a thread (it raises if
        # thread/start returns no id).
        if proc_alive:
            logger.info(
                "agent %s: codex session has alive proc but empty "
                "conversation_id; respawning to recover",
                self.agent_id,
            )
            await self._teardown_locked()
        await self._spawn()

    async def _spawn(self) -> None:
        logger.info(
            "agent %s: spawning codex app-server (argv=%s)",
            self.agent_id, " ".join(self.argv),
        )
        try:
            from ..._proc import no_window_kwargs
            proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
                **no_window_kwargs(),
                # Override asyncio's default 64 KiB StreamReader buffer
                # — codex emits very chunky single-line JSON
                # notifications (full tool catalogs on
                # mcpServer/startupStatus/updated, session snapshots
                # on thread/started, etc.) that overrun it.
                limit=STREAM_READER_LIMIT_BYTES,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"`codex` binary not on PATH: {exc}. Install via "
                "`npm install -g @openai/codex` or the official install "
                "script, then re-run."
            ) from exc

        self._proc = proc
        self._reader_task = asyncio.create_task(
            self._reader_loop(proc.stdout), name=f"codex-reader-{self.agent_id}",
        )
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(proc.stderr), name=f"codex-stderr-{self.agent_id}",
        )

        # Anything below this point that raises must tear the process
        # back down — otherwise ``_ensure_running`` on the next turn
        # sees a "live" proc and skips spawn, sending turn requests
        # against a half-initialised App Server.
        try:
            await self._bootstrap_session()
        except Exception:
            await self._teardown_locked()
            raise

    async def _bootstrap_session(self) -> None:
        """Run the initialize handshake + thread/start (or thread/resume).
        Separated from ``_spawn`` so the spawn path can wrap it in a
        try/except that tears down the proc on any failure."""
        # 1. JSON-RPC initialize handshake. Most JSON-RPC servers
        # require this before accepting other methods; codex is no
        # exception. Send a minimal clientInfo + capabilities envelope
        # and ignore the response — we don't read server capabilities
        # back yet.
        try:
            await self._send_raw_request(
                self._reserve_id(),
                METHOD_INITIALIZE,
                {
                    "clientInfo": {
                        "name": "puffo-agent",
                        "version": "0.10.0a1",
                    },
                    "capabilities": {},
                    # protocolVersion is a polite hint — codex accepts
                    # most values and pins to its own internal version.
                    "protocolVersion": "2025-06-18",
                },
            )
        except Exception as exc:
            # initialize is best-effort: some App Server versions don't
            # require it. We log + continue so older / newer servers
            # both work.
            logger.info(
                "agent %s: codex initialize returned %s (continuing — "
                "initialize is sometimes optional)",
                self.agent_id, exc,
            )

        # 2. Start or resume the conversation/thread.
        if self._conversation_id:
            try:
                await self._send_raw_request(
                    self._reserve_id(),
                    METHOD_RESUME_CONVERSATION,
                    {"threadId": self._conversation_id},
                )
                logger.info(
                    "agent %s: resumed codex thread %s",
                    self.agent_id, self._conversation_id,
                )
                return
            except Exception as exc:
                logger.warning(
                    "agent %s: resume failed (%s); starting fresh "
                    "thread",
                    self.agent_id, exc,
                )
                self._conversation_id = ""

        # thread/start params per codex-rs/app-server-protocol/src/
        # protocol/v2.rs. The on-wire schema is camelCase (NOT
        # snake_case — the Python SDK FAQ that suggested otherwise is
        # describing the Python SDK's wrapper field names, not the
        # wire JSON). The thread-level sandbox field is bare ``sandbox``
        # (not ``sandbox_mode``, not ``sandboxMode``) — a single word.
        #
        # ``approvalPolicy: "never"`` means **auto-approve everything
        # without bothering the client**. Confusing name; verified by
        # live behaviour. Puffo trust model = operator vouches for the
        # agent + machine, all tools allowed.
        #
        # ``sandbox`` is codex's sandbox policy (read-only |
        # workspace-write | danger-full-access), per-agent via agent.yml.
        # Default keeps it fully open — cli-local runs as the operator's
        # UID, so codex's in-process sandbox is mostly cosmetic; the real
        # boundary is cli-docker's container.
        new_conv_params: dict[str, Any] = {
            "cwd": self.cwd or os.getcwd(),
            "approvalPolicy": (
                "never" if self.permission_mode == "bypassPermissions" else "untrusted"
            ),
            "sandbox": self.sandbox,
        }
        if self.model:
            new_conv_params["model"] = self.model

        result = await self._send_raw_request(
            self._reserve_id(),
            METHOD_NEW_CONVERSATION,
            new_conv_params,
        )
        cid = _extract_thread_id(result)
        if not cid:
            raise RuntimeError(
                f"agent {self.agent_id}: codex thread/start returned "
                f"no thread id: {result!r}"
            )
        self._conversation_id = cid
        self._save_conversation_id(self._conversation_id)
        logger.info(
            "agent %s: started codex thread %s",
            self.agent_id, self._conversation_id,
        )


    async def _teardown_locked(self) -> None:
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()
        if self._active_turn is not None and not self._active_turn.completed.done():
            self._active_turn.completed.set_exception(
                RuntimeError("codex session torn down before turn completed"),
            )
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            # EOF lets Rust Drop the sqlite handles synchronously before
            # exit; TerminateProcess would leak them to async kernel cleanup.
            if proc.stdin is not None and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    try:
                        await proc.wait()
                    except Exception:
                        pass
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._reader_task = None
        self._stderr_task = None

    # ── JSON-RPC plumbing ────────────────────────────────────────────────────

    async def _send_raw_request(
        self, request_id: int, method: str, params: dict,
    ) -> Any:
        assert self._proc is not None and self._proc.stdin is not None
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = _PendingRequest(
            future=future, method=method, started_at=time.time(),
        )
        self._proc.stdin.write((body + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise RuntimeError(
                f"agent {self.agent_id}: codex {method} timed out "
                f"after {REQUEST_TIMEOUT_SECONDS}s"
            )

    async def _send_notification(self, method: str, params: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        body = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })
        self._proc.stdin.write((body + "\n").encode("utf-8"))
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _reader_loop(self, stdout: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await stdout.readline()
            except Exception as exc:
                logger.warning(
                    "agent %s: codex stdout read failed: %s",
                    self.agent_id, exc,
                )
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                logger.debug(
                    "agent %s: codex emitted non-JSON line: %s",
                    self.agent_id, text[:200],
                )
                continue
            await self._dispatch_message(msg)

        # EOF — fail every pending request so callers don't hang.
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(
                    RuntimeError("codex app-server closed stdout"),
                )
        self._pending.clear()
        if self._active_turn is not None and not self._active_turn.completed.done():
            self._active_turn.completed.set_exception(
                RuntimeError("codex app-server closed stdout mid-turn"),
            )

    async def _stderr_loop(self, stderr: asyncio.StreamReader) -> None:
        # codex emits structured logs on stderr; surface them at INFO
        # with the agent prefix so they round-trip through the puffo-
        # agent log pipeline.
        while True:
            try:
                line = await stderr.readline()
            except Exception:
                break
            if not line:
                break
            logger.info(
                "agent %s: codex stderr: %s",
                self.agent_id,
                line.decode("utf-8", errors="replace").rstrip(),
            )

    async def _dispatch_message(self, msg: dict) -> None:
        # JSON-RPC response (id present, result|error)
        if "id" in msg and ("result" in msg or "error" in msg):
            req_id = msg["id"]
            pending = self._pending.pop(req_id, None) if isinstance(req_id, int) else None
            if pending is None:
                logger.debug(
                    "agent %s: codex response for unknown id %r",
                    self.agent_id, req_id,
                )
                return
            if "error" in msg:
                err = msg["error"]
                pending.future.set_exception(RuntimeError(
                    f"codex {pending.method} error: {err}"
                ))
            else:
                pending.future.set_result(msg.get("result"))
            return

        # JSON-RPC request from server (e.g. approval prompts)
        method = msg.get("method")
        if not isinstance(method, str):
            return
        if "id" in msg:
            await self._handle_server_request(msg["id"], method, msg.get("params") or {})
            return

        # JSON-RPC notification (one-way)
        await self._handle_notification(method, msg.get("params") or {})

    async def _handle_server_request(
        self, request_id: Any, method: str, params: dict,
    ) -> None:
        """codex app-server sends several flavours of server-initiated
        request mid-turn. Each has a DIFFERENT response shape — schema
        pinned from ``codex-rs/app-server/README.md`` (canonical):

          * ``item/commandExecution/requestApproval``
            → ``{decision: "accept" | "acceptForSession" | "decline" | "cancel" | <nested>}``
          * ``item/fileChange/requestApproval``
            → same ``{decision: ...}`` envelope
          * ``item/permissions/requestApproval``
            → ``{permissions: {...}, scope?: "session"}`` (mirror what
              was requested; omitted entries treated as denied)
          * ``mcpServer/elicitation/request``
            → ``{action: "accept" | "decline" | "cancel", content: {} | null}``
          * ``item/tool/call`` (dynamic tool invocation — server asks
            CLIENT to execute a tool we registered)
            → ``{contentItems: [...], success: bool}``

        We auto-grant everything under ``bypassPermissions`` (puffo
        trust model: operator vouches for the agent + host). Dynamic
        tool calls have no client-side handler yet, so we reply with
        ``success: false`` and an error item.
        """
        accept = self.permission_mode == "bypassPermissions"

        if method == "mcpServer/elicitation/request":
            if accept:
                await self._reply_to_server_request(
                    request_id, {"action": "accept", "content": {}},
                )
            else:
                await self._reply_to_server_request(
                    request_id, {"action": "decline", "content": None},
                )
            return

        if method == "item/commandExecution/requestApproval":
            await self._reply_to_server_request(
                request_id,
                {"decision": "accept" if accept else "decline"},
            )
            return

        if method == "item/fileChange/requestApproval":
            await self._reply_to_server_request(
                request_id,
                {"decision": "accept" if accept else "decline"},
            )
            return

        if method == "item/permissions/requestApproval":
            # Mirror back whatever permissions were requested. The
            # README's example showed result.permissions matching the
            # request's permission shape; we trust the request body
            # since approvalPolicy is "never" + bypassPermissions.
            requested = params.get("permissions") if isinstance(params, dict) else None
            if accept and requested:
                await self._reply_to_server_request(
                    request_id,
                    {"scope": "session", "permissions": requested},
                )
            else:
                await self._reply_to_server_request(
                    request_id, {"permissions": {}},
                )
            return

        if method == "item/tool/call":
            # codex is invoking a client-registered dynamic tool. We
            # don't register any (yet). Respond with the contract
            # shape but mark success=false so codex surfaces the
            # right error to the model.
            await self._reply_to_server_request(
                request_id,
                {
                    "contentItems": [{
                        "type": "errorText",
                        "text": (
                            "puffo-agent doesn't register any "
                            "dynamic tools; this call shape is "
                            "unexpected."
                        ),
                    }],
                    "success": False,
                },
            )
            return

        # Unknown server-initiated request — log and reply with an
        # error so it doesn't wedge the server waiting.
        logger.warning(
            "agent %s: codex sent unknown server request %s; replying "
            "with method-not-found",
            self.agent_id, method,
        )
        await self._reply_to_server_request_error(
            request_id, -32601, f"method not implemented: {method}",
        )

    async def _reply_to_server_request(
        self, request_id: Any, result: Any,
    ) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
        self._proc.stdin.write((body + "\n").encode("utf-8"))
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _reply_to_server_request_error(
        self, request_id: Any, code: int, message: str,
    ) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        })
        self._proc.stdin.write((body + "\n").encode("utf-8"))
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _handle_notification(self, method: str, params: dict) -> None:
        # Normalise slash / dot separators.
        m = method.replace(".", "/").lower()
        turn = self._active_turn

        if m.startswith("item/") and turn is not None:
            await self._handle_item_event(m, params, turn)
            return
        if m.startswith("turn/completed") and turn is not None:
            self._absorb_turn_usage(turn, params)
            if not turn.completed.done():
                turn.completed.set_result(None)
            return
        # codex emits both ``turn/failed`` AND a top-level ``error``
        # notification, depending on whether the error happens
        # client-side validation or upstream from the model. Treat
        # both as turn-fatal so the turn future resolves with a clear
        # error message instead of timing out into "no reply".
        if (m == "error" or m.startswith("turn/failed")) and turn is not None:
            err = (params or {}).get("error") or params or "(no detail)"
            # Codex's ``error`` notification wraps the upstream error
            # JSON-as-string under params.error.message. Unwrap so the
            # operator sees the actual reason ("model not supported"
            # etc.) rather than a JSON-soup blob.
            err_text = _readable_error(err)
            if not turn.completed.done():
                turn.completed.set_exception(
                    RuntimeError(f"codex turn failed: {err_text}"),
                )
            return

    async def _handle_item_event(
        self, normalised_method: str, params: dict, turn: _PendingTurn,
    ) -> None:
        """The ``item/*`` family covers per-step events. We forward
        ``agentMessage`` deltas to the reply buffer, count ``tool_use``
        items, and otherwise pass through silently.

        codex emits two distinct payload shapes here:

          * Streaming delta (``item/agentMessage/delta``):
            ``{threadId, turnId, itemId, delta: "<chunk>"}``
            — text fragment lives at the top level under ``delta``,
            NOT nested under ``item``. Missing this was why
            reply_len was tiny in the first live test: we were
            reading ``params.item.text`` which didn't exist.

          * Completed item (``item/completed``):
            ``{item: {id, type, text, ...}}`` — type is camelCase
            ``"agentMessage"``, full text under ``item.text``.
        """
        # 1. Streaming delta — text at params.delta directly.
        if "delta" in normalised_method:
            delta_text = params.get("delta")
            if isinstance(delta_text, str) and delta_text:
                turn.reply_chunks.append(delta_text)
                # PUF-324: tee streaming narrative into the audit log
                # so the operator can see intermediate ``searching
                # web`` / ``updating code`` text live, matching the
                # claude-code adapter's ``assistant.text`` shape.
                if self.audit is not None:
                    self.audit.write("assistant.text", text=delta_text)
            return
        # 2. Final item — nested under params.item.
        item = params.get("item") or {}
        kind = (item.get("type") or "").lower()
        if normalised_method.endswith("/started"):
            return
        if normalised_method.endswith("/completed"):
            if kind in ("agent_message", "agentmessage"):
                text = item.get("text") or item.get("content") or ""
                if isinstance(text, str) and text:
                    # Prefer the authoritative final text over the
                    # concatenated deltas when they're inconsistent —
                    # protects against missed-delta edge cases.
                    joined = "".join(turn.reply_chunks)
                    if joined.strip() != text.strip():
                        turn.reply_chunks = [text]
                        # PUF-324: when we replace the delta-built
                        # buffer because of the missed-delta path, the
                        # streaming-delta audit rows above don't reflect
                        # the authoritative text. Emit a synthetic
                        # ``assistant.text`` so audit.log still carries
                        # the operator-observable final narrative.
                        if self.audit is not None:
                            self.audit.write(
                                "assistant.text", text=text,
                            )
            elif kind in ("tool_use", "tooluse", "tool_call", "toolcall"):
                turn.tool_calls += 1
                # PUF-324: cross-adapter parity with
                # ``ClaudeSession``'s tool capture. Some codex shapes
                # nest the tool name as ``item.name`` / args as
                # ``item.input``; fall through to empty strings rather
                # than skipping the row entirely so the operator at
                # least sees the count + presence in audit.log.
                if self.audit is not None:
                    self.audit.write(
                        "tool",
                        name=str(item.get("name") or kind),
                        input=item.get("input") or item.get("arguments") or {},
                        id=str(item.get("id") or ""),
                    )
            elif kind == "mcptoolcall":
                # Real codex shape per debug logs: ``item/completed``
                # with ``item.type == "mcpToolCall"``, ``item.server``
                # == server name, ``item.tool`` == tool name,
                # ``item.status`` ∈ {"completed", "failed", ...},
                # ``item.arguments`` == the tool's JSON input.
                turn.tool_calls += 1
                status = (item.get("status") or "").lower()
                server = item.get("server") or ""
                tool = item.get("tool") or ""
                args = item.get("arguments") or {}
                if (
                    server == "puffo"
                    and tool in _PUFFO_SEND_MESSAGE_TOOLS
                    and status == "completed"
                ):
                    # Shape mirrors the claude-code adapter so core.py's
                    # ``send_message_called`` check is identical
                    # regardless of harness.
                    turn.send_message_targets.append({
                        "channel": str(args.get("channel", "")),
                        "root_id": str(args.get("root_id", "")),
                    })
                # PUF-324: cross-adapter audit-log shape — the tool
                # name codex puts on the ``mcpToolCall`` item is
                # ``server__tool`` (e.g. ``puffo__send_message``),
                # mirroring how the claude-code adapter sees these.
                if self.audit is not None:
                    self.audit.write(
                        "tool",
                        name=f"{server}__{tool}" if server and tool else (tool or "mcp"),
                        input=args,
                        id=str(item.get("id") or ""),
                    )
            return

    def _absorb_turn_usage(self, turn: _PendingTurn, params: dict) -> None:
        usage = params.get("usage") or {}
        try:
            turn.input_tokens = int(usage.get("input_tokens") or 0)
            turn.output_tokens = int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            pass

    def _absorb_sync_turn_response(
        self, turn: _PendingTurn, response: Any,
    ) -> bool:
        """Pull the agent reply out of a synchronous ``turn/start``
        response shape (``{turn: {id, status, items, error}}``).

        Returns True when the response already carries a completed
        turn — caller skips waiting on ``turn.completed``. Returns
        False when the response indicates the turn is still running
        (so notifications will drive completion).
        """
        if not isinstance(response, dict):
            return False
        turn_obj = response.get("turn")
        if not isinstance(turn_obj, dict):
            return False
        status = (turn_obj.get("status") or "").lower()
        # Completed-by-server response: extract final items.
        if status in ("completed", "done", "finished", "success"):
            items = turn_obj.get("items") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                kind = (item.get("type") or "").lower()
                if kind in ("agent_message", "agentmessage", "assistant_message"):
                    text = item.get("text") or item.get("content") or ""
                    if isinstance(text, str) and text:
                        turn.reply_chunks.append(text)
                elif kind in ("tool_call", "toolcall", "tool_use", "tooluse"):
                    turn.tool_calls += 1
            usage = turn_obj.get("usage") or {}
            try:
                turn.input_tokens = int(usage.get("input_tokens") or 0)
                turn.output_tokens = int(usage.get("output_tokens") or 0)
            except (TypeError, ValueError):
                pass
            return True
        # Failed-by-server response: surface the error.
        if status in ("failed", "error"):
            err = turn_obj.get("error") or "(no detail)"
            raise RuntimeError(f"codex turn failed: {err}")
        # ``running`` / unknown — let notifications take over.
        return False


def _readable_error(err: Any) -> str:
    """Codex's notification ``error`` field can be: a plain string, a
    dict with ``message`` (sometimes JSON-encoded itself), or a nested
    error envelope. Walk the structure to surface the most-readable
    text — operators want "model not supported" not a JSON blob.
    """
    if isinstance(err, str):
        # Maybe a JSON-encoded error string.
        try:
            inner = json.loads(err)
        except (ValueError, TypeError):
            return err
        return _readable_error(inner)
    if isinstance(err, dict):
        # Common patterns.
        for key in ("message", "msg", "detail"):
            v = err.get(key)
            if isinstance(v, str) and v:
                return _readable_error(v)
        # Nested error envelope.
        for key in ("error",):
            v = err.get(key)
            if v is not None:
                return _readable_error(v)
        return json.dumps(err)[:500]
    return repr(err)[:500]


def _extract_thread_id(result: Any) -> str:
    """Pull a thread id out of whatever shape ``thread/start`` returned.

    Codex hasn't published a stable response schema for ``thread/*``,
    so we try the obvious flat / camelCase / snake_case / nested
    variations and fall back to empty. Caller surfaces a clear error
    when this returns empty.
    """
    if not isinstance(result, dict):
        return ""
    for key in ("threadId", "thread_id", "conversationId", "conversation_id", "id"):
        v = result.get(key)
        if isinstance(v, str) and v:
            return v
    # Nested {thread: {id: "..."}} / {result: {threadId: "..."}}
    for parent_key in ("thread", "result"):
        nested = result.get(parent_key)
        if isinstance(nested, dict):
            inner = _extract_thread_id(nested)
            if inner:
                return inner
    return ""


__all__ = ["CodexSession"]
