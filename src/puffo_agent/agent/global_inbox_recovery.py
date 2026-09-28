"""Startup self-healing for quarantines with nothing to replay."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from ._logging import log_runtime_event
from .turn_recovery import read_recovery, write_recovery

logger = logging.getLogger(__name__)


async def resolve_effectless_recovery(
    *, workspace: Path, store: Any, agent_id: str,
) -> bool:
    """True when no quarantine blocks startup recovery.

    Operator gate stays for replayable rows; stopped + row-free resolves.
    """
    record = read_recovery(workspace)
    if record is None or record.resolved:
        return True
    if not record.stopped or record.retry_requested:
        return False
    turn_id = record.durable_turn_id
    if not turn_id:
        candidates = [
            run
            for run in await store.get_active_turn_runs()
            if (run.provider_session_id or "") == record.provider_session_id
        ]
        if len(candidates) != 1:
            return False
        turn_id = candidates[0].turn_id
    run = await store.get_turn_run(turn_id)
    if run is not None and run.message_ids:
        return False
    write_recovery(workspace, replace(record, resolved=True))
    log_runtime_event(
        logger,
        "turn.recovery_resolved",
        agent_id=agent_id,
        turn_id=turn_id,
        provider_session_id=record.provider_session_id,
        reason=record.reason,
        mode="startup_effectless_recovery",
        outcome="resolved",
    )
    return True
