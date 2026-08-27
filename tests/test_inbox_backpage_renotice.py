"""Partial Inbox reads must not silence the unread back pages.

A notice mentions every pending row, and claiming it records all of them as
delivered to the session. If the model reads only the first page, the rows it
never admitted must become notice-eligible again once the turn completes —
in the same provider session, without waiting for new arrivals.
"""

import re

import pytest

from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime

from test_global_inbox_runtime import Adapter, make_store, receipt


class _PartialReader:
    """Read one message per turn and cover it, leaving the rest unread."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.runtime = None
        self.reads = []

    async def __call__(self, planned):
        await self.adapter.admit()
        page = await self.runtime.read_inbox(limit=1, tool_arguments={"limit": 1})
        ids = []
        for block in page["messages"]:
            ids.extend(re.findall(r'message_id="([^"]+)"', block))
        self.reads.append(ids)
        if ids:
            await self.runtime.store.add_message_covers(
                tuple(ids), source="send", by_envelope_id=f"reply-{len(self.reads)}",
            )


@pytest.mark.asyncio
async def test_unread_back_pages_renotice_in_same_session(tmp_path):
    store = await make_store(tmp_path)
    for i in range(3):
        await receipt(
            store, f"m{i}", i + 1,
            content={"text": f"q{i}", "sender_type": "human"},
        )
    adapter = Adapter()
    runner = _PartialReader(adapter)
    runtime = GlobalInboxRuntime(
        store=store, adapter=adapter, run_turn=runner, workspace=tmp_path,
    )
    runner.runtime = runtime

    # Each turn reads one page; the unread remainder must wake the same
    # session again until the backlog drains.
    for expected_backlog in (2, 1, 0):
        assert await runtime.process_once()
        pending = [item.envelope_id for item in await store.get_pending()]
        assert len(pending) == expected_backlog
    assert runner.reads == [["m0"], ["m1"], ["m2"]]
    assert not await runtime.process_once()
    await store.close()
