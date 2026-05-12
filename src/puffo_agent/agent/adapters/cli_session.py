"""Long-lived ``claude`` CLI session with audit logging.

Spawned once per agent, fed one user message per turn, kept alive
across turns via stream-json I/O. The session id from the init event
is persisted to ``cli_session.json`` so a daemon restart re-spawns
with ``--resume <id>``. Agnostic to whether the subprocess runs on
the host or via ``docker exec`` — the caller supplies a
``build_command`` callback returning the full argv.

Wire protocol (one JSON object per line):

  stdin (write)
    {"type":"user","message":{"role":"user","content":"..."},
     "parent_tool_use_id":null,"session_id":"..."}

  stdout (read)
    {"type":"system","subtype":"init","session_id":"...",...}
    {"type":"assistant","message":{"content":[{"type":"text",...}, ...]}}
    {"type":"user","message":{"content":[{"type":"tool_result",...}]}}
    {"type":"result","subtype":"success","session_id":"...","usage":{...}}

One turn = write one user event, read until ``result`` arrives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .base import TurnResult

logger = logging.getLogger(__name__)


# Cap any single audit field so one huge user message or tool input
# doesn't bloat the log / the docker-logs stream.
AUDIT_FIELD_MAX = 2000


# Case-insensitive substrings that mark a claude reply as an auth /
# token failure rather than a real response. Kept STRONG-ONLY — weak
# markers like "401" or "unauthorized" would false-positive on users
# discussing HTTP / auth concepts.
_AUTH_ERROR_MARKERS = (
    "please run /login",
    "please run `claude /login`",
    "run `claude login`",
    "invalid api key",
    "invalid_grant",
    "authentication failed",
    "credentials expired",
    "failed to authenticate",
    "api error: 401",
    "invalid authentication credentials",
    '"type":"authentication_error"',
)

# Backoffs between auth-error retries (5 attempts total, worst case
# ~45s). First interval is short: the common cause is a multi-agent
# rotating-refresh-token race that resolves within a second of the
# winner writing the new token to the shared credentials file.
AUTH_RETRY_BACKOFFS_SECONDS = (3, 6, 12, 24)


def _looks_like_auth_error(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in _AUTH_ERROR_MARKERS)


class AuditLog:
    """Per-agent ndjson audit log.

    Lives inside the agent's workspace (which is bind-mounted into
    the cli-docker container) so the same file feeds the container's
    ``tail -F`` PID 1 and ``docker logs``.
    """

    def __init__(self, path: Path, agent_id: str):
        self.path = path
        self.agent_id = agent_id
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Touch so the container's tail starts cleanly even if no
            # turn has happened yet.
            self.path.touch(exist_ok=True)
        except OSError as exc:
            logger.warning(
                "agent %s: cannot prepare audit log at %s: %s",
                agent_id, path, exc,
            )

    def write(self, event: str, **fields) -> None:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agent": self.agent_id,
            "event": event,
            **{k: _truncate(v) for k, v in fields.items()},
        }
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as exc:
            # Audit failure must not kill the turn.
            logger.warning(
                "agent %s: audit log write failed: %s",
                self.agent_id, exc,
            )


def _truncate(v):
    if isinstance(v, str) and len(v) > AUDIT_FIELD_MAX:
        return v[:AUDIT_FIELD_MAX] + "... (truncated)"
    if isinstance(v, dict):
        return {k: _truncate(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_truncate(x) for x in v]
    return v


# Wait this long for the init event before giving up; some claude
# versions delay it until the first user message, so we fall back to
# capturing the session id from the first result event instead.
INIT_TIMEOUT_SECONDS = 10.0


# StreamReader buffer size for the claude subprocess's stdout. The
# asyncio default is 64 KiB; single stream-json events from Opus-class
# models (verbose metadata + long tool results) routinely exceed that,
# which would raise ``LimitOverrunError`` from ``readline()`` and wedge
# the turn. 16 MiB bounds memory per agent while comfortably covering
# every event size seen in practice.
STREAM_READER_LIMIT_BYTES = 16 * 1024 * 1024


class _ResumeFailed(Exception):
    """The subprocess exited before emitting init — usually because
    ``--resume <id>`` referenced a session claude no longer has a
    transcript for."""


class ClaudeSession:
    def __init__(
        self,
        agent_id: str,
        session_file: Path,
        build_command: Callable[[list[str], dict[str, str]], list[str]],
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        audit: Optional["AuditLog"] = None,
        extra_args: Optional[list[str]] = None,
    ):
        """
        ``build_command(extra_args, env_overrides)`` returns the full
        argv to spawn. cli-local prepends ``["claude", "--dangerously-
        skip-permissions", ...]`` and ignores ``env_overrides`` (env
        is merged on the host); cli-docker prepends ``["docker",
        "exec", "-i", ...]`` plus ``-e KEY=VALUE`` for each
        ``env_overrides`` entry.

        ``audit`` is optional; when set, each turn appends structured
        events for operators to tail.
        """
        self.agent_id = agent_id
        self.session_file = session_file
        self.build_command = build_command
        self.cwd = cwd
        self.env = env
        self.audit = audit
        # Extra claude CLI flags re-applied on every spawn (typically
        # --mcp-config / --permission-prompt-tool).
        self.extra_args = list(extra_args or [])

        self._proc: asyncio.subprocess.Process | None = None
        self._system_prompt_seen: str | None = None
        self._session_id: str = self._load_session_id()
        self._lock = asyncio.Lock()
        self._stderr_drain_task: asyncio.Task | None = None
        # True iff the most recent ``_spawn`` used ``--resume``. False
        # after a fresh spawn (no session id) or when ``_ResumeFailed``
        # forced a fresh fallback. ``run_retry_turn`` reads this to
        # decide whether the cheap "session errored, please resume"
        # kick is enough, or whether the caller's full-payload
        # fallback needs to be sent because claude-code has no
        # transcript to resume.
        self._last_spawn_resumed: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_turn(self, user_message: str, system_prompt: str) -> TurnResult:
        async with self._lock:
            await self._ensure_running(system_prompt)
            # Retry on auth-error replies — most commonly a transient
            # rotating-refresh-token race or a 5xx blip; short backoffs
            # usually rescue the turn.
            attempts = len(AUTH_RETRY_BACKOFFS_SECONDS) + 1
            last_result: TurnResult | None = None
            for attempt in range(attempts):
                if attempt > 0:
                    delay = AUTH_RETRY_BACKOFFS_SECONDS[attempt - 1]
                    logger.warning(
                        "agent %s: auth-error reply on attempt %d/%d; "
                        "retrying in %ds",
                        self.agent_id, attempt, attempts, delay,
                    )
                    await asyncio.sleep(delay)
                    # Re-ensure running — subprocess may have died
                    # during the wait. Respawn re-reads the shared
                    # credentials file.
                    await self._ensure_running(system_prompt)
                result = await self._one_turn(user_message)
                if not _looks_like_auth_error(result.reply):
                    return result
                last_result = result
                if self.audit is not None:
                    self.audit.write(
                        "auth_error.detected",
                        attempt=attempt + 1,
                        of=attempts,
                        reply=result.reply,
                    )
            # All attempts exhausted. Return an empty reply so the
            # shell suppresses the post; operators still see the
            # state via the ERROR log + audit + ``auth_failed`` in
            # metadata.
            logger.error(
                "agent %s: auth error persisted across %d attempts; "
                "suppressing reply. last reply: %s",
                self.agent_id, attempts,
                (last_result.reply if last_result else "")[:500],
            )
            if self.audit is not None:
                self.audit.write(
                    "auth_error.exhausted_retries",
                    attempts=attempts,
                    reply=last_result.reply if last_result else "",
                )
            md: dict = {"auth_failed": True, "attempts": attempts}
            if last_result is not None:
                md = {**last_result.metadata, **md}
                return TurnResult(
                    reply="",
                    input_tokens=last_result.input_tokens,
                    output_tokens=last_result.output_tokens,
                    tool_calls=last_result.tool_calls,
                    metadata=md,
                )
            return TurnResult(reply="", metadata=md)

    async def run_retry_turn(
        self,
        kick_text: str,
        fallback_user_message: str,
        system_prompt: str,
    ) -> TurnResult:
        """Retry the most recently failed turn.

        If claude-code's session was resumed successfully (the
        previous user input is still in its transcript), send just
        ``kick_text`` — a small control message like "session
        errored on rate limiting, please resume processing". The
        agent reads the transcript and retries its previous response
        without seeing a duplicate of the original input.

        If ``--resume`` failed (the session id is no longer valid),
        ``_ensure_running`` has already cleared the session id and
        spawned a fresh claude-code with no transcript. The kick
        would be meaningless on its own, so we send
        ``fallback_user_message`` instead — the full original payload
        that the caller would have sent for a normal turn.

        Auth-error retry inside ``run_turn`` would normally re-send
        the user message on each attempt; for the API-error path the
        consumer drives retries from outside (with its own backoff),
        so this method is a single-shot.
        """
        async with self._lock:
            await self._ensure_running(system_prompt)
            if self._last_spawn_resumed:
                user_message = kick_text
            else:
                logger.warning(
                    "agent %s: --resume not in effect for retry; "
                    "falling back to the original payload",
                    self.agent_id,
                )
                user_message = fallback_user_message
            return await self._one_turn(user_message)

    async def warm(self, system_prompt: str) -> None:
        """Spawn the claude subprocess without running a turn so the
        first real message doesn't pay process + init latency.
        Idempotent.
        """
        async with self._lock:
            await self._ensure_running(system_prompt)

    def has_persisted_session(self) -> bool:
        """True when a previous run left a session id on disk — i.e.
        warming would resume an existing conversation rather than
        burn startup cost on an idle agent."""
        return bool(self._session_id)

    async def aclose(self) -> None:
        async with self._lock:
            await self._kill_proc()

    # ── Session id persistence ────────────────────────────────────────────────

    def _load_session_id(self) -> str:
        if not self.session_file.exists():
            return ""
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return (data.get("session_id") or "").strip()

    def _save_session_id(self, sid: str) -> None:
        self._session_id = sid
        data = {"session_id": sid, "updated_at": int(time.time())}
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.session_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.session_file)

    def _clear_session_id(self) -> None:
        self._session_id = ""
        try:
            self.session_file.unlink()
        except OSError:
            pass

    # ── Subprocess lifecycle ──────────────────────────────────────────────────

    async def _ensure_running(self, system_prompt: str) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if self._proc is not None:
            logger.warning(
                "agent %s: claude subprocess exited (rc=%s); re-spawning",
                self.agent_id, self._proc.returncode,
            )
            self._proc = None

        had_session_id = bool(self._session_id)
        try:
            await self._spawn(system_prompt)
            # _spawn either uses --resume (when _session_id was set
            # going in) or starts a fresh session and learns the new
            # session id on system/init. ``_last_spawn_resumed``
            # captures the former path so ``run_retry_turn`` can
            # decide whether the kick alone is sufficient.
            self._last_spawn_resumed = had_session_id
            return
        except _ResumeFailed as exc:
            logger.warning(
                "agent %s: --resume failed (%s); starting a fresh session",
                self.agent_id, exc,
            )
            self._clear_session_id()
            await self._spawn(system_prompt)
            self._last_spawn_resumed = False

    async def _spawn(self, system_prompt: str) -> None:
        # --verbose is required with --output-format stream-json +
        # --print / streaming input; the CLI rejects the combo
        # otherwise.
        args = [
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
        ]
        args.extend(self.extra_args)
        # System prompt is NOT passed on argv. The worker writes it
        # (plus primer + memory snapshot) to ``<cwd>/.claude/
        # CLAUDE.md``; Claude Code auto-discovers that at startup. We
        # only capture the value here for diagnostics.
        self._system_prompt_seen = system_prompt or None
        if self._session_id:
            args.extend(["--resume", self._session_id])

        # ``env_overrides`` is reserved for future per-spawn env
        # injection. Today's spawn doesn't set anything — adding
        # NODE_OPTIONS=--max-old-space-size made things worse on
        # constrained Docker Desktop VMs (V8 delayed GC, RSS
        # climbed). The real fix for resume contention is serialised
        # warm in worker.py + per-container memory caps in
        # docker_cli.py.
        env_overrides: dict[str, str] = {}
        cmd = self.build_command(args, env_overrides)
        logger.info(
            "agent %s: spawning claude session (resume=%s)",
            self.agent_id, bool(self._session_id),
        )
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
            limit=STREAM_READER_LIMIT_BYTES,
        )
        if self.audit is not None:
            self.audit.write(
                "session.start",
                resume=bool(self._session_id),
                session_id=self._session_id or "",
            )
        # Capture session_id from init; on timeout we pick it up from
        # the first result event instead. Stderr drain only starts
        # after a successful init so the failure path can read stderr
        # for diagnostics.
        try:
            sid = await asyncio.wait_for(
                self._read_init(self._proc), timeout=INIT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.debug(
                "agent %s: no init event within %.1fs; will capture session_id from first result",
                self.agent_id, INIT_TIMEOUT_SECONDS,
            )
            self._stderr_drain_task = asyncio.ensure_future(self._drain_stderr(self._proc))
            return
        if sid and sid != self._session_id:
            self._save_session_id(sid)
        self._stderr_drain_task = asyncio.ensure_future(self._drain_stderr(self._proc))

    async def _read_init(self, proc: asyncio.subprocess.Process) -> str:
        while True:
            line = await proc.stdout.readline()
            if not line:
                rc = await proc.wait()
                # Grab stderr synchronously — no drain task running yet.
                stderr_tail = ""
                if proc.stderr is not None:
                    try:
                        buf = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
                        stderr_tail = buf.decode("utf-8", errors="replace").strip()[-800:]
                    except asyncio.TimeoutError:
                        pass
                raise _ResumeFailed(
                    f"claude exited rc={rc} before init event"
                    + (f"; stderr: {stderr_tail}" if stderr_tail else "")
                )
            event = _parse_event(line)
            if event is None:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                return (event.get("session_id") or "").strip()

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                # Surface stderr at WARNING — most of it is a real
                # complaint worth seeing.
                logger.warning(
                    "agent %s claude stderr: %s",
                    self.agent_id, text,
                )
        except Exception:
            return

    async def _handle_stream_failure(self, phase: str, exc) -> None:
        """Cleanup for mid-turn stream-json failures (oversize line,
        broken pipe, EOF). Logs, audits, kills the subprocess so the
        next turn respawns. Callers should return an empty reply so
        the shell suppresses the post.
        """
        err_type = type(exc).__name__ if isinstance(exc, BaseException) else "str"
        err_str = str(exc)
        logger.error(
            "agent %s: claude stream failure in %s (%s: %s) — "
            "killing subprocess; next turn will respawn",
            self.agent_id, phase, err_type, err_str,
        )
        if self.audit is not None:
            self.audit.write(
                "session.stream_error",
                phase=phase,
                error_type=err_type,
                error=err_str,
                action="respawned_claude_subprocess",
            )
        await self._kill_proc()

    async def _kill_proc(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        # Cancel the stderr drain before the subprocess goes away —
        # without this the drain awaits readline() forever on Windows
        # and blocks ``asyncio.run`` from exiting cleanly on shutdown.
        drain = self._stderr_drain_task
        self._stderr_drain_task = None
        if drain is not None and not drain.done():
            drain.cancel()
            try:
                await drain
            except (asyncio.CancelledError, Exception):
                pass
        if proc.returncode is not None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass

    # ── One turn ──────────────────────────────────────────────────────────────

    async def _one_turn(self, user_message: str) -> TurnResult:
        assert self._proc is not None and self._proc.stdin is not None
        if self.audit is not None:
            self.audit.write("turn.input", content=user_message)
        turn_started_at = time.time()
        frame = {
            "type": "user",
            "message": {"role": "user", "content": user_message},
            "parent_tool_use_id": None,
            "session_id": self._session_id or "puffoagent-turn",
        }
        self._proc.stdin.write((json.dumps(frame) + "\n").encode("utf-8"))
        try:
            await self._proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            # Subprocess died before we could hand it the turn. Treat
            # the same as a mid-read failure: kill, audit, silent
            # empty reply. Next turn respawns.
            await self._handle_stream_failure("stdin_drain", exc)
            return TurnResult(reply="", metadata={"stream_error": "stdin_drain"})

        reply_parts: list[str] = []
        tool_calls = 0
        # Names of every tool invoked this turn (debug / audit).
        tool_names_used: list[str] = []
        # ``(channel, root_id)`` of every ``mcp__puffo__send_message``
        # call. The shell uses this to decide whether to suppress its
        # auto-reply (otherwise narration around the MCP call posts as
        # a duplicate). Empty ``root_id`` = top-level post.
        send_message_targets: list[dict] = []
        input_tokens = 0
        output_tokens = 0
        event_types_seen: list[str] = []

        while True:
            try:
                line = await self._proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                # asyncio wraps LimitOverrunError in ValueError when
                # raising out of readline(); catch both. Recover
                # rather than wedge if a single event exceeds the
                # 16 MiB StreamReader buffer set at spawn time.
                await self._handle_stream_failure("readline_limit", exc)
                return TurnResult(reply="", metadata={"stream_error": "readline_limit"})
            except (ConnectionResetError, BrokenPipeError) as exc:
                await self._handle_stream_failure("readline_pipe", exc)
                return TurnResult(reply="", metadata={"stream_error": "readline_pipe"})
            if not line:
                rc = await self._proc.wait()
                # Subprocess died mid-turn. Audit, respawn on next
                # turn, return empty reply so the channel doesn't see
                # a traceback-flavoured bot message.
                await self._handle_stream_failure("eof_mid_turn", f"rc={rc}")
                return TurnResult(reply="", metadata={"stream_error": "eof_mid_turn"})
            event = _parse_event(line)
            if event is None:
                continue
            event_types_seen.append(
                f"{event.get('type')}/{event.get('subtype', '-')}"
            )
            logger.debug("agent %s stream event: %s", self.agent_id, event)

            t = event.get("type")
            if t == "assistant":
                msg = event.get("message") or {}
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "text":
                        text = block.get("text", "") or ""
                        reply_parts.append(text)
                        if self.audit is not None and text:
                            self.audit.write("assistant.text", text=text)
                    elif bt == "tool_use":
                        tool_calls += 1
                        name = block.get("name", "")
                        tool_input = block.get("input") or {}
                        tool_names_used.append(name)
                        if name == "mcp__puffo__send_message":
                            send_message_targets.append({
                                "channel": str(tool_input.get("channel", "")),
                                "root_id": str(tool_input.get("root_id", "")),
                            })
                        if self.audit is not None:
                            self.audit.write(
                                "tool",
                                name=name,
                                input=tool_input,
                                id=block.get("id", ""),
                            )
            elif t == "system":
                sid = (event.get("session_id") or "").strip()
                if sid and sid != self._session_id:
                    self._save_session_id(sid)
            elif t == "result":
                sid = (event.get("session_id") or "").strip()
                if sid and sid != self._session_id:
                    self._save_session_id(sid)
                usage = event.get("usage") or {}
                input_tokens = int(usage.get("input_tokens", 0) or 0)
                output_tokens = int(usage.get("output_tokens", 0) or 0)
                # Fallback for CLI versions where the assembled text
                # reply only appears on ``result.result``.
                result_text = event.get("result") or ""
                if not reply_parts and result_text:
                    reply_parts.append(result_text)
                break

        reply = "".join(reply_parts).strip()
        if not reply:
            logger.warning(
                "agent %s: claude turn produced no text reply. events seen: %s",
                self.agent_id, event_types_seen,
            )
        # Auth-error detection + rewriting lives in run_turn; this
        # method only reports what happened.
        if self.audit is not None:
            self.audit.write(
                "turn.end",
                reply_len=len(reply),
                tool_calls=tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=int((time.time() - turn_started_at) * 1000),
                event_types=event_types_seen,
            )
        return TurnResult(
            reply=reply,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            metadata={
                "session_id": self._session_id,
                "tool_names": tool_names_used,
                "send_message_targets": send_message_targets,
                # Per-frame assistant text — used by the shell to
                # build a bulleted fallback when the agent neither
                # called send_message nor said [SILENT].
                "assistant_text_parts": list(reply_parts),
            },
        )


def _parse_event(line: bytes) -> dict | None:
    try:
        return json.loads(line.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
