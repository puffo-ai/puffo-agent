"""Server scheduler transport and acknowledged, durable Inbox delivery."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable
from urllib.parse import quote
from uuid import UUID, uuid4

from ..crypto.http_client import PuffoCoreHttpClient
from .message_store import MessageStore

logger = logging.getLogger(__name__)


class ScheduleAPI:
    def __init__(self, http: PuffoCoreHttpClient, slug: str):
        self.http = http
        prefix = "/v2/cloud-agents" if http.keyless else "/v2"
        self.path = f"{prefix}/agents/{quote(slug, safe='')}"

    async def list(self) -> dict[str, Any]:
        path = f"{self.path}/schedules"
        if self.http.keyless:
            return await self.http.get_unsigned(path)
        return await self.http.get(path)

    async def read(self, schedule_id: str) -> dict[str, Any]:
        path = f"{self.path}/schedules/{UUID(schedule_id)}"
        if self.http.keyless:
            return await self.http.get_unsigned(path)
        return await self.http.get(path)

    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/schedules", body)

    async def update(self, schedule_id: str, version: int, body: dict[str, Any]) -> dict[str, Any]:
        path = f"{self.path}/schedules/{UUID(schedule_id)}?version={int(version)}"
        if self.http.keyless:
            return await self.http.put_unsigned(path, body)
        return await self.http.put(path, body)

    async def delete(self, schedule_id: str, version: int) -> dict[str, bool]:
        path = f"{self.path}/schedules/{UUID(schedule_id)}?version={int(version)}"
        if self.http.keyless:
            await self.http.delete_unsigned(path)
        else:
            await self.http.delete(path)
        return {"deleted": True}

    async def claim(self, claim_id: str) -> dict[str, Any]:
        return await self._post("/schedule-runs/claim", {"claim_id": claim_id})

    async def acknowledge(self, run_id: str, claim_id: str) -> None:
        await self._post(f"/schedule-runs/{UUID(run_id)}/ack", {"claim_id": claim_id})

    async def _post(self, suffix: str, body: dict[str, Any]) -> Any:
        if self.http.keyless:
            return await self.http.post_unsigned(self.path + suffix, body)
        return await self.http.post(self.path + suffix, body)


def _run_payload(run: dict[str, Any], *, slug: str, claim_id: str) -> dict[str, Any]:
    run_id = str(UUID(run["id"]))
    schedule_id = str(UUID(run["schedule_id"]))
    if run["agent_id"] != slug or run["claim_id"] != claim_id:
        raise ValueError("scheduler delivery binding mismatch")
    due = datetime.fromisoformat(run["scheduled_at"].replace("Z", "+00:00"))
    if due.tzinfo is None or not run["owner_slug"] or not isinstance(run["prompt"], str):
        raise ValueError("invalid scheduler delivery")
    return {
        "envelope_id": f"schedule-run:{run_id}",
        "envelope_kind": "dm",
        "sender_slug": run["owner_slug"],
        "recipient_slug": slug,
        "content_type": "application/puffo-schedule+json",
        "content": {
            "event_type": "scheduled_task", "sender_type": "system",
            "schedule_id": schedule_id, "run_id": run_id,
            "name": run["name"], "text": run["prompt"],
            "scheduled_at": run["scheduled_at"],
        },
        "sent_at": int(due.timestamp() * 1000),
        "is_encrypted": False,
    }


class ScheduleDelivery:
    def __init__(self, *, http: PuffoCoreHttpClient, slug: str, store: MessageStore, notify: Callable[[], None]):
        self._api = ScheduleAPI(http, slug)
        self._slug = slug
        self._store = store
        self._notify = notify
        self._claim_id = str(uuid4())

    async def deliver_once(self) -> None:
        async with asyncio.timeout(10):
            response = await self._api.claim(self._claim_id)
        for run in response["runs"]:
            payload = _run_payload(run, slug=self._slug, claim_id=self._claim_id)
            await self._store.store_local_event(
                payload, reason="scheduled task", idempotent=True,
            )
            self._notify()
            async with asyncio.timeout(10):
                await self._api.acknowledge(run["id"], self._claim_id)

    async def run(self) -> None:
        while True:
            try:
                await self.deliver_once()
            except Exception:
                # Error bodies can contain the prompt; log no response text.
                logger.warning("scheduler delivery failed; will retry")
            await asyncio.sleep(15)
