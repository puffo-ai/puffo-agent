"""Driver text must survive the trip to both consumers, not just be emitted.

Each driver's own tests pin the payload it produces and the consumers' tests
pin what they do with a canonical payload, but nothing ran one into the other.
That gap let three drivers ship ``turn.assistant_delta`` under a key neither
consumer reads: the Profile Log lost every ``assistant_text`` record and
``TurnResult.reply`` came back empty, so an agent that answered in plain text
instead of calling a tool said nothing at all.

Each case below drives one harness's real emit path, then asserts the text
arrives whole at both consumers -- the legacy status projector and the runtime
manager's reply accumulation -- across several deltas.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from puffo_agent.agent.adapters.base import TurnContext
from puffo_agent.agent.harness.drivers.codex import CODEX_CAPABILITIES
from puffo_agent.agent.harness.driver import (
    Driver,
    HarnessEvent,
    HarnessEventType,
    RuntimeOpened,
    RuntimeRef,
    RuntimeSpec,
    SessionRef,
    TurnRef,
    TurnStarted,
    UnsupportedCapability,
)
from puffo_agent.agent.harness.drivers.opencode_protocol import (
    normalize_opencode_frame,
)
from puffo_agent.agent.harness.drivers.pi_protocol import normalize_pi_event
from puffo_agent.agent.runtime_event_outbox import RuntimeEventOutbox

TURN = TurnRef("driver-turn")
SESSION = SessionRef("native-session")
CHUNKS = ("QA-", "TEXT-", "OK")
ANSWER = "".join(CHUNKS)


def _pi_events() -> list[HarnessEvent]:
    """Real ``normalize_pi_event`` output for a multi-delta text block."""
    frames = [
        {"type": "message_update",
         "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0,
                                   "delta": chunk}}
        for chunk in CHUNKS
    ]
    frames.append({"type": "message_update",
                   "assistantMessageEvent": {"type": "text_end",
                                             "contentIndex": 0}})
    events: list[HarnessEvent] = []
    for frame in frames:
        events.extend(
            normalize_pi_event(frame, session_ref=SESSION, turn_ref=TURN)
        )
    return events


def _opencode_events() -> list[HarnessEvent]:
    """Real ``normalize_opencode_frame`` output; each frame carries the text
    accumulated so far, so the projector must not concatenate them blindly."""
    events: list[HarnessEvent] = []
    for chunk in CHUNKS:
        events.extend(
            normalize_opencode_frame(
                {"type": "text", "sessionID": "ses",
                 "part": {"id": "prt", "type": "text", "text": chunk}},
                session_ref=SESSION,
                turn_ref=TURN,
            )
        )
    return events


async def _acp_events() -> list[HarnessEvent]:
    """Real ``AcpDriver._session_update`` output, drained from its queue."""
    from acp.schema import AgentMessageChunk, TextContentBlock

    from puffo_agent.agent.harness.drivers.acp import AcpDriver

    driver = AcpDriver()
    driver._native_session_id = "acp-session"
    driver._session_ref = SESSION
    driver._active = TURN
    for chunk in CHUNKS:
        await driver._session_update(
            "acp-session",
            AgentMessageChunk(
                session_update="agent_message_chunk",
                message_id="message_1",
                content=TextContentBlock(type="text", text=chunk),
            ),
        )
    events: list[HarnessEvent] = []
    while not driver._events.empty():
        events.append(driver._events.get_nowait())
    return events


class _QueueDriver(Driver):
    """A driver whose event stream is whatever the test puts on the queue."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.turn = TURN

    async def open(self, spec, resume=None):
        return RuntimeOpened(
            RuntimeRef("runtime"), SESSION, "native-session", False,
            CODEX_CAPABILITIES, SimpleNamespace(),
        )

    async def start_turn(self, input):
        return TurnStarted(self.turn, "native-turn")

    async def steer_turn(self, turn, input):
        return UnsupportedCapability("steer")

    async def cancel_turn(self, turn):
        return UnsupportedCapability("cancel")

    async def context_status(self):
        return UnsupportedCapability("context_status")

    async def compact(self, request):
        return UnsupportedCapability("compact")

    async def resolve_permission(self, request, decision):
        return UnsupportedCapability("permission")

    def events(self):
        async def iterate():
            while True:
                yield await self.queue.get()
        return iterate()

    async def close(self):
        return None


def _lifecycle(type_: HarnessEventType, data: dict) -> HarnessEvent:
    return HarnessEvent.normalized(
        type=type_, driver="harness", session_ref=SESSION, turn_ref=TURN,
        native_session_id="native-session", native_turn_id="native-turn",
        data=data,
    )


