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
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .base import STATUS_PREVIEW_CHARS, TurnResult, is_silent
from .._logging import safe_diagnostic_summary, safe_value_summary
from ..context_controller import (
    AdmissionCallback,
    CompactionResult,
    ContextCapabilities,
    ContextSnapshot,
    ProviderAdmissionEvent,
    RolloverResult,
    ToolResultAdmission,
    normalize_context_snapshot,
)

logger = logging.getLogger(__name__)


AUDIT_MAX_BYTES = 5 * 1024 * 1024
AUDIT_BACKUP_COUNT = 3
_AUDIT_PAYLOAD_FIELDS = frozenset(
    {
        "content",
        "text",
        "input",
        "reply",
        "raw_reply",
        "raw_event",
        "stdout",
        "stderr",
        "result",
        "tool_result",
    }
)
_AUDIT_DIAGNOSTIC_FIELDS = frozenset({"error", "diagnostic"})
_AUDIT_METADATA_FIELDS = frozenset(
    {
        "action",
        "attempt",
        "attempts",
        "cap",
        "duration_ms",
        "event_type",
        "event_types",
        "id",
        "input_tokens",
        "name",
        "of",
        "output_tokens",
        "phase",
        "reply_len",
        "resume",
        "session_id",
        "tool_calls",
        "user_message_bytes",
        "error_type",
    }
)


def _is_audit_payload_field(key: str) -> bool:
    return (
        key in _AUDIT_PAYLOAD_FIELDS
        or key.startswith(("stdout", "stderr", "raw_"))
        or key.endswith(("_input", "_result"))
    )


# 180KB leaves ~20KB headroom under Anthropic's ~200KB request cap
# for system prompt + tools + headers.
MAX_USER_MESSAGE_BYTES = 180 * 1000

REQUEST_TOO_LARGE_FRIENDLY = (
    "Your message has too much content. Please reduce attachments "
    "or split your message and try again."
)

# Fullwidth `：` `，` because some localised Claude Code builds emit them.
_REQUEST_TOO_LARGE_PATTERN: re.Pattern[str] = re.compile(
    r"(?:API Error:\s*)?Prompt is too long\b"
    r"|input length and `max_tokens` exceed context limit"
    r"|size error\s*[:：]\s*request too large",
    re.IGNORECASE,
)


# Backoffs between auth-error retries (5 attempts total, worst case
# ~45s). First interval is short: the common cause is a multi-agent
# rotating-refresh-token race that resolves within a second of the
# winner writing the new token to the shared credentials file.
AUTH_RETRY_BACKOFFS_SECONDS = (3, 6, 12, 24)


# Case-insensitive substrings that mark a claude reply as a POISONED
# SESSION: the conversation transcript accumulated content the API
# now rejects wholesale (most commonly an image whose longest edge
# tops 2000px). ``--resume`` reloads the same poisoned transcript, so
# every later turn fails identically and the agent dead-locks. The
# only fix is what the API itself advises — start a fresh session.
# Kept STRONG-ONLY: these are verbatim API error strings, vanishingly
# unlikely in normal chatter. The inbound-image downscale
# (puffo_core_client._downscale_oversized_image) is the prevention
# half; this is the recovery half for anything that slips through or
# is already stuck in an existing transcript.
_POISONED_SESSION_MARKERS = (
    "exceeds the dimension limit for many-image requests",
    "start a new session with fewer images",
)


