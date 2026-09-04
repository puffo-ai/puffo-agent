"""Shared bounded subprocess stream handling for CLI harness Drivers."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from ...._proc import no_window_kwargs
from ....tasks import spawn


_CREATE_NEW_PROCESS_GROUP = 0x00000200


class ProcessTreeShutdownError(RuntimeError):
    """The child tree could not be confirmed closed within its budget."""


def process_group_spawn_kwargs() -> dict[str, Any]:
    """Isolate a child so lifecycle shutdown can target its whole tree."""
    kwargs = no_window_kwargs()
    if os.name == "nt":
        kwargs["creationflags"] = (
            int(kwargs.get("creationflags", 0)) | _CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs


async def signal_process_tree(
    proc: Any,
    *,
    force: bool,
    timeout: float,
) -> None:
    """Best-effort signal of an isolated process tree."""
    pid = getattr(proc, "pid", None)
    if os.name != "nt" and isinstance(pid, int):
        try:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
            return
        except ProcessLookupError:
            # ESRCH is ambiguous: either the isolated group is already gone,
            # or a caller-supplied factory never made ``pid`` a group leader.
            # Only the direct child's return code distinguishes those cases.
            if getattr(proc, "returncode", None) is not None:
                return
        except OSError:
            # A caller-supplied factory may not have created a new session.
            # Fall back to the direct-child API in that case.
            pass
    elif os.name == "nt" and isinstance(pid, int):
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        taskkill = None
        try:
            taskkill = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **no_window_kwargs(),
            )
            returncode = await asyncio.wait_for(
                taskkill.wait(), timeout=timeout
            )
            if returncode == 0:
                return
        except (OSError, TimeoutError):
            if taskkill is not None and taskkill.returncode is None:
                taskkill.kill()
                try:
                    await asyncio.wait_for(taskkill.wait(), timeout=timeout)
                except TimeoutError:
                    pass
    if getattr(proc, "returncode", None) is not None:
        return
    getattr(proc, "kill" if force else "terminate")()


def abandon_process_transport(proc: Any) -> None:
    """Explicitly close asyncio pipe and process transports."""
    if proc is None:
        return
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(proc, stream_name, None)
        transport = getattr(stream, "_transport", None)
        if transport is not None:
            transport.close()
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()


async def shutdown_process_tree(
    proc: Any,
    *,
    waiter: asyncio.Task[Any] | None,
    timeout: float,
    task_name: str,
) -> None:
    """TERM, KILL, and confirm closure; raise on transport abandonment.

    A descendant can keep inherited stdout/stderr descriptors open after the
    direct child exits. ``proc.wait()`` then remains pending in asyncio's
    subprocess transport. Two bounded waits distinguish a clean tree shutdown
    from the final pipe/transport abandonment path; callers can therefore
    never mistake a timed-out cleanup for success.
    """
    if proc is None:
        return
    owns_waiter = waiter is None
    if waiter is None:
        waiter = spawn(proc.wait(), name=task_name)

    if getattr(proc, "returncode", None) is None:
        await signal_process_tree(proc, force=False, timeout=timeout)
    if await _waiter_settled(proc, waiter, timeout):
        abandon_process_transport(proc)
        return

    # The direct parent may already have exited while a descendant remains in
    # its process group holding inherited pipes. Always target the group/tree.
    await signal_process_tree(proc, force=True, timeout=timeout)
    if await _waiter_settled(proc, waiter, timeout):
        abandon_process_transport(proc)
        return

    abandon_process_transport(proc)
    waiter.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(waiter, return_exceptions=True), timeout=timeout
        )
    except TimeoutError:
        pass
    finally:
        if owns_waiter and not waiter.done():
            waiter.cancel()
    raise ProcessTreeShutdownError(
        "process tree did not close before transport abandonment"
    )


async def _waiter_settled(
    proc: Any, waiter: asyncio.Task[Any], timeout: float
) -> bool:
    try:
        await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
    except TimeoutError:
        return False
    except asyncio.CancelledError:
        if not waiter.cancelled():
            raise
    except Exception:
        # A failed waiter is settled as a task, but that says nothing about
        # whether the process exited. The return-code check below remains the
        # authoritative lifecycle oracle.
        pass
    return getattr(proc, "returncode", None) is not None


async def drain_subprocess_stream(stream: Any) -> None:
    """Continuously discard a child stream so OS pipe backpressure cannot stall it."""
    if stream is None:
        return
    while await stream.read(64 * 1024):
        pass


async def drain_subprocess_stream_keeping_tail(
    stream: Any, *, max_bytes: int = 8192
) -> bytes:
    """Like ``drain_subprocess_stream``, but return the last ``max_bytes`` seen.

    Diagnostic use only (e.g. logging why a CLI child died) — the caller
    still owns backpressure relief, we just remember the tail in passing.
    """
    if stream is None:
        return b""
    tail = b""
    while chunk := await stream.read(64 * 1024):
        tail = (tail + chunk)[-max_bytes:]
    return tail
