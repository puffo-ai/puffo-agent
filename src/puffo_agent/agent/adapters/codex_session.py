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

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .base import TurnResult

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
        model: str = "",
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
        # Codex's thread/start takes ``model`` as a required-ish
        # parameter; empty string means "let codex pick its default".
        self.model = model

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._next_id: int = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._active_turn: _PendingTurn | None = None
        self._lock = asyncio.Lock()
        self._conversation_id: str = self._load_conversation_id()
        # The latest system prompt we've been handed. Stored so
        # ``reload`` can detect a no-op vs a real change, and so a
        # respawn can re-issue ``newConversation`` with current
        # instructions when the conversation id is missing or rotted.
        self.current_instructions: str = ""

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
            turn = _PendingTurn(
                request_id=self._reserve_id(),
                started_at=time.time(),
            )
            self._active_turn = turn

        logger.debug(
            "agent %s: codex turn/start sending (msg_len=%d, thread=%s)",
            self.agent_id, len(user_message), self._conversation_id,
        )
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
                    return TurnResult(
                        reply="",
                        metadata={"codex_turn_timeout": True},
                    )
        finally:
            self._active_turn = None

        reply = "".join(turn.reply_chunks).strip()
        logger.debug(
            "agent %s: codex turn complete (reply_len=%d, tool_calls=%d, "
            "send_msg_calls=%d, in=%d out=%d)",
            self.agent_id, len(reply), turn.tool_calls,
            len(turn.send_message_targets),
            turn.input_tokens, turn.output_tokens,
        )
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
        """Update the in-memory ``current_instructions`` snapshot.

        If the App Server honours per-turn ``instructions`` (Phase 0
        #1), this is sufficient — the next ``sendUserTurn`` carries
        the new prompt with no thread restart. If the server ignores
        it, we degrade to "respawn on next warm" by clearing the
        process state. History is lost in that fallback; the v1
        trade-off is documented in this module's docstring.
        """
        if new_system_prompt == self.current_instructions:
            return
        self.current_instructions = new_system_prompt
        # Conservative for alpha: don't tear down the process here.
        # ``run_turn`` will pass ``instructions`` per turn; if that's
        # silently ignored, the user notices and we lift to "respawn
        # on reload" in 0.10.0a2.

    async def aclose(self) -> None:
        async with self._lock:
            await self._teardown_locked()

    def has_persisted_session(self) -> bool:
        return bool(self._conversation_id)

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

    def _save_conversation_id(self, cid: str) -> None:
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            self.session_file.write_text(
                json.dumps({"conversation_id": cid}),
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
        if self._proc is not None and self._proc.returncode is None:
            return
        await self._spawn()

    async def _spawn(self) -> None:
        logger.info(
            "agent %s: spawning codex app-server (argv=%s)",
            self.agent_id, " ".join(self.argv),
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
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

        # thread/start params per codex-rs/app-server protocol.
        # System prompt comes from ``$CODEX_HOME/AGENTS.md`` which we
        # wrote at adapter init, NOT a request param.
        #
        # Field names: codex Python SDK migrated from camelCase to
        # snake_case (per sdk/python/docs/faq.md). The Rust app-server
        # uses serde default (snake_case). Live test confirmed camelCase
        # was silently ignored — the agent reported codex defaults
        # (sandbox=read-only, approval=never) instead of what we passed.
        # Stick to snake_case.
        #
        # ApprovalPolicy "never" means **auto-approve, don't bother the
        # client** — confusing name, but verified by live behaviour:
        # MCP tool calls just go through, no elicitation round-trip.
        # That's exactly what we want for the puffo trust model
        # (puffo-agent vouches for the operator, all tools allowed).
        #
        # SandboxMode "danger-full-access": cli-local runs as the
        # operator's UID anyway, so a sandbox would only block the
        # agent from doing useful work. cli-docker (when supported)
        # will still use this — the container itself is the boundary.
        new_conv_params: dict[str, Any] = {
            "cwd": self.cwd or os.getcwd(),
            "approval_policy": (
                "never" if self.permission_mode == "bypassPermissions" else "untrusted"
            ),
            "sandbox_mode": "danger-full-access",
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
        """The App Server occasionally asks us things — most commonly
        approval for risky tool calls (``mcpServer/elicitation/request``)
        or command / patch approval (older shapes).

        ``bypassPermissions`` auto-approves all of these; other modes
        will route through the puffo permission proxy DM flow (deferred
        for v1).
        """
        m = method.lower()

        # 1. App Server MCP elicitation (codex 0.x) — the canonical
        # mechanism for "agent wants to use an MCP tool, ask user".
        # Response contract per codex-rs/app-server README:
        #
        #   accept:  {"action": "accept",  "content": {}}
        #   decline: {"action": "decline", "content": null}
        #   cancel:  {"action": "cancel",  "content": null}
        #
        # ``content`` must match the request's ``requestedSchema``;
        # for plain approvals (``codex_approval_kind == "mcp_tool_call"``)
        # there's no schema, so ``{}`` is the right value.
        if "elicitation" in m:
            if self.permission_mode == "bypassPermissions":
                await self._reply_to_server_request(
                    request_id,
                    {"action": "accept", "content": {}},
                )
            else:
                await self._reply_to_server_request(
                    request_id,
                    {"action": "decline", "content": None},
                )
            return

        # 2. Command execution / patch / write approval (codex
        # ``CommandExecutionRequestApprovalResponse`` family). codex's
        # error response told us the legal variants: ``accept`` /
        # ``acceptForSession`` / ``acceptWithExecpolicyAmendment`` /
        # ``applyNetworkPolicyAmendment`` / ``decline`` / ``cancel``.
        # Same ``{decision: ...}`` envelope as the historical
        # mcp-server shape; only the variant name differs from the old
        # ``"approved"``. Plain ``accept`` is enough for the puffo
        # trust model.
        exec_approval = (
            "approval" in m or "approve" in m
            or "applypatch" in m or "execcommand" in m
            or "commandexecution" in m
        )
        if exec_approval:
            decision = (
                "accept" if self.permission_mode == "bypassPermissions"
                else "decline"
            )
            await self._reply_to_server_request(
                request_id, {"decision": decision},
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
            elif kind in ("tool_use", "tooluse", "tool_call", "toolcall"):
                turn.tool_calls += 1
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
