"""Terminal durable retry health is honest and bounded."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from puffo_agent.agent.context_controller import (
    ContextCapabilities,
    ContextSnapshot,
    ProviderAdmissionEvent,
)
from puffo_agent.agent.core import AgentAPIError
from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime
from puffo_agent.agent.message_store import MessageStore, ReceiptDisposition


class Adapter:
    def __init__(self):
        self.callback = None
        self.key = ""

    async def get_context_snapshot(self):
        return ContextSnapshot(0, 200_000, "test", datetime.now(timezone.utc))

    def get_context_capabilities(self):
        return ContextCapabilities()

    def get_provider_session_id(self):
        return "session"

    def register_admission_callback(self, callback, planning_cycle_key=""):
        self.callback = callback
        self.key = planning_cycle_key

    async def admit(self):
        await self.callback(ProviderAdmissionEvent(
            planning_cycle_key=self.key,
            provider_session_id="session",
            provider_turn_id="provider-turn",
            admitted_at=datetime.now(timezone.utc),
        ))


@pytest.mark.asyncio
async def test_rate_limit_retry_is_bounded_and_requeues(tmp_path):
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    await store.store_receipt(
        {
            "envelope_id": "message",
            "envelope_kind": "channel",
            "sender_slug": "human",
            "space_id": "space",
            "channel_id": "channel",
            "content_type": "text/plain",
            "content": "hello",
            "sent_at": 1,
        },
        server_seq=1,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    adapter = Adapter()

    class Runner:
        retries = 0

        async def __call__(self, _planned):
            await adapter.admit()
            raise AgentAPIError("rate limited")

        async def handle_global_inbox_retry(self, _planned):
            self.retries += 1
            raise AgentAPIError("rate limited")

    runner = Runner()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=runner,
        workspace=tmp_path,
        max_api_retries=2,
        retry_sleep=lambda _delay: asyncio.sleep(0),
    )
    await runtime.process_once()
    assert runner.retries == 2
    assert [row.envelope_id for row in await store.get_pending()] == ["message"]
    assert runtime.health.state == "degraded"
    await store.close()


@pytest.mark.asyncio
async def test_drained_turn_parks_instead_of_retrying(tmp_path):
    """Hold-no-retry for a spent quota: one drained provider attempt must
    stay one across degraded-recovery wakes and inbound notifies — the
    pre-park behavior retried every backoff window against the same spent
    account while the operator DM claimed messages were being held. The
    unpark signal is the drained check clearing (usage-snapshot driven)
    followed by a wake."""
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    await store.store_receipt(
        {
            "envelope_id": "message",
            "envelope_kind": "channel",
            "sender_slug": "human",
            "space_id": "space",
            "channel_id": "channel",
            "content_type": "text/plain",
            "content": "hello",
            "sent_at": 1,
        },
        server_seq=1,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="test",
    )
    adapter = Adapter()
    health = {"drained": True}

    class Runner:
        calls = 0

        async def __call__(self, _planned):
            await adapter.admit()
            self.calls += 1
            raise AgentAPIError("usage limit reached", is_drained=True)

    runner = Runner()
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=runner,
        workspace=tmp_path,
        max_api_retries=2,
        retry_sleep=lambda _delay: asyncio.sleep(0),
        drained_check=lambda: health["drained"],
    )

    await runtime.process_once()
    assert runner.calls == 1
    assert runtime.health.state == "degraded"
    assert "parked" in runtime.health.diagnostic

    # A degraded-recovery/coalescer wake must not retry the spent account.
    await runtime.process_once()
    # Neither must a new inbound message: notify() clears the degraded
    # backoff but deliberately not the drained park.
    runtime.notify()
    await runtime.process_once()
    assert runner.calls == 1
    # The durable row is retained the whole time, not dropped.
    assert [row.envelope_id for row in await store.get_pending()] == ["message"]

    # Quota returns (usage snapshot cleared the worker's health): the
    # next wake unparks and the provider is attempted again.
    health["drained"] = False
    await runtime.process_once()
    assert runner.calls == 2
    await store.close()
