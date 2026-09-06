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

# Constructible / loadable states. ``revoked`` is deliberately NOT here:
# it belongs to the composite Disconnect (grant revocation first, then
# token) and no code path may mint it while only the token axis exists.
# Keeping it out of this tuple makes that a structural absence rather
# than an enumeration of today's call sites — an on-disk ``revoked``
# (older build, rollback, hand-edit) clamps to ``disconnected`` instead
# of being served to the UI as a revocation that never happened.
STATES = ("disconnected", "pending", "connected", "failed")
RESERVED_STATES = ("revoked",)

# Layer-B reason roster. Executor-minted values are EXACTLY invoke schema
# v1.1 §4's closed set (60adad76…); the rest are daemon-minted. Anything
# else — including free text from a buggy or hostile executor — clamps to
# internal_error, so "no free text in Layer B" does not rest on the
# executor behaving. Structural narrowing decides *where* a value may go;
# it cannot stop the same bytes being posted into a field that is allowed
# to exist, so the roster is what actually closes that side channel.
EXECUTOR_REASONS = (
    "bundle_verify_failed",
    "callback_timeout",
    "exchange_failed",
    "scope_mismatch",
    "keychain_error",
    "internal_error",
)
DAEMON_REASONS = (
    "refused",
    "executor_unavailable",
    "non_loopback_ready",
    "timeout",
    "protocol",
    "revoke_unconfirmed",
)
REASONS = ("",) + EXECUTOR_REASONS + DAEMON_REASONS


@dataclass
class GmailConnectStatus:
    state: str = "disconnected"
    # Coarse reason only (roster above); detail stays in the executor.
    reason: str = ""
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        # Enforced on every construction, so the load path (untrusted
        # bytes on disk) is covered by the same rule as the spawn path.
        if self.state not in STATES:
            self.state = "disconnected"
        if self.reason not in REASONS:
            logger.warning(
                "gmail-connect: out-of-roster reason clamped (len=%d)",
                len(self.reason),
            )
            self.reason = "internal_error"

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
    return GmailConnectStatus(
        state=str(raw.get("state", "disconnected")),
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
