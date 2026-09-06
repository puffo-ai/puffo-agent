"""Subprocess seam to the gateway OAuth executor.

Invoke contract v1 (pending the gateway side's counter-proposal):

- The daemon spawns the locally configured entrypoint with no
  request-derived argv and writes exactly one line of JSON to stdin.
  Parameters never travel via env or argv — both are readable by any
  same-UID process (``ps -E``).
- The executor prints one JSON ready line, then one JSON terminal
  result line, to stdout. Each read has a deadline; a silent or
  malformed executor is killed (whole process group — executors may
  spawn browsers/listeners) and reported as a coarse failure.
- stderr is discarded: failure detail crosses only as the structured
  coarse ``reason``, so token material can't reach daemon logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass


logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
READY_DEADLINE_S = 15.0
_MAX_LINE_BYTES = 64 * 1024


class ExecutorRefused(RuntimeError):
    """Raised before any process is spawned."""


@dataclass
class ExecutorOutcome:
    status: str  # "connected" | "failed" | "revoked" | "disconnected"
    account: str = ""
    expires_at: str = ""
    reason: str = ""


def _validate_request(request: dict) -> None:
    host = str(request.get("callback_host", "127.0.0.1"))
    if host not in LOOPBACK_HOSTS:
        raise ExecutorRefused(f"non-loopback callback host {host!r}")


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    try:
        await proc.wait()
    except Exception:  # noqa: BLE001 — already killing; nothing left to do
        pass


async def _read_json_line(proc: asyncio.subprocess.Process, deadline_s: float) -> dict:
    assert proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=deadline_s)
    if not line or len(line) > _MAX_LINE_BYTES:
        raise ValueError("executor closed stdout or overran line budget")
    parsed = json.loads(line)
    if not isinstance(parsed, dict):
        raise ValueError("executor line is not a JSON object")
    return parsed


async def run_gmail_executor(
    entrypoint: str,
    request: dict,
    *,
    flow_timeout_s: float,
) -> ExecutorOutcome:
    """Run one executor operation to its terminal result.

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
        ready = await _read_json_line(proc, READY_DEADLINE_S)
        if ready.get("ready") is not True:
            raise ValueError("executor ready line missing ready=true")
        result = await _read_json_line(proc, flow_timeout_s)
    except asyncio.TimeoutError:
        await _kill_group(proc)
        return ExecutorOutcome(status="failed", reason="timeout")
    except (ValueError, OSError):
        await _kill_group(proc)
        return ExecutorOutcome(status="failed", reason="protocol")
    # The result line is terminal (the executor persists the token
    # before printing it), so a lingering process is cleanup, not work:
    # give it a moment, then reap the whole group.
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        await _kill_group(proc)
    status = str(result.get("status", ""))
    if status not in ("connected", "failed", "revoked", "disconnected"):
        return ExecutorOutcome(status="failed", reason="protocol")
    return ExecutorOutcome(
        status=status,
        account=str(result.get("account", "")),
        expires_at=str(result.get("expires_at", "")),
        reason=str(result.get("reason", "")),
    )
