"""Per-agent pending DM-approval persistence.

When ``auto_accept_dm=false`` and a foreign sender DMs the agent,
the daemon buffers the message + prompts the operator. The prompt
DM's envelope_id keys the pending entry so the operator's in-thread
y/n reply (matching ``thread_root_id == prompt_envelope_id``) routes
back to the right buffered message.

State survives daemon restart so a prompt DM the operator missed
during a restart window still gets honored on next reply.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ..portal.state import agent_dir

logger = logging.getLogger(__name__)


def _pending_dir(slug: str) -> Path:
    return agent_dir(slug) / ".puffo-agent"


def pending_dm_approvals_path(slug: str) -> Path:
    return _pending_dir(slug) / "pending_dm_approvals.json"


def load_pending_dm_approvals(slug: str) -> dict[str, dict[str, Any]]:
    path = pending_dm_approvals_path(slug)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "pending_dm_approvals: %s unreadable (%s); starting empty",
            path, exc,
        )
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def save_pending_dm_approvals(
    slug: str, pending: dict[str, dict[str, Any]],
) -> None:
    path = pending_dm_approvals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(pending, indent=2), encoding="utf-8")
    os.replace(tmp, path)
