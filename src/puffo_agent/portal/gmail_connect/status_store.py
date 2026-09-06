"""Sanitized Gmail-connector status for this machine.

The OAuth executor owns the token file; the daemon stores only this
projection. Serialization is whitelist-only via ``projection()`` so a
new field can't reach disk, the control plane, or the local RPC without
being added here deliberately — consumers of this file can never learn
credential material because none is ever accepted into the type.
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
    account_masked: str = ""
    # RFC3339 string from the executor; opaque to the daemon.
    expires_at: str = ""
    # Coarse reason only (§4 coarsening); detail stays in the executor.
    reason: str = ""
    updated_at: float = 0.0

    def projection(self) -> dict:
        """The only serialization: an explicit whitelist."""
        return {
            "state": self.state,
            "account_masked": self.account_masked,
            "expires_at": self.expires_at,
            "reason": self.reason,
        }


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
        account_masked=str(raw.get("account_masked", "")),
        expires_at=str(raw.get("expires_at", "")),
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


def mask_account(email: str) -> str:
    """Keep at most two leading characters of the local part.

    The raw address never persists: masking happens before the value
    enters ``GmailConnectStatus``, not at display time.
    """
    email = email.strip()
    if "@" not in email:
        return "***" if email else ""
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}"
