"""Driver text deltas must arrive under the key their consumer reads.

Each driver's own tests pin the payload it emits, and the projector's tests pin
what it does with a canonical payload — but nothing tied the two together, so
three drivers shipped ``assistant_delta`` under a key the projector never reads
and every plain-text answer was dropped on the way to the Profile Log.
"""

from __future__ import annotations

import pytest

from puffo_agent.agent.harness.driver import (
    HarnessEvent,
    HarnessEventType,
    SessionRef,
    TurnRef,
)
from puffo_agent.agent.harness.drivers.opencode_protocol import (
    normalize_opencode_frame,
)
from puffo_agent.agent.harness.drivers.pi_protocol import normalize_pi_event
from puffo_agent.agent.harness.runtime import local_runtime

SESSION = SessionRef("logical")
TURN = TurnRef("turn-1")


def _pi_text_events() -> list[HarnessEvent]:
    events: list[HarnessEvent] = []
    for delta in (
        {"type": "text_delta", "contentIndex": 0, "delta": "hello"},
        {"type": "text_end", "contentIndex": 0},
    ):
        events.extend(
            normalize_pi_event(
                {"type": "message_update", "assistantMessageEvent": delta},
                session_ref=SESSION,
                turn_ref=TURN,
            )
        )
    return events


def _opencode_text_events() -> list[HarnessEvent]:
    return list(
        normalize_opencode_frame(
            {
                "type": "text",
                "sessionID": "ses_123",
                "part": {"id": "prt_text", "type": "text", "text": "hello"},
            },
            session_ref=SESSION,
            turn_ref=TURN,
        )
    )


@pytest.mark.parametrize(
    "build_events",
    [_pi_text_events, _opencode_text_events],
    ids=["pi", "opencode"],
)
def test_driver_text_reaches_the_legacy_status_projector(
    build_events, monkeypatch,
):
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        local_runtime,
        "_emit_status",
        lambda agent_id, event, payload: emitted.append((event, payload)),
    )
    projector = local_runtime._LegacyStatusProjector("agent")
    projector.project(
        HarnessEvent.normalized(
            type=HarnessEventType.TURN_STARTED,
            driver="driver",
            session_ref=SESSION,
            turn_ref=TURN,
            data={},
        )
    )

    for event in build_events():
        projector.project(event)

    assert emitted == [("assistant_text", {"text": "hello"})]
