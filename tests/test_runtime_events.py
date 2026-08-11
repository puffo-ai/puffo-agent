from __future__ import annotations

import json
import logging
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import puffo_agent
from puffo_agent.agent.harness.driver import HarnessEvent, SessionRef, TurnRef
from puffo_agent.agent._logging import RUNTIME_EVENT_NAMES, log_runtime_event
from puffo_agent.agent.runtime_events import (
    LifecycleValidator,
    RuntimeEvent,
    RuntimeEventProjector,
    TrustedScope,
    RUNTIME_EVENT_TYPES,
    _SAFE_MESSAGES,
)


def event(type_: str, payload: dict, **kwargs) -> RuntimeEvent:
    return RuntimeEvent(
        agent_id="agent_1", session_ref="session_1", turn_ref="turn_1",
        type=type_, payload=payload, **kwargs,
    )


def test_schema_has_exact_v1_envelope_and_metadata_only_types():
    assert RUNTIME_EVENT_TYPES == {
        "turn.started", "activity.updated", "tool.updated", "permission.updated",
        "turn.finished",
    }
    value = event("turn.started", {}).as_dict()
    assert set(value) == {
        "version", "event_id", "agent_id", "session_ref", "turn_ref",
        "scope", "type", "occurred_at", "payload",
    }
    for invalid in ("native.frame", "reasoning.updated", "session.started"):
        with pytest.raises(ValueError):
            event(invalid, {})
    assert value["scope"] == {"kind": "operator"}


def test_runtime_event_v1_scope_is_operator_only():
    assert TrustedScope().as_dict() == {"kind": "operator"}
    assert TrustedScope(kind="operator").as_dict() == {"kind": "operator"}
    for context_refs in (None, [], ["ctx_1"], ["ctx_1", "ctx_2"]):
        assert TrustedScope.from_runtime_context(context_refs).as_dict() == {
            "kind": "operator",
        }
    for kwargs in (
        {"kind": "context"},
        {"kind": "context", "context_ref": "ctx_1"},
        {"kind": "unknown"},
        {"kind": "operator", "context_ref": "ctx_1"},
    ):
        with pytest.raises(ValueError, match="must be operator"):
            TrustedScope(**kwargs)


def test_lifecycle_accepts_metadata_without_output_blocks():
    validator = LifecycleValidator()
    validator.accept(event("turn.started", {}))
    validator.accept(event("activity.updated", {"text": "Working"}))
    validator.accept(event("turn.finished", {"outcome": "succeeded"}))
    assert validator.active_turn_ref is None


@pytest.mark.parametrize(
    "outcome", ["succeeded", "failed", "cancelled", "abandoned"]
)
def test_lifecycle_accepts_exact_closed_terminal_outcomes(outcome):
    validator = LifecycleValidator()
    validator.accept(event("turn.started", {}))
    validator.accept(event("turn.finished", {"outcome": outcome}))
    assert validator.active_turn_ref is None


def test_schema_rejects_open_enum_values_and_extra_payload_fields():
    invalid = [
        ("tool.updated", {
            "tool_call_ref": "tool", "label": "Tool", "state": "waiting",
        }),
        ("permission.updated", {
            "permission_ref": "perm", "title": "Permission", "state": "maybe",
        }),
        ("turn.finished", {"outcome": "timed_out"}),
        ("output.updated", {
            "block_id": "out", "kind": "reasoning", "phase": "start",
        }),
        ("output.updated", {
            "block_id": "out", "kind": "result", "phase": "start",
            "tool_input": "must-not-fit-the-schema",
        }),
    ]
    for type_, payload in invalid:
        with pytest.raises(ValueError):
            event(type_, payload)