@dataclass
class _ClaudeTurnState:
    started_at: float
    reply_parts: list[str] = field(default_factory=list)
    tool_calls: int = 0
    tool_names_used: list[str] = field(default_factory=list)
    send_message_targets: list[dict[str, str]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    event_types_seen: list[str] = field(default_factory=list)


# Shared with ``core`` (which tags ``AgentAPIError.is_auth``) so the
# two detections can't drift.
from .._auth_markers import looks_like_auth_error as _looks_like_auth_error  # noqa: E402


def _looks_like_poisoned_session(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in _POISONED_SESSION_MARKERS)


def _looks_like_request_too_large(text: str) -> bool:
    if not text:
        return False
    return _REQUEST_TOO_LARGE_PATTERN.search(text) is not None


class AuditLog:
    """Per-agent ndjson audit log.

    Lives inside the agent's workspace (which is bind-mounted into
    the cli-docker container) so the same file feeds the container's
    ``tail -F`` PID 1 and ``docker logs``.
    """

    def __init__(
        self,
        path: Path,
        agent_id: str,
        *,
        max_bytes: int = AUDIT_MAX_BYTES,
        backup_count: int = AUDIT_BACKUP_COUNT,
    ):
        self.path = path
        self.agent_id = agent_id
        self.max_bytes = max(1, max_bytes)
        self.backup_count = max(0, backup_count)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Touch so the container's tail starts cleanly even if no
            # turn has happened yet.
            self.path.touch(exist_ok=True)
        except OSError as exc:
            self._warn("cannot prepare audit log", exc)

    def write(self, event: str, **fields) -> None:
        try:
            safe_fields = {}
            for key, value in fields.items():
                if _is_audit_payload_field(key):
                    safe_fields[f"{key}_summary"] = safe_value_summary(value)
                elif key in _AUDIT_DIAGNOSTIC_FIELDS:
                    safe_fields[f"{key}_summary"] = safe_diagnostic_summary(value)
                elif key in _AUDIT_METADATA_FIELDS:
                    safe_fields[key] = _bounded_metadata(value)
                else:
                    safe_fields[f"{key}_summary"] = safe_value_summary(value)
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "agent": self.agent_id,
                "event": event,
                **safe_fields,
            }
            encoded = json.dumps(rec, ensure_ascii=False)
            self._rotate_if_needed()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(encoded + "\n")
        except Exception as exc:
            self._warn("audit log write failed", exc)

    def _warn(self, message: str, exc: BaseException) -> None:
        try:
            logger.warning(
                "agent %s: %s: %s",
                self.agent_id,
                message,
                type(exc).__name__,
            )
        except Exception:
            pass

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
            if self.backup_count == 0:
                self.path.write_text("", encoding="utf-8")
                return
            oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
            try:
                oldest.unlink()
            except FileNotFoundError:
                pass
            for index in range(self.backup_count - 1, 0, -1):
                source = self.path.with_name(f"{self.path.name}.{index}")
                if source.exists():
                    os.replace(
                        source,
                        self.path.with_name(f"{self.path.name}.{index + 1}"),
                    )
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
            self.path.touch()
        except Exception as exc:
            self._warn("audit log rotation failed", exc)


def _bounded_metadata(v):
    """Bound non-payload metadata while retaining IDs and counters."""
    if isinstance(v, str) and len(v) > 256:
        return safe_value_summary(v)
    if isinstance(v, dict):
        return {str(k): _bounded_metadata(x) for k, x in list(v.items())[:64]}
    if isinstance(v, list):
        return [_bounded_metadata(x) for x in v[:64]]
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
CONTEXT_USAGE_TIMEOUT_SECONDS = 3.0

