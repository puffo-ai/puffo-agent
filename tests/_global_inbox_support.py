"""Shared fakes for Global Inbox runtime contract tests."""

from __future__ import annotations

from datetime import datetime, timezone

from puffo_agent.agent.context_controller import (
    ContextCapabilities,
    ContextSnapshot,
    ProviderAdmissionEvent,
)
from puffo_agent.agent.message_store import (
    MessageStore,
    ReceiptDisposition,
)


class Adapter:
    def __init__(self):
        self.callback = None
        self.key = ""
        self.session = "provider-1"
        self.inputs = []

    async def get_context_snapshot(self):
        return ContextSnapshot(0, 200_000, "test", datetime.now(timezone.utc))

    def get_context_capabilities(self):
        return ContextCapabilities()

    async def compact_context(self):
        raise AssertionError("not expected")

    async def rollover_context(self):
        raise AssertionError("not expected")

    def get_provider_session_id(self):
        return self.session

    def register_admission_callback(self, callback, planning_cycle_key=""):
        self.callback = callback
        self.key = planning_cycle_key

    def register_autonomous_callback(self, callback):
        self.autonomous_callback = callback
        return True

    async def admit(
        self,
        session: str | None = "provider-1",
        provider_turn_id: str = "provider-turn",
    ):
        callback, self.callback = self.callback, None
        assert callback is not None
        await callback(
            ProviderAdmissionEvent(
                planning_cycle_key=self.key,
                provider_session_id=session,
                provider_turn_id=provider_turn_id,
                admitted_at=datetime.now(timezone.utc),
            )
        )


class ToolReturnAdapter(Adapter):
    tool_result_admission_boundary = "tool_return"

    def register_continuation_callback(self, *_args, **_kwargs):
        raise AssertionError("tool-return admission must not await provider completion")


async def make_store(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    return store


async def receipt(
    store,
    envelope_id,
    seq,
    *,
    kind="channel",
    channel="ch-1",
    space="sp-1",
    sender="alice",
    disposition=ReceiptDisposition.ELIGIBLE,
    content=None,
    is_encrypted=True,
):
    return await store.store_receipt(
        {
            "envelope_id": envelope_id,
            "envelope_kind": kind,
            "sender_slug": sender,
            "recipient_slug": "agent" if kind == "dm" else None,
            "channel_id": channel if kind != "dm" else None,
            "space_id": space if kind != "dm" else None,
            "content": content if content is not None else f"text-{envelope_id}",
            "content_type": "text/plain",
            "sent_at": seq,
            "is_encrypted": is_encrypted,
        },
        server_seq=seq,
        disposition=disposition,
        reason="test",
    )