def test_runtime_event_is_immutable_after_id_assignment():
    value = event("turn.finished", {
        "outcome": "failed",
        "error": {
            "code": "unknown", "message": _SAFE_MESSAGES["unknown"], "retryable": False,
        },
    }, event_id="evt_fixed")
    with pytest.raises(FrozenInstanceError):
        value.event_id = "changed"
    with pytest.raises(TypeError):
        value.payload["outcome"] = "succeeded"
    with pytest.raises(TypeError):
        value.payload["error"]["message"] = "changed"
    assert value.as_dict()["event_id"] == "evt_fixed"


def test_assistant_output_is_not_projected_to_remote_events():
    secret = "sk-secret-native-tool-input"
    projector = RuntimeEventProjector(
        agent_id="agent_1", session_ref="session_1",
        scope=TrustedScope.from_runtime_context(None),
    )
    native = HarnessEvent.normalized(
        type="turn.assistant_delta", driver="codex",
        session_ref=SessionRef("native"), turn_ref=TurnRef("turn_1"),
        data={"text": "visible"}, native_payload={
            "reasoning": secret, "tool_input": secret, "scope": {
                "kind": "context", "context_ref": "model_chosen",
            },
        },
    )
    public = projector.project(native)
    assert public is None
    assert TrustedScope.from_runtime_context(["ctx_1"]).as_dict() == {
        "kind": "operator",
    }
    assert TrustedScope.from_runtime_context(["a", "b"]).as_dict() == {
        "kind": "operator",
    }


def test_projection_is_provider_neutral_and_uses_only_logical_references():
    def projected(driver):
        ids = iter([
            "evt_start", "evt_done",
        ])
        projector = RuntimeEventProjector(
            agent_id="agent", session_ref="logical_session",
            id_factory=lambda: next(ids),
        )
        native_session = f"{driver}-native-session"
        native_turn = f"{driver}-native-turn"
        source = [
            HarnessEvent.normalized(
                type="turn.started", driver=driver,
                session_ref=SessionRef(native_session),
                turn_ref=TurnRef("logical_turn"),
                native_session_id=native_session, native_turn_id=native_turn,
                native_payload={"provider_turn_id": native_turn},
            ),
            HarnessEvent.normalized(
                type="turn.assistant_delta", driver=driver,
                session_ref=SessionRef(native_session),
                turn_ref=TurnRef("logical_turn"),
                native_session_id=native_session, native_turn_id=native_turn,
                data={"text": "hello", "block_id": "provider-block"},
            ),
            HarnessEvent.normalized(
                type="turn.assistant_completed", driver=driver,
                session_ref=SessionRef(native_session),
                turn_ref=TurnRef("logical_turn"),
                data={"block_id": "provider-block"},
            ),
            HarnessEvent.normalized(
                type="turn.completed", driver=driver,
                session_ref=SessionRef(native_session),
                turn_ref=TurnRef("logical_turn"),
                data={"outcome": "succeeded"},
            ),
        ]
        result = []
        for item in source:
            result.extend(projector.project_all(item))
        return result, native_session, native_turn

    codex, codex_session, codex_turn = projected("codex")
    claude, claude_session, claude_turn = projected("claude-code")
    def comparable(values):
        result = []
        for value in values:
            payload = value.as_dict()["payload"]
            result.append(
                (value.type, payload, value.session_ref, value.turn_ref)
            )
        return result
    assert comparable(codex) == comparable(claude)
    encoded = json.dumps([value.as_dict() for value in codex + claude])
    for native in (codex_session, codex_turn, claude_session, claude_turn):
        assert native not in encoded


def test_privacy_redacts_every_non_allowlisted_native_or_model_field():
    secrets = [
        "chain-of-thought-secret", "raw-frame-secret", "tool-input-secret",
        "tool-result-secret", "credential-sk-123456789012345",
        "ciphertext-secret", "unrelated-message-secret",
    ]
    projector = RuntimeEventProjector(
        agent_id="agent", session_ref="logical-session"
    )
    public = projector.project(HarnessEvent.normalized(
        type="turn.assistant_delta", driver="codex",
        session_ref=SessionRef("native-session"),
        turn_ref=TurnRef("logical-turn"),
        data={
            "text": "safe visible text", "block_id": "result",
            "reasoning": secrets[0], "tool_input": secrets[2],
            "tool_result": secrets[3], "credential": secrets[4],
            "ciphertext": secrets[5], "message": secrets[6],
            "scope": {"kind": "context", "context_ref": "model-choice"},
        },
        native_payload={"raw": secrets[1], "authorization": secrets[4]},
    ))
    assert public is None


