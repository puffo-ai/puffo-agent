"""Durable operator gate for a provider turn whose effects are unknown."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from ..portal.host_assets import atomic_write_private


@dataclass(frozen=True)
class TurnRecovery:
    session_ref: str
    turn_ref: str
    provider_session_id: str
    provider_turn_id: str
    owner: str
    reason: str
    durable_turn_id: str = ""
    stop_attempted: bool = False
    stopped: bool = False
    retry_requested: bool = False
    resolved: bool = False


def recovery_path(workspace: str | Path) -> Path:
    return Path(workspace) / ".puffo-agent" / "turn_recovery.json"


def read_recovery(workspace: str | Path) -> TurnRecovery | None:
    try:
        raw = json.loads(recovery_path(workspace).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    # Corrupt/unknown records fail closed rather than permitting a replay.
    record = TurnRecovery(**raw)
    for name in ("session_ref", "turn_ref", "owner", "reason"):
        if not isinstance(getattr(record, name), str) or not getattr(record, name):
            raise ValueError("invalid turn recovery identity")
    for name in ("stop_attempted", "stopped", "retry_requested", "resolved"):
        if type(getattr(record, name)) is not bool:
            raise ValueError("invalid turn recovery phase")
    return record


def write_recovery(workspace: str | Path, record: TurnRecovery) -> None:
    path = recovery_path(workspace)
    atomic_write_private(path, json.dumps(asdict(record), indent=2))
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def recovery_required(workspace: str | Path) -> bool:
    record = read_recovery(workspace)
    return record is not None and not record.resolved
