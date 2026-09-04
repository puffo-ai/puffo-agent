"""Preserve primary failures while making cleanup failures queryable."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from ....tasks import spawn


_CLEANUP_ERRORS_ATTR = "_puffo_cleanup_errors"
_SUPPRESSED_ERRORS_ATTR = "_puffo_suppressed_primary_errors"
CLEANUP_TIMEOUT_SECONDS = 10.0


class CleanupTimeoutError(TimeoutError):
    """A supervised cleanup operation exceeded its explicit upper bound."""


def attach_cleanup_error(
    primary: BaseException, cleanup: BaseException
) -> None:
    """Attach structured cleanup evidence without replacing the primary."""
    errors = (*cleanup_errors(primary), cleanup)
    setattr(primary, _CLEANUP_ERRORS_ATTR, errors)
    primary.add_note(
        f"puffo cleanup failure: {type(cleanup).__name__}: {cleanup}"
    )


def attach_suppressed_primary_error(
    cancellation: BaseException, primary: BaseException
) -> None:
    """Preserve a real failure displaced by the raw cancellation contract."""
    value = getattr(cancellation, _SUPPRESSED_ERRORS_ATTR, ())
    if not isinstance(value, tuple):
        raise TypeError("malformed structured suppressed-error evidence")
    setattr(cancellation, _SUPPRESSED_ERRORS_ATTR, (*value, primary))
    cancellation.add_note(
        "puffo primary failure suppressed by cancellation: "
        f"{type(primary).__name__}: {primary}"
    )


def mark_cleanup_checked(primary: BaseException) -> None:
    """Record that the structured cleanup protocol ran without a failure."""
    if not hasattr(primary, _CLEANUP_ERRORS_ATTR):
        setattr(primary, _CLEANUP_ERRORS_ATTR, ())


def cleanup_errors(error: BaseException) -> tuple[BaseException, ...]:
    """Return structured cleanup failures attached to an error."""
    if not hasattr(error, _CLEANUP_ERRORS_ATTR):
        raise LookupError("error has no structured cleanup evidence")
    value = getattr(error, _CLEANUP_ERRORS_ATTR)
    if not isinstance(value, tuple) or not all(
        isinstance(item, BaseException) for item in value
    ):
        raise TypeError("malformed structured cleanup evidence")
    return value


def suppressed_primary_errors(
    error: BaseException,
) -> tuple[BaseException, ...]:
    """Return real failures displaced by cancellation, if any."""
    value = getattr(error, _SUPPRESSED_ERRORS_ATTR, ())
    if not isinstance(value, tuple) or not all(
        isinstance(item, BaseException) for item in value
    ):
        raise TypeError("malformed structured suppressed-error evidence")
    return value


async def collect_cleanup_errors(
    awaitable: Awaitable[Any],
    errors: list[BaseException],
    *,
    timeout: float,
) -> None:
    """Supervise cleanup through cancellation, but never beyond ``timeout``."""
    if timeout <= 0:
        raise ValueError("cleanup timeout must be positive")

    async def finish() -> Any:
        return await awaitable

    task = spawn(finish(), name="harness.cleanup")
    deadline = asyncio.get_running_loop().time() + timeout
    cancellation: asyncio.CancelledError | None = None

    def settle_done_task(
        *,
        preserve_cancellation: bool = True,
        destination: list[BaseException] | None = None,
    ) -> bool:
        """Collect the task's actual terminal state when it has one."""
        nonlocal cancellation
        if not task.done():
            return False
        settled_errors = errors if destination is None else destination
        try:
            task.result()
        except asyncio.CancelledError as exc:
            if preserve_cancellation and cancellation is None:
                cancellation = exc
                settled_errors.append(exc)
        except BaseException as exc:
            settled_errors.append(exc)
        return True

    async def cancel_without_waiting_forever() -> None:
        nonlocal cancellation
        task.cancel()
        try:
            # Give cancellation-aware coroutines one scheduling turn.  A
            # coroutine that needs longer or suppresses cancellation remains
            # owned by the task supervisor rather than blocking this caller.
            await asyncio.sleep(0)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
                errors.append(exc)

    async def record_collector_timeout() -> None:
        """Cancel at the deadline and preserve any immediate terminal state."""
        await cancel_without_waiting_forever()
        terminal_errors: list[BaseException] = []
        # Settle after the helper's yield and before recording our conclusion.
        # Cancellation is the expected result of enforcing our own deadline;
        # retain only a distinct terminal failure produced while handling it.
        settle_done_task(
            preserve_cancellation=False, destination=terminal_errors
        )
        errors.append(
            CleanupTimeoutError(f"cleanup exceeded {timeout:g} seconds")
        )
        errors.extend(terminal_errors)

    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            await record_collector_timeout()
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
                errors.append(exc)
            if settle_done_task():
                return
            continue
        except TimeoutError:
            # ``wait_for`` also propagates TimeoutError raised by the task.
            # Inspect a completed task before synthesizing our own deadline.
            if settle_done_task():
                return
            await record_collector_timeout()
            return
        except BaseException as exc:
            errors.append(exc)
        return


def raise_collected_errors(
    label: str, errors: list[BaseException]
) -> None:
    """Raise ordered failures without grouping or replacing cancellation.

    Callers append a primary operation failure first, then failures from
    cleanup operations in execution order.  Entries before cancellation are
    therefore classified as suppressed primary failures; entries after it
    are classified as cleanup failures.
    """
    if not errors:
        return
    cancelled_index = next(
        (
            index
            for index, error in enumerate(errors)
            if isinstance(error, asyncio.CancelledError)
        ),
        None,
    )
    cancelled = (
        errors[cancelled_index] if cancelled_index is not None else None
    )
    if cancelled is not None:
        mark_cleanup_checked(cancelled)
        assert cancelled_index is not None
        for error in errors[:cancelled_index]:
            attach_suppressed_primary_error(cancelled, error)
        for error in errors[cancelled_index + 1 :]:
            if not isinstance(error, asyncio.CancelledError):
                attach_cleanup_error(cancelled, error)
        raise cancelled
    non_exception = next(
        (error for error in errors if not isinstance(error, Exception)),
        None,
    )
    if non_exception is not None:
        mark_cleanup_checked(non_exception)
        for error in errors:
            if error is not non_exception:
                attach_cleanup_error(non_exception, error)
        raise non_exception
    if len(errors) == 1:
        mark_cleanup_checked(errors[0])
        raise errors[0]
    group = ExceptionGroup(label, errors)  # type: ignore[arg-type]
    setattr(group, _CLEANUP_ERRORS_ATTR, tuple(errors[1:]))
    raise group
