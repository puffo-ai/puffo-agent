"""OpenCode JSONL conformance at the process boundary.

These tests pin only shapes emitted by ``opencode run --format json``.  They
guard the future Driver from treating a per-turn child like a stdin session,
leaking tool payloads, or declaring a provider step to be the whole turn.
"""

from puffo_agent.agent.harness.driver import (
    HarnessEventType,
    RuntimeSpec,
    SessionRef,
    TurnRef,
)
from puffo_agent.agent.harness.drivers.opencode_protocol import (
    build_opencode_run_command,
    normalize_opencode_frame,
)


def test_run_command_resumes_by_session_and_keeps_prompt_positional():
    spec = RuntimeSpec(
        workspace_dir="/workspace",
        model="openai/gpt-5",
        executable="/bin/opencode",
        launch_args=("--title", "Puffo"),
        permission_mode="bypassPermissions",
    )
    command = build_opencode_run_command(
        spec,
        prompt="one semantic input",
        native_session_id="ses_123",
    )

    assert command == (
        "/bin/opencode",
        "run",
        "--format",
        "json",
        "--dir",
        "/workspace",
        "--model",
        "openai/gpt-5",
        "--session",
        "ses_123",
        "--auto",
        "--title",
        "Puffo",
        "one semantic input",
    )


def test_run_command_forwards_model_variant_before_prompt():
    spec = RuntimeSpec(
        workspace_dir="/workspace",
        model="openai/gpt-5",
        executable="/bin/opencode",
        launch_args=("--variant", "high"),
    )

    command = build_opencode_run_command(spec, prompt="hello")

    assert command[-3:] == ("--variant", "high", "hello")


def test_text_frame_emits_one_completed_output_block():
    frame = {
        "type": "text",
        "sessionID": "ses_123",
        "part": {"id": "prt_text", "type": "text", "text": "hello"},
    }

    events = normalize_opencode_frame(
        frame,
        session_ref=SessionRef("logical"),
        turn_ref=TurnRef("turn-1"),
    )

    assert [event.type for event in events] == [
        HarnessEventType.ASSISTANT_DELTA,
        HarnessEventType.ASSISTANT_COMPLETED,
    ]
    assert events[0].data == {"block_id": "prt_text", "delta": "hello"}
    assert events[1].data == {"block_id": "prt_text"}
    assert all(event.native_session_id == "ses_123" for event in events)


def test_completed_tool_frame_keeps_arguments_and_result_native_only():
    frame = {
        "type": "tool_use",
        "sessionID": "ses_123",
        "part": {
            "id": "prt_tool",
            "type": "tool",
            "tool": "read_inbox",
            "state": {
                "status": "completed",
                "input": {"secret": "do-not-surface"},
                "output": "private result",
            },
        },
    }

    started, completed = normalize_opencode_frame(
        frame,
        session_ref=SessionRef("logical"),
        turn_ref=TurnRef("turn-1"),
    )

    assert started.type is HarnessEventType.TOOL_STARTED
    assert started.data == {
        "tool_call_ref": "prt_tool",
        "label": "read_inbox",
    }
    assert completed.type is HarnessEventType.TOOL_COMPLETED
    assert completed.data == {
        "tool_call_ref": "prt_tool",
        "label": "read_inbox",
        "outcome": "succeeded",
    }
    assert "private result" not in repr(started.data)
    assert "private result" not in repr(completed.data)
    assert started.native_diagnostic == frame
    assert completed.native_diagnostic == frame


def test_step_finish_updates_usage_but_is_not_turn_terminal():
    frame = {
        "type": "step_finish",
        "sessionID": "ses_123",
        "part": {
            "id": "prt_finish",
            "type": "step-finish",
            "tokens": {
                "input": 12,
                "output": 3,
                "reasoning": 2,
                "cache": {"read": 8, "write": 1},
                "total": 23,
            },
            "cost": 0.01,
        },
    }

    [event] = normalize_opencode_frame(
        frame,
        session_ref=SessionRef("logical"),
        turn_ref=TurnRef("turn-1"),
    )

    assert event.type is HarnessEventType.CONTEXT_UPDATED
    assert event.data == {
        "input_tokens": 12,
        "output_tokens": 3,
        "reasoning_tokens": 2,
        "cache_read_tokens": 8,
        "cache_write_tokens": 1,
        # tokens.total is the step's full context occupancy (input +
        # cache.read + output) — the number context tracking runs on.
        "total_tokens": 23,
    }
