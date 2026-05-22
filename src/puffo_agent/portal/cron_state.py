"""PUF-239: per-agent cron schedules.

Sidecar persistence (NOT an ``agent.yml`` extension) — agents
self-mutate via the ``set_cron`` / ``disable_cron`` MCP tools
without a heavyweight ``AgentConfig.save()`` round-trip, and the
schema stays loosely coupled to the rest of the agent config so
future cron evolutions don't churn ``agent.yml``.

File layout::

    ~/.puffo-agent/agents/<agent_id>/.crons.json

Shape::

    {
      "crons": [
        {
          "id": "cron_<uuid-short>",
          "schedule": "0 9 * * *",
          "prompt": "Generate ticket status report and send to #Dev Team",
          "enabled": true,
          "created_at": 1716372000000,    // unix ms
          "last_fire": 1716458400000,     // unix ms or null
          "fire_count": 12
        }
      ]
    }

UTC clock throughout — timezone support is a follow-up if cohort
asks (Equation locked v1 = UTC).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from croniter import CroniterBadCronError, croniter

from .state import agent_dir

logger = logging.getLogger(__name__)


def crons_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / ".crons.json"


@dataclass
class CronSchedule:
    """One row in ``.crons.json``.

    Fields are documented at the module level. ``id`` is generated
    by ``new_cron_id()`` at registration time; ``last_fire`` /
    ``fire_count`` are bumped by the scheduler when the cron fires.
    """

    id: str
    schedule: str
    prompt: str
    enabled: bool = True
    created_at: int = 0
    last_fire: int | None = None
    fire_count: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CronSchedule":
        return cls(
            id=str(raw["id"]),
            schedule=str(raw["schedule"]),
            prompt=str(raw["prompt"]),
            enabled=bool(raw.get("enabled", True)),
            created_at=int(raw.get("created_at", 0)),
            last_fire=(
                int(raw["last_fire"])
                if raw.get("last_fire") is not None
                else None
            ),
            fire_count=int(raw.get("fire_count", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # ``asdict`` keeps ``None`` for ``last_fire``; that's the
        # intended wire shape (preserves "never fired" semantics).
        return d


# Sentinel for "no crons file yet" — distinct from "empty list" so
# callers can choose whether to materialise the file on first write
# or stay no-op when nothing's been registered.
_EMPTY: list[CronSchedule] = []


def new_cron_id() -> str:
    """Short, opaque, URL-safe id. The first 12 hex chars of a UUID4
    is plenty of entropy for the per-agent uniqueness contract."""
    return f"cron_{uuid.uuid4().hex[:12]}"


def now_ms() -> int:
    return int(time.time() * 1000)


def validate_schedule(schedule: str) -> tuple[bool, str]:
    """Returns ``(ok, reason)``. ``reason`` is the empty string on
    success and a human-readable rejection on failure. Wraps
    ``croniter``'s exception type so callers don't import the
    library themselves."""
    if not isinstance(schedule, str) or not schedule.strip():
        return False, "schedule must be a non-empty string"
    try:
        croniter(schedule.strip())
    except (CroniterBadCronError, ValueError) as exc:
        return False, f"invalid cron expression: {exc}"
    return True, ""


def load_crons(agent_id: str) -> list[CronSchedule]:
    """Read the sidecar. Returns an empty list when the file is
    absent / corrupt — by design the scheduler should never crash
    over a malformed sidecar (it would block all agents)."""
    path = crons_path(agent_id)
    if not path.exists():
        return list(_EMPTY)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "agent %s: .crons.json unreadable (%s); treating as empty",
            agent_id, exc,
        )
        return list(_EMPTY)
    rows = raw.get("crons") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return list(_EMPTY)
    out: list[CronSchedule] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            out.append(CronSchedule.from_dict(r))
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "agent %s: dropping malformed .crons.json row (%s): %s",
                agent_id, exc, r,
            )
    return out


def save_crons(agent_id: str, crons: list[CronSchedule]) -> None:
    """Atomic write — temp-file + ``os.replace``. Same shape as
    ``RuntimeState.save`` so a crash mid-write leaves the previous
    sidecar intact."""
    path = crons_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"crons": [c.to_dict() for c in crons]}
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def upsert_cron(agent_id: str, cron: CronSchedule) -> CronSchedule:
    """Insert if absent, replace by ``id`` otherwise. Returns the
    persisted row. Used by the scheduler to bump ``last_fire`` +
    ``fire_count`` and by ``set_cron`` to register new schedules."""
    crons = load_crons(agent_id)
    for i, existing in enumerate(crons):
        if existing.id == cron.id:
            crons[i] = cron
            save_crons(agent_id, crons)
            return cron
    crons.append(cron)
    save_crons(agent_id, crons)
    return cron


def disable_cron(agent_id: str, cron_id: str) -> CronSchedule | None:
    """Flip ``enabled = False`` on the matching row. Returns the
    updated row, or ``None`` when no row matches the id."""
    crons = load_crons(agent_id)
    for i, c in enumerate(crons):
        if c.id == cron_id:
            updated = replace(c, enabled=False)
            crons[i] = updated
            save_crons(agent_id, crons)
            return updated
    return None