# Read loop has no turn-correlation — collects until ``result`` — so any
# pre-turn ``assistant`` chatter (Claude Code's internal cron ticks) leaks
# into the next turn's reply. Drained before the frame write. Per-readline
# timeout bounds the empty-buffer check; wall-time cap keeps a chatty
# producer from stalling the turn.
STALE_STDOUT_DRAIN_TIMEOUT = 0.1
STALE_STDOUT_DRAIN_BUDGET = 1.0


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
        audit: Optional[AuditLog] = None,
        extra_args: Optional[list[str]] = None,
        model: str = "",
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

        ``model`` scopes the persisted session to the model that
        produced it: a transcript born under one model must not be
        ``--resume``d under another. Cross-family replay injects the
        old model's thinking blocks into the new provider's API — a
        kimi-born session resumed under an Anthropic model makes
        claude-code send ``clear_thinking_20251015`` without thinking
        enabled, and every turn 400s until the session rotates
        (PUF-159). Empty = legacy behavior (no scoping).
        """
        self.agent_id = agent_id
        self.session_file = session_file
        self.build_command = build_command
        self.cwd = cwd
        self.env = env
        self.audit = audit
        self.model = (model or "").strip()
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
        self._context_used_tokens: int = 0
        self._context_measured_at: datetime | None = None
        self._context_usage: dict[str, object] | None = None
        self._context_usage_supported: bool | None = None
        self._control_request_counter = 0
        self._admission_callback: AdmissionCallback | None = None
        self._admission_planning_cycle_key: str = ""
        self._continuation_admissions: list[ToolResultAdmission] = []
        self._active_puffo_tool_calls: dict[str, tuple[str, dict[str, object]]] = {}
        self._active_provider_turn_id: str | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def _rewrite_if_request_too_large(self, result: TurnResult) -> TurnResult:
        if not _looks_like_request_too_large(result.reply):
            return result
        logger.warning(
            "agent %s: claude reply matched Prompt-is-too-long pattern; "
            "rewriting to friendly error (%s)",
            self.agent_id,
            safe_diagnostic_summary(result.reply),
        )
        if self.audit is not None:
            self.audit.write(
                "turn.request_too_large_reactive",
                raw_reply=result.reply,
            )
        new_md = dict(result.metadata or {})
        new_md["request_too_large"] = "reactive"
        new_md["original_reply"] = result.reply
        return TurnResult(
            reply=REQUEST_TOO_LARGE_FRIENDLY,
            metadata=new_md,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            tool_calls=result.tool_calls,
        )

    async def _one_turn_with_poison_recovery(
        self,
        user_message: str,
        system_prompt: str,
    ) -> TurnResult:
        """``_one_turn`` plus a single fresh-session retry when the
        turn comes back poisoned.

        ``_one_turn`` already cleared the session id + killed the
        subprocess on detection, so ``_ensure_running`` here spawns a
        FRESH session with no transcript — the poison was content from
        an EARLIER turn that the fresh session simply doesn't have.
        The current message's own image attachments are downscaled at
        save time, so re-sending it on the clean transcript succeeds;
        without this retry the triggering message would be silently
        dropped (it never gets another turn if it's the only inbound
        message). Retried at most once: if the message itself somehow
        still poisons the fresh session, return the (empty) poisoned
        result rather than loop."""
        result = await self._one_turn(user_message)
        if not result.metadata.get("poisoned_session"):
            return result
        logger.warning(
            "agent %s: re-running the turn on a fresh session after "
            "poisoned-transcript recovery so the message isn't dropped",
            self.agent_id,
        )
        await self._ensure_running(system_prompt)
        return await self._one_turn(user_message)

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
                        "agent %s: auth-error reply on attempt %d/%d; retrying in %ds",
                        self.agent_id,
                        attempt,
                        attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    # Re-ensure running — subprocess may have died
                    # during the wait. Respawn re-reads the shared
                    # credentials file.
                    await self._ensure_running(system_prompt)
                result = await self._one_turn_with_poison_recovery(
                    user_message,
                    system_prompt,
                )
                if not _looks_like_auth_error(result.reply):
                    return self._rewrite_if_request_too_large(result)
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
                "suppressing reply. last_reply=%s",
                self.agent_id,
                attempts,
                safe_diagnostic_summary(last_result.reply if last_result else ""),
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
            return await self._one_turn_with_poison_recovery(
                user_message,
                system_prompt,
            )

    async def warm(self, system_prompt: str) -> None:
        """Spawn the claude subprocess without running a turn so the
        first real message doesn't pay process + init latency.
        Idempotent.
        """
        async with self._lock:
            await self._ensure_running(system_prompt)
            await self._refresh_context_usage()

    def context_limits(self) -> tuple[int | None, int | None]:
        if self._context_usage is None:
            return None, None
        return (
            _positive_int(self._context_usage.get("rawMaxTokens")),
            _positive_int(self._context_usage.get("autoCompactThreshold")),
        )

    def has_persisted_session(self) -> bool:
        """True when a previous run left a session id on disk — i.e.
        warming would resume an existing conversation rather than
        burn startup cost on an idle agent."""
        return bool(self._session_id)

    async def aclose(self) -> None:
        async with self._lock:
            await self._kill_proc()

    async def get_context_snapshot(self) -> ContextSnapshot:
        max_tokens, _ = self.context_limits()
        return normalize_context_snapshot(
            used_tokens=self._context_used_tokens,
            provider_context_window=max_tokens,
            measured_at=self._context_measured_at,
            estimated_source="claude_usage_fallback_200000",
        )

    def get_context_capabilities(self) -> ContextCapabilities:
        return ContextCapabilities(
            native_measurement=bool(self._context_measured_at),
            rollover=True,
            diagnostic="Claude reports usage; native compaction unsupported",
        )

    async def compact_context(self) -> CompactionResult:
        return CompactionResult(
            completed=False,
            provider_session_id=self.get_provider_session_id(),
            diagnostic="native Claude compaction unsupported; no text frame sent",
        )

    async def rollover_context(self) -> RolloverResult:
        previous = self.get_provider_session_id()
        async with self._lock:
            self._clear_session_id()
            await self._kill_proc()
            self._context_used_tokens = 0
            self._context_measured_at = None
            self._context_usage = None
            self._context_usage_supported = None
        return RolloverResult(
            completed=True,
            previous_provider_session_id=previous,
            diagnostic="Claude session and cached occupancy cleared",
        )

    def get_provider_session_id(self) -> str | None:
        return self._session_id or None

    def register_admission_callback(
        self,
        callback: AdmissionCallback | None,
        planning_cycle_key: str = "",
    ) -> None:
        self._admission_callback = callback
        self._admission_planning_cycle_key = planning_cycle_key
        if callback is None:
            self._continuation_admissions.clear()

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
        if callback is None:
            self._continuation_admissions.clear()
            return
        provider_turn_id = self._active_provider_turn_id
        if provider_turn_id is None:
            if tool_names or tool_arguments is not None or correlation_receipt:
                raise RuntimeError(
                    "no active Claude provider turn for tool-result admission"
                )
            self.register_admission_callback(callback, planning_cycle_key)
            return
        self._continuation_admissions.append(
            ToolResultAdmission.build(
                callback,
                planning_cycle_key,
                provider_turn_id,
                channel_id=channel_id,
                tool_names=tool_names
                or (
                    "send_message",
                    "send_message_with_attachments",
                ),
                tool_arguments=tool_arguments,
                correlation_receipt=correlation_receipt,
            )
        )

    async def _fire_admission(self, provider_turn_id: str) -> None:
        callback = self._admission_callback
        key = self._admission_planning_cycle_key
        self._admission_callback = None
        self._admission_planning_cycle_key = ""
        if callback is not None:
            await callback(
                ProviderAdmissionEvent(
                    planning_cycle_key=key,
                    provider_session_id=self.get_provider_session_id(),
                    provider_turn_id=provider_turn_id,
                    admitted_at=datetime.now(timezone.utc),
                )
            )

    async def _fire_matching_continuations(
        self,
        event: dict,
        provider_turn_id: str,
    ) -> None:
        if event.get("type") != "user" or not self._continuation_admissions:
            return
        content = (event.get("message") or {}).get("content") or []
        selected: list[int] = []
        selected_tool_ids: dict[int, str] = {}
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = str(block.get("tool_use_id") or "")
            tool_call = self._active_puffo_tool_calls.get(tool_use_id)
            if block.get("is_error") is True:
                self._active_puffo_tool_calls.pop(tool_use_id, None)
                continue
            serialized = json.dumps(block, sort_keys=True, default=str)
            tool_name = tool_call[0] if tool_call is not None else ""
            tool_arguments = tool_call[1] if tool_call is not None else {}
            exact = next(
                (
                    index
                    for index, admission in enumerate(self._continuation_admissions)
                    if index not in selected
                    and admission.provider_turn_id == provider_turn_id
                    and admission.receipt_marker
                    and admission.receipt_marker in serialized
                    and tool_call is not None
                    # A receipt proves that the model saw this result, but it
                    # does not by itself prove which semantic call produced
                    # it.  Keep the same tool/argument contract as every
                    # other provider boundary before admitting held rows.
                    and admission.matches(tool_name, tool_arguments)
                ),
                None,
            )
            if exact is not None:
                selected.append(exact)
                selected_tool_ids[exact] = tool_use_id
                continue
            if tool_call is None:
                continue
            candidates = [
                index
                for index, admission in enumerate(self._continuation_admissions)
                if index not in selected
                and admission.provider_turn_id == provider_turn_id
                and not admission.correlation_receipt
                and admission.matches(tool_name, tool_arguments)
            ]
            if candidates:
                selected_index = max(
                    candidates,
                    key=lambda index: (
                        self._continuation_admissions[index].match_specificity,
                        -index,
                    ),
                )
                selected.append(selected_index)
                selected_tool_ids[selected_index] = tool_use_id
            self._active_puffo_tool_calls.pop(tool_use_id, None)

        admissions = [
            (self._continuation_admissions[index], selected_tool_ids[index])
            for index in selected
        ]
        for index in sorted(selected, reverse=True):
            self._continuation_admissions.pop(index)
        for admission, selected_tool_id in admissions:
            await admission.callback(
                ProviderAdmissionEvent(
                    planning_cycle_key=admission.planning_cycle_key,
                    provider_session_id=self.get_provider_session_id(),
                    provider_turn_id=provider_turn_id,
                    tool_call_id=selected_tool_id,
                    admitted_at=datetime.now(timezone.utc),
                )
            )

    # ── Session id persistence ────────────────────────────────────────────────

    def _load_session_id(self) -> str:
        if not self.session_file.exists():
            return ""
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        stored_model = (data.get("model") or "").strip()
        if self.model and stored_model and stored_model != self.model:
            # Model changed since this transcript was recorded — resuming
            # it would replay the old model's thinking blocks into the new
            # provider (PUF-159). Rotate: drop the file, start fresh.
            logger.info(
                "agent %s: model changed %s -> %s; rotating CLI session %s",
                self.agent_id,
                stored_model,
                self.model,
                (data.get("session_id") or "")[:8],
            )
            self._clear_session_file()
            return ""
        return (data.get("session_id") or "").strip()

    def _save_session_id(self, sid: str) -> None:
        self._session_id = sid
        data = {"session_id": sid, "updated_at": int(time.time())}
        if self.model:
            data["model"] = self.model
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.session_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.session_file)

    def _clear_session_id(self) -> None:
        self._session_id = ""
        self._clear_session_file()

    def _clear_session_file(self) -> None:
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
                self.agent_id,
                self._proc.returncode,
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
                self.agent_id,
                exc,
            )
            self._clear_session_id()
            self._context_used_tokens = 0
            self._context_measured_at = None
            await self._spawn(system_prompt)
            self._last_spawn_resumed = False

    async def _spawn(self, system_prompt: str) -> None:
        # --verbose is required with --output-format stream-json +
        # --print / streaming input; the CLI rejects the combo
        # otherwise.
        self._context_usage = None
        self._context_usage_supported = None
        args = [
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
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
        # the Docker Driver runtime.
        env_overrides: dict[str, str] = {}
        cmd = self.build_command(args, env_overrides)
        logger.info(
            "agent %s: spawning claude session (resume=%s)",
            self.agent_id,
            bool(self._session_id),
        )
        from ..._proc import no_window_kwargs

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
            limit=STREAM_READER_LIMIT_BYTES,
            **no_window_kwargs(),
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
                self._read_init(self._proc),
                timeout=INIT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.debug(
                "agent %s: no init event within %.1fs; will capture session_id from first result",
                self.agent_id,
                INIT_TIMEOUT_SECONDS,
            )
            self._stderr_drain_task = asyncio.ensure_future(
                self._drain_stderr(self._proc)
            )
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
                        stderr_tail = buf.decode("utf-8", errors="replace").strip()[
                            -800:
                        ]
                    except asyncio.TimeoutError:
                        pass
                raise _ResumeFailed(
                    f"claude exited rc={rc} before init event"
                    + (
                        f"; stderr_summary: {safe_diagnostic_summary(stderr_tail)}"
                        if stderr_tail
                        else ""
                    )
                )
            event = _parse_event(line)
            if not isinstance(event, dict):
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
                    self.agent_id,
                    safe_diagnostic_summary(text),
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
            self.agent_id,
            phase,
            err_type,
            safe_diagnostic_summary(err_str),
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

    async def _handle_poisoned_session(self, reply: str) -> None:
        """Recovery for a poisoned session transcript (see
        ``_POISONED_SESSION_MARKERS``). ``--resume`` would reload the
        same rejected content, so clear the session id AND kill the
        subprocess: the next turn spawns a FRESH session with no
        transcript. The dropped turn is re-readable from messages.db,
        so the agent can rebuild context on its next turn."""
        logger.error(
            "agent %s: claude session poisoned (API rejects the whole "
            "conversation) — clearing session id, respawning fresh. "
            "reply_summary: %s",
            self.agent_id,
            safe_diagnostic_summary(reply),
        )
        if self.audit is not None:
            self.audit.write(
                "session.poisoned",
                reply=reply,
                action="cleared_session_id_and_respawned_fresh",
            )
        self._clear_session_id()
        await self._kill_proc()
        self._context_used_tokens = 0
        self._context_measured_at = None

    async def _kill_proc(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        self._context_usage = None
        self._context_usage_supported = None
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

    async def _refresh_context_usage(self) -> dict[str, object] | None:
        if self._context_usage_supported is False or self._proc is None:
            return None
        proc = self._proc
        assert proc.stdin is not None and proc.stdout is not None
        self._control_request_counter += 1
        request_id = f"ctx_{self._control_request_counter}"
        frame = {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "get_context_usage"},
        }
        try:
            proc.stdin.write((json.dumps(frame) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            usage = await asyncio.wait_for(
                self._read_context_usage_response(proc, request_id),
                timeout=CONTEXT_USAGE_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, ConnectionError, RuntimeError) as exc:
            if isinstance(exc, RuntimeError):
                self._context_usage_supported = False
            logger.debug(
                "agent %s: Claude context usage unavailable: %s",
                self.agent_id,
                exc,
            )
            return None
        self._context_usage_supported = True
        self._context_usage = {
            "totalTokens": _positive_int(usage.get("totalTokens")),
            "rawMaxTokens": _positive_int(usage.get("rawMaxTokens")),
            "autoCompactThreshold": _positive_int(
                usage.get("autoCompactThreshold")
            ),
        }
        reported_context = _positive_int(self._context_usage.get("totalTokens"))
        if reported_context is not None:
            self._context_used_tokens = reported_context
            self._context_measured_at = datetime.now(timezone.utc)
        return self._context_usage

    async def _read_context_usage_response(
        self,
        proc: asyncio.subprocess.Process,
        request_id: str,
    ) -> dict[str, object]:
        assert proc.stdout is not None
        while True:
            # Queries run under the turn lock while stdout should be quiet.
            line = await proc.stdout.readline()
            if not line:
                raise ConnectionError("Claude stream closed during context query")
            event = _parse_event(line)
            if event is None:
                continue
            if event.get("type") == "system":
                self._update_session_from_event(event)
                continue
            if event.get("type") != "control_response":
                logger.warning(
                    "agent %s: ignoring unexpected %s event during context query",
                    self.agent_id,
                    event.get("type"),
                )
                continue
            response = event.get("response") or {}
            if not isinstance(response, dict):
                raise RuntimeError("context query returned an invalid envelope")
            if response.get("request_id") != request_id:
                continue
            if response.get("subtype") == "error":
                raise RuntimeError(response.get("error") or "context query failed")
            usage = response.get("response")
            if not isinstance(usage, dict):
                raise RuntimeError("context query returned an invalid response")
            return usage

    async def _drain_stale_stdout(self) -> int:
        """Consume stdout events buffered before this turn's frame is
        written, so pre-turn chatter isn't folded into the reply."""
        assert self._proc is not None and self._proc.stdout is not None
        drained = 0
        deadline = time.monotonic() + STALE_STDOUT_DRAIN_BUDGET
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(),
                    timeout=STALE_STDOUT_DRAIN_TIMEOUT,
                )
            except asyncio.TimeoutError:
                break
            except (
                asyncio.LimitOverrunError,
                ValueError,
                ConnectionResetError,
                BrokenPipeError,
            ):
                # Best-effort — main read loop handles recovery.
                break
            if not line:
                break
            drained += 1
            event = _parse_event(line)
            if event is None or self.audit is None:
                continue
            t = event.get("type")
            text = ""
            if t == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text += block.get("text", "") or ""
            self.audit.write("turn.pre_drain", event_type=t, text=text)
        if drained:
            logger.info(
                "agent %s: drained %d stale pre-turn stdout event(s)",
                self.agent_id,
                drained,
            )
        return drained

    async def _one_turn(self, user_message: str) -> TurnResult:
        provider_turn_id = f"claude-turn-{uuid.uuid4().hex}"
        self._active_provider_turn_id = provider_turn_id
        self._active_puffo_tool_calls.clear()
        try:
            return await self._one_turn_inner(user_message, provider_turn_id)
        finally:
            if self._active_provider_turn_id == provider_turn_id:
                self._active_provider_turn_id = None
            self._continuation_admissions = [
                admission
                for admission in self._continuation_admissions
                if admission.provider_turn_id != provider_turn_id
            ]
            self._active_puffo_tool_calls.clear()

    async def _one_turn_inner(
        self,
        user_message: str,
        provider_turn_id: str,
    ) -> TurnResult:
        assert self._proc is not None and self._proc.stdin is not None
        oversized = self._oversized_turn_result(user_message)
        if oversized is not None:
            return oversized
        if self.audit is not None:
            self.audit.write("turn.input", content=user_message)
        state = _ClaudeTurnState(started_at=time.time())
        stream_error = await self._write_turn_frame(user_message)
        if stream_error is not None:
            return stream_error
        from ...portal.control.reporter import get_reporter

        stream_error = await self._collect_turn_events(
            provider_turn_id,
            get_reporter(),
            state,
        )
        if stream_error is not None:
            return stream_error
        return await self._finish_turn(state)

    def _oversized_turn_result(self, user_message: str) -> TurnResult | None:
        size = len(user_message.encode("utf-8"))
        if size <= MAX_USER_MESSAGE_BYTES:
            return None
        logger.warning(
            "agent %s: user_message=%d bytes > cap %d; short-circuiting "
            "with friendly reply (no claude turn spawned)",
            self.agent_id,
            size,
            MAX_USER_MESSAGE_BYTES,
        )
        if self.audit is not None:
            self.audit.write(
                "turn.request_too_large_pre_send",
                user_message_bytes=size,
                cap=MAX_USER_MESSAGE_BYTES,
            )
        return TurnResult(
            reply=REQUEST_TOO_LARGE_FRIENDLY,
            metadata={
                "request_too_large": "pre_send",
                "user_message_bytes": size,
                "cap_bytes": MAX_USER_MESSAGE_BYTES,
            },
        )

    async def _write_turn_frame(self, user_message: str) -> TurnResult | None:
        assert self._proc is not None and self._proc.stdin is not None
        frame = {
            "type": "user",
            "message": {"role": "user", "content": user_message},
            "parent_tool_use_id": None,
            "session_id": self._session_id or "puffoagent-turn",
        }
        await self._drain_stale_stdout()
        self._proc.stdin.write((json.dumps(frame) + "\n").encode("utf-8"))
        try:
            await self._proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            await self._handle_stream_failure("stdin_drain", exc)
            return TurnResult(reply="", metadata={"stream_error": "stdin_drain"})
        return None

    async def _collect_turn_events(
        self,
        provider_turn_id: str,
        reporter: Any,
        state: _ClaudeTurnState,
    ) -> TurnResult | None:
        while True:
            event, stream_error = await self._read_turn_event()
            if stream_error is not None:
                return stream_error
            if event is None:
                continue
            await self._observe_turn_event(event, provider_turn_id, state)
            if await self._project_turn_event(event, reporter, state):
                return None

    async def _read_turn_event(self) -> tuple[dict[str, Any] | None, TurnResult | None]:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            line = await self._proc.stdout.readline()
        except (asyncio.LimitOverrunError, ValueError) as exc:
            await self._handle_stream_failure("readline_limit", exc)
            return None, TurnResult(
                reply="", metadata={"stream_error": "readline_limit"}
            )
        except (ConnectionResetError, BrokenPipeError) as exc:
            await self._handle_stream_failure("readline_pipe", exc)
            return None, TurnResult(
                reply="", metadata={"stream_error": "readline_pipe"}
            )
        if not line:
            rc = await self._proc.wait()
            await self._handle_stream_failure("eof_mid_turn", f"rc={rc}")
            return None, TurnResult(reply="", metadata={"stream_error": "eof_mid_turn"})
        event = _parse_event(line)
        return (event if isinstance(event, dict) else None), None

    async def _observe_turn_event(
        self,
        event: dict[str, Any],
        provider_turn_id: str,
        state: _ClaudeTurnState,
    ) -> None:
        session_id = str(event.get("session_id") or "").strip()
        if session_id and session_id != self._session_id:
            self._save_session_id(session_id)
        if self._admission_callback is not None:
            await self._fire_admission(provider_turn_id)
        await self._fire_matching_continuations(event, provider_turn_id)
        state.event_types_seen.append(
            f"{event.get('type')}/{event.get('subtype', '-')}"
        )
        logger.debug(
            "agent %s stream event: type=%s subtype=%s session_present=%s",
            self.agent_id,
            event.get("type"),
            event.get("subtype"),
            bool(event.get("session_id")),
        )

    async def _project_turn_event(
        self,
        event: dict[str, Any],
        reporter: Any,
        state: _ClaudeTurnState,
    ) -> bool:
        event_type = event.get("type")
        if event_type == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict):
                    await self._project_assistant_block(block, reporter, state)
        elif event_type == "system":
            self._update_session_from_event(event)
        elif event_type == "result":
            self._update_session_from_event(event)
            self._project_result_event(event, state)
            return True
        return False

    async def _project_assistant_block(
        self,
        block: dict[str, Any],
        reporter: Any,
        state: _ClaudeTurnState,
    ) -> None:
        if block.get("type") == "text":
            text = block.get("text", "") or ""
            state.reply_parts.append(text)
            if text and not is_silent(text):
                asyncio.ensure_future(
                    reporter.emit(self.agent_id, "assistant_text", {"text": text})
                )
            if self.audit is not None and text:
                self.audit.write("assistant.text", text=text)
        elif block.get("type") == "tool_use":
            self._project_tool_use(block, reporter, state)

    def _project_tool_use(
        self,
        block: dict[str, Any],
        reporter: Any,
        state: _ClaudeTurnState,
    ) -> None:
        state.tool_calls += 1
        name = block.get("name", "")
        tool_input = block.get("input") or {}
        state.tool_names_used.append(name)
        tool_event = {"tool": name}
        if "send_message" in name and isinstance(tool_input.get("text"), str):
            tool_event["content"] = tool_input["text"][:STATUS_PREVIEW_CHARS]
        asyncio.ensure_future(reporter.emit(self.agent_id, "tool_use", tool_event))
        if name == "mcp__puffo__send_message":
            state.send_message_targets.append(
                {
                    "channel": str(tool_input.get("channel", "")),
                    "root_id": str(tool_input.get("root_id", "")),
                }
            )
        self._track_puffo_tool(block, name, tool_input)
        if self.audit is not None:
            self.audit.write(
                "tool", name=name, input=tool_input, id=block.get("id", "")
            )

    def _track_puffo_tool(
        self, block: dict[str, Any], name: str, tool_input: dict
    ) -> None:
        if not name.startswith("mcp__puffo__"):
            return
        tool_use_id = str(block.get("id") or "")
        if tool_use_id:
            self._active_puffo_tool_calls[tool_use_id] = (
                name.removeprefix("mcp__puffo__"),
                {str(key): value for key, value in tool_input.items()},
            )

    def _update_session_from_event(self, event: dict[str, Any]) -> None:
        session_id = (event.get("session_id") or "").strip()
        if session_id and session_id != self._session_id:
            self._save_session_id(session_id)

    def _project_result_event(
        self, event: dict[str, Any], state: _ClaudeTurnState
    ) -> None:
        usage = event.get("usage") or {}
        state.input_tokens = int(usage.get("input_tokens", 0) or 0) + int(
            usage.get("cache_creation_input_tokens", 0) or 0
        )
        self._context_used_tokens = state.input_tokens + int(
            usage.get("cache_read_input_tokens", 0) or 0
        )
        self._context_measured_at = datetime.now(timezone.utc)
        state.output_tokens = int(usage.get("output_tokens", 0) or 0)
        result_text = event.get("result") or ""
        if not state.reply_parts and result_text:
            state.reply_parts.append(result_text)

    async def _finish_turn(self, state: _ClaudeTurnState) -> TurnResult:
        await self._refresh_context_usage()
        reply = "".join(state.reply_parts).strip()
        if _looks_like_poisoned_session(reply):
            await self._handle_poisoned_session(reply)
            return TurnResult(reply="", metadata={"poisoned_session": True})
        if not reply:
            logger.warning(
                "agent %s: claude turn produced no text reply. events seen: %s",
                self.agent_id,
                state.event_types_seen,
            )
        if self.audit is not None:
            self.audit.write(
                "turn.end",
                reply_len=len(reply),
                tool_calls=state.tool_calls,
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                duration_ms=int((time.time() - state.started_at) * 1000),
                event_types=state.event_types_seen,
            )
        return TurnResult(
            reply=reply,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            tool_calls=state.tool_calls,
            metadata={
                "session_id": self._session_id,
                "context_tokens": self._context_used_tokens,
                "tool_names": state.tool_names_used,
                "send_message_targets": state.send_message_targets,
                "assistant_text_parts": list(state.reply_parts),
            },
        )


def _parse_event(line: bytes) -> dict | None:
    try:
        return json.loads(line.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