def test_started_projection_is_fixed_ordered_lifecycle_pair():
    projector = RuntimeEventProjector(
        agent_id="agent", session_ref="logical", id_factory=lambda: "evt_start",
    )
    source = HarnessEvent.normalized(
        type="turn.started", driver="codex",
        session_ref=SessionRef("provider-secret"), turn_ref=TurnRef("turn_1"),
        data={"credential": "secret", "reasoning": "secret"},
        native_payload={"native": "secret"},
    )
    projected = projector.project_all(source)
    assert [(value.type, value.as_dict()["payload"]) for value in projected] == [
        ("turn.started", {}), ("activity.updated", {"text": "Working"}),
    ]
    assert projected[0].event_id == "evt_start"
    assert projected[0].event_id != projected[1].event_id


@pytest.mark.parametrize("code,message", _SAFE_MESSAGES.items())
@pytest.mark.parametrize("retryable", [True, False])
def test_finished_error_accepts_only_exact_safe_pairs(code, message, retryable):
    event("turn.finished", {"outcome": "failed", "error": {
        "code": code, "message": message, "retryable": retryable,
    }})
    event("turn.finished", {"outcome": "failed"})


@pytest.mark.parametrize(
    "code,message",
    [
        (code, message)
        for code in _SAFE_MESSAGES
        for message in _SAFE_MESSAGES.values()
        if message != _SAFE_MESSAGES[code]
    ],
)
def test_finished_error_rejects_all_off_diagonal_safe_messages(code, message):
    with pytest.raises(ValueError):
        event("turn.finished", {"outcome": "failed", "error": {
            "code": code, "message": message, "retryable": False,
        }})


@pytest.mark.parametrize("code", _SAFE_MESSAGES)
def test_finished_error_rejects_noncanonical_message_for_each_code(code):
    with pytest.raises(ValueError):
        event("turn.finished", {"outcome": "failed", "error": {
            "code": code, "message": f"privacy-sentinel-{code}",
            "retryable": False,
        }})


@pytest.mark.parametrize("error", [None, "error", []])
def test_finished_error_rejects_non_objects(error):
    with pytest.raises(ValueError):
        event("turn.finished", {"outcome": "failed", "error": error})


@pytest.mark.parametrize("error", [
    {"message": _SAFE_MESSAGES["unknown"], "retryable": False},
    {"code": "unknown", "retryable": False},
    {"code": "unknown", "message": _SAFE_MESSAGES["unknown"]},
    {"code": "unknown", "message": _SAFE_MESSAGES["unknown"],
     "retryable": False, "extra": "nope"},
    {"code": "seventh_code", "message": "not a safe message", "retryable": False},
])
def test_finished_error_rejects_missing_extra_and_unknown_fields(error):
    with pytest.raises(ValueError):
        event("turn.finished", {"outcome": "failed", "error": error})


@pytest.mark.parametrize("retryable", [0, 1, "false", None])
def test_finished_error_rejects_non_boolean_retryable(retryable):
    with pytest.raises(ValueError):
        event("turn.finished", {"outcome": "failed", "error": {
            "code": "unknown", "message": _SAFE_MESSAGES["unknown"],
            "retryable": retryable,
        }})


def test_finished_error_rejects_nonfailed_outcomes():
    for outcome in ("succeeded", "cancelled", "abandoned"):
        with pytest.raises(ValueError):
            event("turn.finished", {"outcome": outcome, "error": {
                "code": "unknown", "message": _SAFE_MESSAGES["unknown"], "retryable": False,
            }})


