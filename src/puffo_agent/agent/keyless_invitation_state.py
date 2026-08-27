"""Durable per-Agent keyless invitation decision state.

One compact JSON document under ``agent_dir(slug) / ".puffo-agent"`` holds
one record per authoritative ``invitation_event_id``. The exact event id is
both the JSON key and a validated field, and each record retains the same
stable prompt / decision ``client_ref``s across a reload so a reconnect can
resume an unsent prompt or an undecided/decided record without generating a
second logical prompt or decision.

Writes are atomic (sibling ``*.tmp`` plus ``os.replace``) and fail closed:
the caller keeps its prior in-memory mirror when a save raises ``OSError``.
Malformed or key-mismatched rows are skipped with an observable warning so a
corrupt or hand-edited file can never resurface as decision state.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..portal.state import agent_dir

logger = logging.getLogger(__name__)

# Marker distinguishing a keyless invitation record inside the JSON file.
KEYLESS_INVITATION_KIND = "keyless_invitation"

# Completion phases: ``unsent`` (prompt not yet sent / send failed),
# ``awaiting_reply`` (prompt sent, operator reply pending), ``decided``
# (operator decision durable, decision frame not yet resolved), and
# ``terminal`` (server decision outcome recorded).
INVITATION_PHASES = frozenset({"unsent", "awaiting_reply", "decided", "terminal"})

# The exact operator decisions a keyless record may carry. ``pending``
# means no exact answer has been received yet.
INVITATION_DECISIONS = frozenset({"pending", "accept", "reject"})

# Terminal decision outcomes from the Server contract. ``applied`` and
# ``already_applied`` are terminal success; ``conflict`` and ``not_found``
# resolve without a retry loop.
INVITATION_OUTCOMES = frozenset(
    {"applied", "already_applied", "conflict", "not_found"}
)

# Authoritative invite fields required on every snapshot row.
_REQUIRED_INVITE_FIELDS = ("invitation_event_id", "scope", "space_id")


@dataclass(frozen=True)
class KeylessInvitation:
    """Resumable keyless invitation record, keyed by ``invitation_event_id``.

    ``invite`` is the authoritative invite snapshot as delivered by the
    Server (validated on parse). ``prompt_client_ref`` is stable across
    reconnects; ``prompt_envelope_id`` / ``prompt_thread_id`` carry the
    returned prompt identity once the prompt is sent. ``decision`` is the
    exact operator answer and ``phase`` its completion stage; ``outcome``
    records the Server's terminal decision result.
    """

    invitation_event_id: str
    invite: dict[str, Any]
    prompt_client_ref: str
    prompt_envelope_id: str | None
    prompt_thread_id: str | None
    decision: str
    decision_client_ref: str | None
    phase: str
    outcome: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": KEYLESS_INVITATION_KIND,
            "invitation_event_id": self.invitation_event_id,
            "invite": self.invite,
            "prompt_client_ref": self.prompt_client_ref,
            "prompt_envelope_id": self.prompt_envelope_id,
            "prompt_thread_id": self.prompt_thread_id,
            "decision": self.decision,
            "decision_client_ref": self.decision_client_ref,
            "phase": self.phase,
            "outcome": self.outcome,
        }


def parse_keyless_invitation(value: Any) -> KeylessInvitation | None:
    """Validate one keyless record's exact shape; ``None`` when malformed.

    Every field is type-checked, the decision/phase/outcome vocabularies
    are enforced, the embedded invite snapshot must carry the authoritative
    fields, and a terminal phase must carry a valid outcome (while a
    non-terminal phase must not), so a corrupt record can never silently
    resurface as promptable or decidable state.
    """
    if not isinstance(value, dict) or value.get("kind") != KEYLESS_INVITATION_KIND:
        return None
    invitation_event_id = value.get("invitation_event_id")
    if not isinstance(invitation_event_id, str) or not invitation_event_id:
        return None
    invite = value.get("invite")
    if not isinstance(invite, dict):
        return None
    for field in _REQUIRED_INVITE_FIELDS:
        if not isinstance(invite.get(field), str) or not invite.get(field):
            return None
    if invite.get("scope") not in ("space", "channel"):
        return None
    if invite.get("invitation_event_id") != invitation_event_id:
        return None
    prompt_client_ref = value.get("prompt_client_ref")
    if not isinstance(prompt_client_ref, str) or not prompt_client_ref:
        return None
    prompt_envelope_id = value.get("prompt_envelope_id")
    if prompt_envelope_id is not None and not isinstance(prompt_envelope_id, str):
        return None
    prompt_thread_id = value.get("prompt_thread_id")
    if prompt_thread_id is not None and not isinstance(prompt_thread_id, str):
        return None
    decision = value.get("decision")
    decision_client_ref = value.get("decision_client_ref")
    if decision not in INVITATION_DECISIONS:
        return None
    if decision_client_ref is not None and (
        not isinstance(decision_client_ref, str) or not decision_client_ref
    ):
        return None
    phase = value.get("phase")
    outcome = value.get("outcome")
    if phase not in INVITATION_PHASES:
        return None
    if phase == "terminal":
        if outcome not in INVITATION_OUTCOMES:
            return None
    elif outcome is not None:
        return None
    return KeylessInvitation(
        invitation_event_id=invitation_event_id,
        invite=invite,
        prompt_client_ref=prompt_client_ref,
        prompt_envelope_id=prompt_envelope_id,
        prompt_thread_id=prompt_thread_id,
        decision=decision,
        decision_client_ref=decision_client_ref,
        phase=phase,
        outcome=outcome,
    )


def keyless_invitations_path(slug: str) -> Path:
    return agent_dir(slug) / ".puffo-agent" / "keyless_invitations.json"


def load_keyless_invitations(slug: str) -> dict[str, dict[str, Any]]:
    """Load the per-Agent invitation document; malformed or
    key-mismatched rows are skipped with a warning, never resurfaced.
    """
    path = keyless_invitations_path(slug)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "keyless_invitations: %s unreadable (%s); starting empty",
            path,
            exc,
        )
        return {}
    if not isinstance(raw, dict):
        return {}
    loaded: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        parsed = parse_keyless_invitation(value)
        if parsed is None or parsed.invitation_event_id != key:
            logger.warning(
                "keyless_invitations: skipping malformed record %s", key,
            )
            continue
        loaded[key] = parsed.to_dict()
    return loaded


def save_keyless_invitations(
    slug: str,
    pending: dict[str, dict[str, Any]],
) -> None:
    """Atomically persist the whole invitation document.

    Writes a sibling ``*.tmp`` file then atomically replaces the target, so
    readers do not observe an in-progress write during ordinary process
    failure. This does not claim power-loss durability because neither the
    temporary file nor its directory is explicitly fsynced. Raises ``OSError``
    on failure so the caller restores its in-memory mirror.
    """
    path = keyless_invitations_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(pending, indent=2), encoding="utf-8")
    os.replace(tmp, path)
