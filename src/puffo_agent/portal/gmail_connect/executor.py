"""Subprocess seam to the gateway OAuth executor.

Implements the daemon side of Bob's EXECUTOR_INVOKE_SCHEMA v1
(sha256 b7f523cb…, msg_96b291d7): spawn the locally configured
entrypoint with no request-derived argv/env, write exactly one line of
non-secret JSON config to stdin, then read two JSON event lines from
stdout with bounded deadlines — ``ready`` (must advertise a loopback
redirect_uri) and the terminal ``result``.

The whole stdout stream is Layer A (daemon-internal). The daemon lifts
ONLY status + reason into ``ExecutorOutcome``; the ``summary`` dict
(scope / expires_in / has_refresh_token / token_db) is never read, so
Layer-A detail cannot leak toward the Layer-B projection by
construction. stderr is discarded — failure detail crosses only as the
coarse ``reason``, so token material can't reach daemon logs. A silent
or malformed executor is killed as a whole process group (executors
spawn browsers/listeners).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
READY_DEADLINE_S = 15.0
# Schema §4 closed reason set (v1.1, 60adad76…). Anything else on a
# result line is clamped to internal_error before it can ride the
# reason field into the Layer-B projection as free text.
SCHEMA_REASONS = frozenset({
    "bundle_verify_failed",
    "callback_timeout",
    "exchange_failed",
    "scope_mismatch",
    "keychain_error",
    "internal_error",
})
# The executor enforces its own consent timeout and reports
# callback_timeout; our read deadline sits above it so the structured
# reason wins over a daemon-side kill.
RESULT_DEADLINE_MARGIN_S = 30.0
_MAX_LINE_BYTES = 64 * 1024


class ExecutorRefused(RuntimeError):
    """Raised before any process is spawned."""


@dataclass
class ExecutorOutcome:
    status: str  # "connected" | "failed"
    reason: str = ""


def _validate_request(request: dict) -> None:
    # Schema v1 carries no callback field (the executor hard-binds
    # loopback), but if any caller ever adds one it must be loopback.
    host = str(request.get("callback_host", "127.0.0.1"))
    if host not in LOOPBACK_HOSTS:
        raise ExecutorRefused(f"non-loopback callback host {host!r}")


def _ready_is_loopback(ready: dict) -> bool:
    """The ready line must advertise a loopback redirect_uri — a
    non-loopback bind means the executor is not the contract's
    executor, and the flow must die before any browser opens."""
    uri = str(ready.get("redirect_uri", ""))
    host = urlsplit(uri).hostname or ""
    return host in LOOPBACK_HOSTS


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    try:
        await proc.wait()
    except Exception:  # noqa: BLE001 — already killing; nothing left to do
        pass


async def _read_event_line(proc: asyncio.subprocess.Process, deadline_s: float) -> dict:
    assert proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=deadline_s)
    if not line or len(line) > _MAX_LINE_BYTES:
        raise ValueError("executor closed stdout or overran line budget")
    parsed = json.loads(line)
    if not isinstance(parsed, dict) or parsed.get("event") not in ("ready", "result"):
        raise ValueError("executor line is not a ready/result event")
    return parsed


async def run_gmail_executor(
    entrypoint: str,
    request: dict,
    *,
    flow_timeout_s: float,
) -> ExecutorOutcome:
    """Run one connect flow to its terminal result.

    ``ExecutorRefused`` propagates (nothing was spawned); every failure
    after spawn degrades to ``ExecutorOutcome(status="failed")`` so
    callers have a single terminal-state code path.
    """
    _validate_request(request)
    try:
        proc = await asyncio.create_subprocess_exec(
            entrypoint,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        logger.warning("gmail-connect: executor spawn failed: %s", exc)
        return ExecutorOutcome(status="failed", reason="executor_unavailable")
    assert proc.stdin is not None
    try:
        proc.stdin.write(json.dumps(request).encode() + b"\n")
        await proc.stdin.drain()
        proc.stdin.close()
        first = await _read_event_line(proc, READY_DEADLINE_S)
        if first.get("event") == "result":
            # Pre-bind failure: 0 ready + 1 failed result. The real
            # reason (e.g. bundle_verify_failed) must survive — a
            # missing ready line is not a timeout (schema v1.1 §3).
            result = first
        else:
            if not _ready_is_loopback(first):
                await _kill_group(proc)
                return ExecutorOutcome(status="failed", reason="non_loopback_ready")
            result = await _read_event_line(
                proc, flow_timeout_s + RESULT_DEADLINE_MARGIN_S
            )
            if result.get("event") != "result":
                raise ValueError("second executor line is not a result event")
    except asyncio.TimeoutError:
        await _kill_group(proc)
        return ExecutorOutcome(status="failed", reason="timeout")
    except (ValueError, OSError):
        await _kill_group(proc)
        return ExecutorOutcome(status="failed", reason="protocol")
    # The result line is terminal (the executor seals the token before
    # printing it), so a lingering process is cleanup, not work: give
    # it a moment, then reap the whole group.
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        await _kill_group(proc)
    status = str(result.get("status", ""))
    if status == "connected":
        if result is first:
            # connected without a ready line is out of contract — a
            # flow that never bound loopback cannot have run consent.
            return ExecutorOutcome(status="failed", reason="protocol")
        return ExecutorOutcome(status="connected")
    if status == "failed":
        reason = str(result.get("reason", ""))
        if reason not in SCHEMA_REASONS:
            # Clamp: the reason field is a closed enum; anything else
            # could carry free text into the Layer-B projection.
            reason = "internal_error"
        return ExecutorOutcome(status="failed", reason=reason)
    return ExecutorOutcome(status="failed", reason="protocol")