def _build_adapter(tmp_path, driver, monkeypatch):
    import puffo_agent.agent.harness.runtime.local_runtime as local_runtime
    from puffo_agent.agent.harness.runtime.local_runtime import (
        PreparedLocalRuntime,
        build_local_runtime_adapter,
    )

    class _StubPreparer:
        agent_id = "agent"

    prepared = PreparedLocalRuntime(
        harness_name="codex",
        spec=RuntimeSpec(str(tmp_path)),
        native_session_id="",
        migration_source="fresh",
        legacy_session_path=tmp_path / "legacy.json",
        preparer=_StubPreparer(),
    )
    outbox = RuntimeEventOutbox(tmp_path / "runtime_events.db")
    monkeypatch.setattr(local_runtime, "build_driver", lambda name: driver)
    adapter = build_local_runtime_adapter(
        prepared, outbox=outbox, logical_session_ref="logical-session"
    )
    return adapter, outbox


def _reporter_emits(monkeypatch):
    emits: list[tuple[str, dict]] = []

    async def fake_emit(agent_slug, event, payload):
        emits.append((event, payload))

    import puffo_agent.portal.control.reporter as reporter_mod

    monkeypatch.setattr(
        reporter_mod, "get_reporter", lambda: SimpleNamespace(emit=fake_emit)
    )
    return emits


async def _run_turn(harness, tmp_path, monkeypatch):
    """Drive one harness's real events through a real adapter.

    Returns the routed reply and every ``assistant_text`` the Profile Log
    received, so each contract below can assert one thing.
    """
    if harness == "pi":
        driver_events = _pi_events()
    elif harness == "opencode":
        driver_events = _opencode_events()
    else:
        driver_events = await _acp_events()

    assert [
        event for event in driver_events
        if event.type is HarnessEventType.ASSISTANT_DELTA
    ], f"{harness} produced no assistant_delta to carry the answer"

    emits = _reporter_emits(monkeypatch)
    driver = _QueueDriver()
    adapter, outbox = _build_adapter(tmp_path, driver, monkeypatch)
    ctx = TurnContext(
        system_prompt="system",
        messages=[{"role": "user", "content": "reply in plain text"}],
        workspace_dir=str(tmp_path),
        claude_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
    )

    task = asyncio.create_task(adapter.run_turn(ctx))
    await _wait_until(lambda: bool(adapter.manager._turn_refs))
    # Drivers close their own blocks at different points (pi on text_end,
    # opencode per frame, acp at end of turn); close whatever is still open so
    # every harness reaches the projector's emit point the same way.
    closed = {
        event.data.get("block_id") for event in driver_events
        if event.type is HarnessEventType.ASSISTANT_COMPLETED
    }
    open_blocks = [
        block for block in dict.fromkeys(
            event.data.get("block_id") for event in driver_events
            if event.type is HarnessEventType.ASSISTANT_DELTA
        )
        if block not in closed
    ]
    for event in [
        _lifecycle(HarnessEventType.TURN_STARTED, {}),
        *driver_events,
        *(
            _lifecycle(HarnessEventType.ASSISTANT_COMPLETED,
                       {"block_id": block})
            for block in open_blocks
        ),
        _lifecycle(HarnessEventType.TURN_COMPLETED, {"outcome": "succeeded"}),
    ]:
        await driver.queue.put(event)
    result = await asyncio.wait_for(task, timeout=5)
    texts = [payload["text"] for event, payload in emits
             if event == "assistant_text"]

    await adapter.aclose()
    outbox.close()
    return result, texts


@pytest.mark.parametrize("harness", ["pi", "opencode", "acp"])
@pytest.mark.asyncio
async def test_driver_text_reaches_the_routed_reply(
    harness, tmp_path, monkeypatch,
):
    """``TurnResult.reply`` is what gets sent to the channel or DM; losing it
    means an agent that answers without calling a tool says nothing."""
    result, _ = await _run_turn(harness, tmp_path, monkeypatch)

    assert ANSWER in result.reply, (
        f"{harness}: the plain-text answer never reached TurnResult.reply"
    )


@pytest.mark.parametrize("harness", [
    "pi",
    pytest.param("opencode", marks=pytest.mark.xfail(
        strict=True,
        reason="opencode closes a block after every text fragment while "
               "reusing the part id, so the projector emits the first "
               "fragment and dedupes the rest -- tracked separately from the "
               "payload-key fix",
    )),
    "acp",
])
@pytest.mark.asyncio
async def test_driver_text_reaches_the_profile_log(
    harness, tmp_path, monkeypatch,
):
    """The web Profile Log renders these ``assistant_text`` records."""
    _, texts = await _run_turn(harness, tmp_path, monkeypatch)

    assert texts, f"{harness}: no assistant_text status was ever reported"
    assert ANSWER in "".join(texts), (
        f"{harness}: the Profile Log record lost part of the answer"
    )


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.001)