def test_log_command_normalization_and_projection_boundaries_are_closed(caplog):
    logger = logging.getLogger("test.runtime.boundaries")
    caplog.set_level(logging.INFO, logger=logger.name)
    secret = "sk-command-payload-secret-123456"
    log_runtime_event(
        logger, "runtime.command",
        agent_id="agent", session_ref="session", turn_ref="turn",
        permission_ref="perm", capability="resolve_permission",
        capability_decision="delivered",
        # A non-allowlisted content-bearing field must be discarded.
        command_payload=secret,
    )
    log_runtime_event(
        logger, "runtime.normalized_event",
        agent_id="agent", session_ref="session", turn_ref="turn",
        event_type="turn.assistant_delta",
    )
    log_runtime_event(
        logger, "runtime.projected",
        agent_id="agent", session_ref="session", turn_ref="turn",
        event_id="evt", event_type="tool.updated",
    )
    parsed = [
        json.loads(record.getMessage().split("runtime_event=", 1)[1])
        for record in caplog.records if "runtime_event=" in record.getMessage()
    ]
    assert [record["event"] for record in parsed] == [
        "runtime.command", "runtime.normalized_event", "runtime.projected",
    ]
    command = parsed[0]
    assert command["agent_id"] == "agent"
    assert command["capability_decision"] == "delivered"
    assert "command_payload" not in command
    assert secret not in "\n".join(
        record.getMessage() for record in caplog.records
    )


_LOGGED_EVENT_NAME = re.compile(
    r'(?:log_runtime_event\(\s*[^,()]+,\s*|_log\(\s*)"([a-z_]+\.[a-z_]+)"'
)


def test_every_logged_runtime_event_name_is_supported():
    """An unlisted name is dropped silently, so evidence disappears unseen.

    ``runtime.discarded`` is the record that a permanently rejected event left
    the outbox; it and every other literal emission site must be admitted by
    the allowlist rather than swallowed by the unsupported-event guard.
    """
    assert "runtime.discarded" in RUNTIME_EVENT_NAMES

    source_root = Path(puffo_agent.__file__).parent
    emitted: dict[str, str] = {}
    for module in sorted(source_root.rglob("*.py")):
        for match in _LOGGED_EVENT_NAME.finditer(module.read_text()):
            emitted.setdefault(match.group(1), module.name)

    assert emitted, "no runtime event emission sites were found to check"
    unsupported = {
        name: module for name, module in emitted.items()
        if name not in RUNTIME_EVENT_NAMES
    }
    assert unsupported == {}


def test_assistant_blocks_are_omitted_and_terminal_metadata_is_pruned():
    projector = RuntimeEventProjector(agent_id="agent_1", session_ref="s_1")
    validator = LifecycleValidator()

    def normalized(type_, **data):
        return HarnessEvent.normalized(
            type=type_, driver="claude-code", session_ref=SessionRef("s_1"),
            turn_ref=TurnRef("turn_1"), data=data,
        )

    source = [normalized("turn.started")]
    for block in ("block-a", "block-b"):
        source.append(normalized(
            "turn.assistant_delta", text="hi", block_id=block
        ))
        source.append(normalized("turn.assistant_completed", block_id=block))
        source.append(normalized("turn.tool_started", tool_call_ref=block))
    source.append(normalized("turn.completed", outcome="succeeded"))

    projected_types = []
    for item in source:
        for projected in projector.project_all(item):
            validator.accept(projected)
            projected_types.append(projected.type)

    assert "output.updated" not in projected_types
    assert projected_types == [
        "turn.started", "activity.updated", "tool.updated", "tool.updated",
        "turn.finished",
    ]
    assert validator.active_turn_ref is None
    assert projector._tool_refs == {}
    # The turn is still remembered as terminal, so a late event is rejected.
    assert "turn_1" in validator._finished
