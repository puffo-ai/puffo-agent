"""Bounded, read-only LingTai evidence projected onto public runtime health."""
from __future__ import annotations

import math
import stat
import time

from .control.lingtai_profile import _read_source_object, is_lingtai_runtime
from .lingtai_profile_sync import _source_directory
from .state import RuntimeConfig

# UI warnings, not diagnoses or automatic restart triggers. Long active turns
# (provider/tool calls, permissions) are deliberately excluded from stalled.
HEARTBEAT_MAX_AGE_S = 15.0
STUCK_WARNING_AFTER_S = 120.0
NO_TURN_WARNING_AFTER_S = 300.0


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def reported_runtime_health(
    *, runtime: RuntimeConfig, current_health: str, worker_status: str,
    worker_started_at: float, now: float | None = None,
) -> str:
    """Never replace a specific worker failure or publish source file contents."""
    if (worker_status != "running" or current_health not in {"ok", "unknown", "in_progress"}
            or not is_lingtai_runtime(runtime)):
        return current_health
    directory = _source_directory(runtime.harness_command)
    if directory is None:
        return current_health
    clock = time.time() if now is None else now
    try:
        try:
            heartbeat = (directory / ".agent.heartbeat").lstat()
        except FileNotFoundError:
            # A supported current process can leave its last healthy snapshot
            # behind after graceful exit. Missing heartbeat then is evidence;
            # an old/unsupported source with no snapshot is not.
            snapshot = (directory / ".status.json").lstat()
            if stat.S_ISREG(snapshot.st_mode) and worker_started_at <= snapshot.st_mtime <= clock:
                return "runtime_unresponsive"
            return current_health
        if not stat.S_ISREG(heartbeat.st_mode) or heartbeat.st_mtime < worker_started_at:
            return current_health
        age = clock - heartbeat.st_mtime
        if age < 0:
            return current_health  # clock skew is not a runtime diagnosis
        if age > HEARTBEAT_MAX_AGE_S:
            return "runtime_unresponsive"
        snapshot_path = directory / ".status.json"
        snapshot = _read_source_object(snapshot_path)
        snapshot_age = clock - snapshot_path.lstat().st_mtime
        if not 0 <= snapshot_age <= HEARTBEAT_MAX_AGE_S:
            return current_health
        source = snapshot.get("runtime")
        if not isinstance(source, dict):
            return current_health
        state = source.get("state")
        changed = _number(source.get("state_changed_at"))
        progress = _number(source.get("no_progress_seconds"))
        if state == "stuck" and changed is not None and 0 <= changed <= clock - STUCK_WARNING_AFTER_S:
            return "runtime_stalled"
        if (state == "active" and snapshot.get("active_turn") is None
                and progress is not None and progress >= NO_TURN_WARNING_AFTER_S):
            return "runtime_stalled"
    except (OSError, ValueError, RecursionError):
        # Older runtimes, missing files and interrupted writes contribute no
        # evidence. Existing worker/transport health remains authoritative.
        pass
    return current_health
