"""Validate signed schedule rules and commit one occurrence before transport ACK."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING
from collections.abc import Callable
from uuid import UUID

from ..crypto.message import MessagePayload

if TYPE_CHECKING:
    from .message_store import MessageStore

CONTENT_TYPE = "application/puffo-schedule+json"


def validate_schedule_text(content: dict[str, Any]) -> None:
    for key, maximum in (("name", 120), ("prompt", 16384)):
        if not isinstance(content[key], str) or not content[key].strip() or len(content[key].encode()) > maximum:
            raise ValueError("invalid encrypted schedule content")


def validate_plan(payload: MessagePayload, delivery: dict[str, Any], *, agent: str, sender: str) -> None:
    content = payload.content
    expected = {"version": 1, "agent_id": agent, **{key: delivery[key] for key in (
        "schedule_id", "revision", "owner_slug", "first_run_at", "interval_seconds",
    )}}
    if (payload.content_type != CONTENT_TYPE or payload.envelope_kind != "dm"
            or payload.recipient_slug != agent
            or payload.sender_slug != sender
            or payload.sender_slug not in (agent, delivery["owner_slug"])
            or not isinstance(content, dict)
            or any(content.get(key) != value for key, value in expected.items())):
        raise ValueError("schedule signature binding mismatch")
    UUID(content["schedule_id"])
    UUID(content["revision"])
    validate_schedule_text(content)


def verified_occurrence(payload: MessagePayload, delivery: dict[str, Any], *, agent: str, owner: str) -> dict[str, Any]:
    validate_plan(payload, delivery, agent=agent, sender=delivery["template"]["sender_slug"])
    due, first, interval = delivery["scheduled_at"], delivery["first_run_at"], delivery["interval_seconds"]
    if (delivery["type"] != "scheduled_message_envelope" or delivery["version"] != 1
            or not owner or delivery["owner_slug"] != owner or payload.content["enabled"] is not True
            or type(due) is not int or type(first) is not int or due < first
            or due > int(time.time() * 1000) + 5000):
        raise ValueError("invalid scheduled occurrence")
    if interval is None:
        valid = due == first
    else:
        valid = type(interval) is int and 60 <= interval <= 31536000 and (due - first) % (interval * 1000) == 0
    if not valid:
        raise ValueError("occurrence does not match signed recurrence")
    return {
        "event_type": "scheduled_task", "sender_type": "system",
        "schedule_id": delivery["schedule_id"], "revision": delivery["revision"],
        "owner_slug": owner, "scheduled_at": due,
        "name": payload.content["name"], "text": payload.content["prompt"],
    }


async def commit_occurrence(content: dict[str, Any], *, store: MessageStore, agent: str,
                            owner: str, notify: Callable[[], None] | None) -> None:
    """Both native verification and the trusted KMS bridge land here."""
    schedule, revision = str(UUID(content["schedule_id"])), str(UUID(content["revision"]))
    due = content["scheduled_at"]
    if (type(due) is not int or not owner or content["owner_slug"] != owner
            or content["event_type"] != "scheduled_task" or content["sender_type"] != "system"):
        raise ValueError("scheduled occurrence owner mismatch")
    run_id = f"{schedule}:{revision}:{due}"
    event = {**content, "run_id": run_id, "scheduled_at": datetime.fromtimestamp(due / 1000, timezone.utc).isoformat()}
    await store.store_local_event({
        "envelope_id": f"schedule-run:{run_id}", "envelope_kind": "dm",
        "sender_slug": owner, "recipient_slug": agent,
        "content_type": CONTENT_TYPE, "content": event, "sent_at": due, "is_encrypted": True,
    }, reason="scheduled task", idempotent=True)
    if notify is not None:
        notify()
