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
    "confirm_timeout",
    "confirm_unavailable",
    "executor_unavailable",
    "non_loopback_ready",
    "timeout",
    "protocol",
    "revoke_unconfirmed",
)
REASONS = ("",) + EXECUTOR_REASONS + DAEMON_REASONS

# Reasons `run_gmail_executor` can put on a failed outcome: the child's
# closed set plus the four the wrapper mints itself. `_reply` does not
# clamp the latter away — they are in REASONS — so they reach the caller
# and persist (Bob 188676, Jeff 188677).
EXECUTOR_FAILURE_REASONS = EXECUTOR_REASONS + (
    "executor_unavailable",
    "non_loopback_ready",
    "timeout",
    "protocol",
)

# Exact `(state, reason)` pairs a producer can actually write, plus the
# default returned when no file exists. This is the PERSIST surface, and
# it is NOT the reply surface: `(pending, "")` is never a reply, and the
# three confirm refusals are replies that never persist.
#
# Validated as a pair, not field by field. Per-field clamping let
# combinations survive that no producer can emit — `(connected,
# 'revoke_unconfirmed')`, `(disconnected, 'refused')`, `(pending,
# 'revoke_unconfirmed')` all passed through intact (测试姬 188700), and a
# reason retired in a later version silently turned every existing file
# into `(state, 'internal_error')` while keeping its old state, so a
# clean Disconnect would read as an error against a machine still
# claiming `connected` (Boris 188696).
PERSISTABLE_PAIRS = frozenset(
    {
        ("disconnected", ""),
        ("pending", ""),
        ("connected", ""),
        ("disconnected", "revoke_unconfirmed"),
        ("failed", "refused"),
    }
    | {("failed", reason) for reason in EXECUTOR_FAILURE_REASONS}
)

# Anything else fails closed to exactly this, rather than to three new
# pairs. Untrusted or stale bytes must never keep claiming `connected` or
# `pending`: the Layer-B projection is only what the UI shows, and the
# token's real fate lives in the executor's sealed store, so the honest
# move is to claim nothing (Jeff 188697). The cost is explicit — a disk
# saying `connected` displays as disconnected while a token may still
# exist.
UNTRUSTED_STATUS_PAIR = ("disconnected", "internal_error")


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
    # Checked on the RAW values, before `GmailConnectStatus` clamps them.
    # Per-field clamping destroys the evidence this check needs: an
    # unknown state becomes `disconnected`, so `("bogus", "")` would
    # arrive here as the perfectly legal `("disconnected", "")` and read
    # in the UI as a clean, deliberate disconnect rather than as a file
    # we cannot account for. Field-wise a value may be legal; as a pair
    # no producer can emit it, so its state claim is not evidence.
    disk_pair = (
        str(raw.get("state", "disconnected")),
        str(raw.get("reason", "")),
    )
    updated_at = float(raw.get("updated_at", 0.0))
    if disk_pair not in PERSISTABLE_PAIRS:
        logger.warning(
            "gmail-connect: status pair %r has no producer; failing closed",
            disk_pair,
        )
        state, reason = UNTRUSTED_STATUS_PAIR
        return GmailConnectStatus(state=state, reason=reason, updated_at=updated_at)
    return GmailConnectStatus(
        state=disk_pair[0], reason=disk_pair[1], updated_at=updated_at
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
