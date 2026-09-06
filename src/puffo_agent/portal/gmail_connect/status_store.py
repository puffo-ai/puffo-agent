"""Sanitized Gmail-connector status for this machine.

Layer B of the two-layer field boundary (design v1.6 §3, frozen):
the product projection carries ONLY the connection-state enum and the
sanitized failure reason. Serialization is whitelist-only via
``projection()`` so no Layer-A detail (token_db, bundle paths, scope,
or any storage detail) can reach disk, the control plane, or the local
RPC — none of it is even representable in this type.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..state import home_dir


logger = logging.getLogger(__name__)

STATES = ("disconnected", "pending", "connected", "failed", "revoked")


@dataclass
class GmailConnectStatus:
    state: str = "disconnected"
    # Coarse reason only (§4 closed enum); detail stays in the executor.
    reason: str = ""
    updated_at: float = 0.0

    def projection(self) -> dict:
        """The only serialization: an explicit whitelist."""
        return {"state": self.state, "reason": self.reason}


def status_path() -> Path:
    return home_dir() / "gmail_connect.json"


def load_status() -> GmailConnectStatus:
    path = status_path()
    if not path.exists():
        return GmailConnectStatus()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("gmail-connect: unreadable status file; treating as disconnected")
        return GmailConnectStatus()
    state = raw.get("state", "disconnected")
    if state not in STATES:
        state = "disconnected"
    return GmailConnectStatus(
        state=state,
        reason=str(raw.get("reason", "")),
        updated_at=float(raw.get("updated_at", 0.0)),
    )


def store_status(status: GmailConnectStatus) -> None:
    status.updated_at = time.time()
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(status.projection(), updated_at=status.updated_at)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
