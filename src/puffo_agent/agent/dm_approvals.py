"""Persistence for pending per-agent DM approval prompts."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..portal.state import agent_dir

logger = logging.getLogger(__name__)

# Marker distinguishing a keyless resumable approval record from a legacy
# native prompt record inside the same flat JSON file.
KEYLESS_APPROVAL_KIND = "keyless_dm_approval"

# The exact operator decisions a keyless record may carry. "pending" means
# no exact answer has been received yet.
DM_APPROVAL_DECISIONS = frozenset({"pending", "approved", "denied"})

# Completion phases: a record opens ``pending`` and moves to ``applied`` once
# the allow/block mutation and denial tombstone have been committed.
DM_APPROVAL_PHASES = frozenset({"pending", "applied"})


@dataclass(frozen=True)
class KeylessDmApproval:
    """Resumable keyless DM approval, keyed by the held foreign envelope.

    The bridge orchestration needs one durable record per held DM so a
    restart can still correlate the operator's reply to the exact envelope.
    ``envelope_id`` is both the JSON key and a validated field; ``server_seq``
    and the prompt envelope/thread ids are genuinely optional (the keyless
    lane can be sequence-less and a prompt may have no envelope/thread).
    ``decision`` is the exact operator answer and ``phase`` its completion
    stage — both restricted to the vocabularies above.
    """

    envelope_id: str
    sender_slug: str
    operator_slug: str
    server_seq: int | None
    prompt_client_ref: str
    prompt_envelope_id: str | None
    prompt_thread_id: str | None
    decision: str
    phase: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": KEYLESS_APPROVAL_KIND,
            "envelope_id": self.envelope_id,
            "sender_slug": self.sender_slug,
            "operator_slug": self.operator_slug,
            "server_seq": self.server_seq,
            "prompt_client_ref": self.prompt_client_ref,
            "prompt_envelope_id": self.prompt_envelope_id,
            "prompt_thread_id": self.prompt_thread_id,
            "decision": self.decision,
            "phase": self.phase,
        }


def parse_keyless_approval(value: Any) -> KeylessDmApproval | None:
    """Validate one keyless record's exact shape; ``None`` when malformed.

    Every field is type-checked and the decision/phase vocabulary enforced,
    so a corrupt or hand-edited record can never silently resurface as
    approval state. ``server_seq`` and the prompt thread/envelope ids are
    genuinely optional; the rest are required.
    """
    if not isinstance(value, dict) or value.get("kind") != KEYLESS_APPROVAL_KIND:
        return None
    envelope_id = value.get("envelope_id")
    sender_slug = value.get("sender_slug")
    operator_slug = value.get("operator_slug")
    prompt_client_ref = value.get("prompt_client_ref")
    if not isinstance(envelope_id, str) or not envelope_id:
        return None
    if not isinstance(sender_slug, str) or not sender_slug:
        return None
    if not isinstance(operator_slug, str):
        return None
    server_seq = value.get("server_seq")
    if server_seq is not None and (
        isinstance(server_seq, bool) or not isinstance(server_seq, int)
    ):
        return None
    if not isinstance(prompt_client_ref, str) or not prompt_client_ref:
        return None
    prompt_envelope_id = value.get("prompt_envelope_id")
    if prompt_envelope_id is not None and not isinstance(prompt_envelope_id, str):
        return None
    prompt_thread_id = value.get("prompt_thread_id")
    if prompt_thread_id is not None and not isinstance(prompt_thread_id, str):
        return None
    decision = value.get("decision")
    phase = value.get("phase")
    if decision not in DM_APPROVAL_DECISIONS or phase not in DM_APPROVAL_PHASES:
        return None
    return KeylessDmApproval(
        envelope_id=envelope_id,
        sender_slug=sender_slug,
        operator_slug=operator_slug,
        server_seq=server_seq,
        prompt_client_ref=prompt_client_ref,
        prompt_envelope_id=prompt_envelope_id,
        prompt_thread_id=prompt_thread_id,
        decision=decision,
        phase=phase,
    )


def _pending_dir(slug: str) -> Path:
    return agent_dir(slug) / ".puffo-agent"


def pending_dm_approvals_path(slug: str) -> Path:
    return _pending_dir(slug) / "pending_dm_approvals.json"


def load_pending_dm_approvals(slug: str) -> dict[str, dict[str, Any]]:
    """Load legacy native prompt records plus valid keyless records from one
    flat file. Malformed keyless records are skipped (never resurfaced as
    approval state); legacy native dictionary records pass through unchanged.
    """
    path = pending_dm_approvals_path(slug)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "pending_dm_approvals: %s unreadable (%s); starting empty",
            path,
            exc,
        )
        return {}
    if not isinstance(raw, dict):
        return {}
    loaded: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        if value.get("kind") == KEYLESS_APPROVAL_KIND:
            parsed = parse_keyless_approval(value)
            if parsed is None or parsed.envelope_id != key:
                logger.warning(
                    "pending_dm_approvals: skipping malformed keyless "
                    "record %s",
                    key,
                )
                continue
            loaded[key] = parsed.to_dict()
        else:
            loaded[key] = value
    return loaded


def save_pending_dm_approvals(
    slug: str,
    pending: dict[str, dict[str, Any]],
) -> None:
    path = pending_dm_approvals_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(pending, indent=2), encoding="utf-8")
    os.replace(tmp, path)
