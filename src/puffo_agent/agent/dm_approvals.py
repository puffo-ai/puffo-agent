"""Per-agent pending DM-approval persistence. Entries are keyed by the
prompt DM's envelope_id (in-thread y/n routes via thread_root_id) and
survive daemon restarts.
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
