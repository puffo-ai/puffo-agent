"""Tests for ``ClaudeSession`` stream-recovery and the refresh-ping
auth probe. Stream tests drive a fake proc that shapes stdout bytes
exactly; the auth-probe test mocks
``asyncio.create_subprocess_exec`` in the local_cli adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from puffo_agent.agent.adapters.cli_session import (
    MAX_USER_MESSAGE_BYTES,
    REQUEST_TOO_LARGE_FRIENDLY,
    STREAM_READER_LIMIT_BYTES,
    AuditLog,
    ClaudeSession,
    _looks_like_poisoned_session,
    _looks_like_request_too_large,
)
from puffo_agent.agent.adapters.base import TurnResult


# ── Fake subprocess helpers ──────────────────────────────────────────────────


class _FakeStdin:
    def __init__(self, on_write=None):
        self.buffer = bytearray()
        self._closed = False
        self._on_write = on_write

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)
        if self._on_write is not None:
            self._on_write()

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True


class _RaisingReader:
    """Stand-in for ``proc.stdout`` that raises on ``readline``,
    driving the overflow-recovery path without a real >16 MiB line.
    """
    def __init__(self, exc: BaseException):
        self._exc = exc

    async def readline(self) -> bytes:
        raise self._exc


class _FakeProc:
    """Stand-in for ``asyncio.subprocess.Process``. Construct inside
    an async helper since StreamReader requires a running loop.

    ``pre_turn_lines`` are fed at construction (buffered before the
    turn frame); ``stdout_lines`` are fed on the first ``stdin.write``,
    mirroring that Claude Code only emits after receiving the turn.
    """
    def __init__(
        self,
        stdout_lines: list[bytes] | None = None,
        stdout_raises: BaseException | None = None,
        returncode: int = 0,
        pre_turn_lines: list[bytes] | None = None,
    ):
        if stdout_raises is not None:
            self.stdin = _FakeStdin()
            self.stdout = _RaisingReader(stdout_raises)
        else:
            reader = asyncio.StreamReader(limit=STREAM_READER_LIMIT_BYTES)
            for line in pre_turn_lines or []:
                reader.feed_data(line)
            self._turn_lines = list(stdout_lines or [])
            self._fed_turn = False

            def _feed_turn() -> None:
                if self._fed_turn:
                    return
                self._fed_turn = True
                for line in self._turn_lines:
                    reader.feed_data(line)
                reader.feed_eof()

            self.stdin = _FakeStdin(on_write=_feed_turn)
            self.stdout = reader
        empty = asyncio.StreamReader()
        empty.feed_eof()
        self.stderr = empty
        self.returncode: int | None = None
        self._final_rc = returncode
        self._terminated = False
        self._killed = False

    async def wait(self) -> int:
        self.returncode = self._final_rc
        return self._final_rc

    def terminate(self) -> None:
        self._terminated = True

    def kill(self) -> None:
        self._killed = True


class _ContextUsageProc:
    def __init__(self, context_response: dict):
        self.stdout = asyncio.StreamReader(limit=STREAM_READER_LIMIT_BYTES)
        self.frames: list[dict] = []
        self.context_response = context_response
        self.stdin = _FakeStdin(on_write=self._on_write)
        self.returncode = None

    def _on_write(self) -> None:
        frame = json.loads(self.stdin.buffer.decode("utf-8").splitlines()[-1])
        self.frames.append(frame)
        if frame["type"] == "user":
            event = {
                "type": "result",
                "subtype": "success",
                "session_id": "sess-1",
                "usage": {
                    "input_tokens": 3,
                    "cache_read_input_tokens": 7,
                    "output_tokens": 2,
                },
            }
        else:
            event = {
                "type": "control_response",
                "response": {
                    "request_id": frame["request_id"],
                    **self.context_response,
                },
            }
        self.stdout.feed_data((json.dumps(event) + "\n").encode())


def _make_session(tmp_path: Path, audit: bool = True) -> ClaudeSession:
    """Build a ClaudeSession pointed at tmp_path. ``build_command``
    is unused — tests inject ``_proc`` directly.
    """
    session_file = tmp_path / "session.json"
    audit_log = AuditLog(tmp_path / "audit.log", agent_id="test-agent") if audit else None
    return ClaudeSession(
        agent_id="test-agent",
        session_file=session_file,
        build_command=lambda args: ["true"],
        cwd=str(tmp_path),
        env={},
        audit=audit_log,
    )


def _read_audit_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def test_audit_log_summarizes_sensitive_turn_and_tool_payloads(tmp_path):
    secret = "sensitive-payload-that-must-never-be-logged"
    path = tmp_path / "audit.log"
    audit = AuditLog(path, agent_id="agent")

    audit.write("turn.input", content=secret)
    audit.write("assistant.text", text=secret)
    audit.write(
        "tool",
        id="call-1",
        name="mcp__puffo__send_message",
        input={"text": secret, "token": secret},
    )
    raw = path.read_text(encoding="utf-8")
    assert secret not in raw

    events = _read_audit_events(path)
    assert events[0]["content_summary"]["length"] == len(secret)
    assert events[1]["text_summary"]["length"] == len(secret)
    assert events[2]["id"] == "call-1"
    assert events[2]["input_summary"]["fields"] == ["text", "token"]
    assert "sha256" not in events[2]["input_summary"]


def test_audit_log_rotates_with_bounded_backup_count(tmp_path):
    path = tmp_path / "audit.log"
    audit = AuditLog(path, agent_id="agent", max_bytes=128, backup_count=2)
    for index in range(12):
        audit.write("turn.end", attempt=index, diagnostic="x" * 80)

    assert path.exists()
    assert path.with_name("audit.log.1").exists()
    assert path.with_name("audit.log.2").exists()
    assert not path.with_name("audit.log.3").exists()


def test_audit_log_write_pipeline_is_fully_best_effort(tmp_path, monkeypatch):
    path = tmp_path / "audit.log"
    audit = AuditLog(path, agent_id="agent")

    import puffo_agent.agent.adapters.cli_session as cli_module

    monkeypatch.setattr(
        cli_module,
        "safe_value_summary",
        lambda _value: (_ for _ in ()).throw(RuntimeError("summary")),
    )
    audit.write("turn.input", content="secret")
    monkeypatch.undo()

    monkeypatch.setattr(
        cli_module.json,
        "dumps",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError("serialize")),
    )
    audit.write("turn.end", attempt=1)
    monkeypatch.undo()

    monkeypatch.setattr(
        audit,
        "_rotate_if_needed",
        lambda: (_ for _ in ()).throw(OSError("rotate")),
    )
    audit.write("turn.end", attempt=2)
    monkeypatch.undo()

    audit.path = tmp_path
    monkeypatch.setattr(
        cli_module.logger,
        "warning",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("logger")),
    )
    audit.write("turn.end", attempt=3)


def test_claude_stderr_logs_only_safe_summary(tmp_path, caplog):
    secret = "provider stderr includes bearer secret-message-body"
    session = _make_session(tmp_path, audit=False)

    async def drive():
        reader = asyncio.StreamReader()
        reader.feed_data((secret + "\n").encode())
        reader.feed_eof()
        await session._drain_stderr(type("Proc", (), {"stderr": reader})())

    caplog.set_level(logging.WARNING)
    asyncio.run(drive())
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert secret not in joined
    assert "category=authentication" in joined
    assert "sha256=" not in joined


# ── Test 1: big line ─────────────────────────────────────────────────────────


def test_one_turn_reads_line_larger_than_default_asyncio_limit(tmp_path):
    """Stream-json result events larger than asyncio's 64 KiB default
    line buffer must read successfully. Regression guard.
    """
    # ~200 KiB assistant text — over the 64 KiB default, well under
    # the 16 MiB limit.
    big_text = "x" * (200 * 1024)
    assistant = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": big_text}]},
    }
    result = {
        "type": "result",
        "subtype": "success",
        "session_id": "sess-1",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    lines = [
        (json.dumps(assistant) + "\n").encode("utf-8"),
        (json.dumps(result) + "\n").encode("utf-8"),
    ]
    session = _make_session(tmp_path, audit=False)

    async def drive():
        session._proc = _FakeProc(stdout_lines=lines)
        return await session._one_turn("hello")

    out = asyncio.run(drive())
    assert out.reply == big_text
    assert out.input_tokens == 10
    assert out.output_tokens == 20


def test_input_tokens_include_cache_creation(tmp_path):
    """``input_tokens`` alone is just the uncached delta (often single digits);
    the recorded figure adds newly-cached input, excluding the cache read."""
    result = {
        "type": "result",
        "subtype": "success",
        "session_id": "sess-1",
        "usage": {
            "input_tokens": 3,
            "cache_creation_input_tokens": 331,
            "cache_read_input_tokens": 142928,
            "output_tokens": 26,
        },
    }
    lines = [(json.dumps(result) + "\n").encode("utf-8")]
    session = _make_session(tmp_path, audit=False)

    async def drive():
        session._proc = _FakeProc(stdout_lines=lines)
        return await session._one_turn("hi")

    out = asyncio.run(drive())
    assert out.input_tokens == 3 + 331  # cache read (142928) excluded
    assert out.metadata["context_tokens"] == 3 + 331 + 142928
    assert out.output_tokens == 26
    snap = asyncio.run(session.get_context_snapshot())
    assert snap.used_tokens == 3 + 331 + 142928
    assert snap.context_window == 200_000
    assert "fallback" in snap.source


def test_admission_waits_for_first_valid_post_write_provider_event(tmp_path):
    result = {
        "type": "result",
        "subtype": "success",
        "session_id": "sess-admitted",
        "usage": {"input_tokens": 1},
    }
    lines = [
        b"not json\n",
        b"[1, 2, 3]\n",
        (json.dumps(result) + "\n").encode(),
    ]
    session = _make_session(tmp_path, audit=False)
    events = []

    async def admitted(event):
        assert session._proc.stdin.buffer
        events.append(event)

    async def drive():
        session.register_admission_callback(admitted, "cycle-a")
        session._proc = _FakeProc(
            pre_turn_lines=[b'{"type":"stale"}\n'],
            stdout_lines=lines,
        )
        return await session._one_turn("hello")

    out = asyncio.run(drive())
    assert out.input_tokens == 1
    assert len(events) == 1
    assert events[0].planning_cycle_key == "cycle-a"
    assert events[0].provider_session_id == "sess-admitted"


def test_held_continuations_match_tool_results_in_same_claude_turn(tmp_path):
    lines = [
        b'{"type":"system","subtype":"init","session_id":"sess-held"}\n',
        b'{"type":"system","subtype":"status","session_id":"sess-held"}\n',
        (
            b'{"type":"assistant","session_id":"sess-held","message":{"content":'
            b'[{"type":"tool_use","id":"send-1","name":"mcp__puffo__send_message",'
            b'"input":{"channel":"ch-a","text":"a"}},'
            b'{"type":"tool_use","id":"send-2","name":"mcp__puffo__send_message",'
            b'"input":{"channel":"ch-b","text":"b"}}]}}\n'
        ),
        (
            b'{"type":"user","session_id":"sess-held","message":{"content":'
            b'[{"type":"tool_result","tool_use_id":"unrelated","content":"ok"}]}}\n'
        ),
        (
            b'{"type":"user","session_id":"sess-held","message":{"content":'
            b'[{"type":"tool_result","tool_use_id":"send-2","content":"held-b"}]}}\n'
        ),
        (
            b'{"type":"user","session_id":"sess-held","message":{"content":'
            b'[{"type":"tool_result","tool_use_id":"send-1","content":"held-a"}]}}\n'
        ),
        (
            b'{"type":"result","subtype":"success","session_id":"sess-held",'
            b'"usage":{"input_tokens":1}}\n'
        ),
    ]
    session = _make_session(tmp_path, audit=False)
    admitted = []

    async def continuation_a(event):
        admitted.append(("continuation-a", event))

    async def continuation_b(event):
        admitted.append(("continuation-b", event))

    async def initial(event):
        admitted.append(("initial", event))
        session.register_continuation_callback(
            continuation_a, "held-continuation-a", channel_id="ch-a",
        )
        session.register_continuation_callback(
            continuation_b, "held-continuation-b", channel_id="ch-b",
        )

    async def drive():
        session.register_admission_callback(initial, "initial")
        session._proc = _FakeProc(stdout_lines=lines)
        return await session._one_turn("hello")

    asyncio.run(drive())
    assert [kind for kind, _event in admitted] == [
        "initial", "continuation-b", "continuation-a",
    ]
    initial_event = admitted[0][1]
    continuation_b_event = admitted[1][1]
    continuation_a_event = admitted[2][1]
    assert initial_event.planning_cycle_key == "initial"
    assert continuation_b_event.planning_cycle_key == "held-continuation-b"
    assert continuation_a_event.planning_cycle_key == "held-continuation-a"
    assert {
        initial_event.provider_turn_id,
        continuation_a_event.provider_turn_id,
        continuation_b_event.provider_turn_id,
    } == {initial_event.provider_turn_id}
    assert initial_event.provider_turn_id.startswith("claude-turn-")


def _history_tool_result(tool_use_id, receipt, *, is_error=False):
    return {
        "type": "user",
        "session_id": "sess-history",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": is_error,
            "content": f"history\n[puffo:model-visible-read:{receipt}]",
        }]},
    }


def _history_tool_uses():
    return {
        "type": "assistant",
        "session_id": "sess-history",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "mcp__puffo__get_channel_history",
                    "input": arguments,
                }
                for call_id, arguments in (
                    ("read-a", {"channel": "ch-a"}),
                    ("read-b", {"channel": "ch-a"}),
                    ("read-b-wrong-arguments", {"channel": "ch-other"}),
                    ("read-failed", {"channel": "ch-a"}),
                    ("read-clamped", {"channel": "ch-a", "limit": 200}),
                )
            ],
        },
    }


def _history_provider_lines():
    encode = lambda event: (json.dumps(event) + "\n").encode()
    wrong_tool = {
        "type": "assistant",
        "session_id": "sess-history",
        "message": {"content": [{
            "type": "tool_use",
            "id": "wrong-tool",
            "name": "mcp__puffo__get_post",
            "input": {"post_ref": "ch-a"},
        }]},
    }
    return [
        encode({"type": "system", "subtype": "init", "session_id": "sess-history"}),
        encode(_history_tool_uses()),
        encode(wrong_tool),
        encode(_history_tool_result("wrong-tool", "receipt-b")),
        # A matching receipt alone must not consume the continuation: this
        # otherwise-valid history call has a different semantic target.
        encode(_history_tool_result("read-b-wrong-arguments", "receipt-b")),
        encode(_history_tool_result("read-b", "receipt-b")),
        encode(_history_tool_result("read-failed", "receipt-failed", is_error=True)),
        encode(_history_tool_result("read-clamped", "receipt-clamped")),
        encode(_history_tool_result("read-a", "receipt-a")),
        encode({
            "type": "result",
            "subtype": "success",
            "session_id": "sess-history",
            "usage": {"input_tokens": 1},
        }),
    ]


def _history_admission_callback(admitted, label):
    async def callback(event):
        admitted.append((label, event))

    return callback


async def _register_history_continuations(session, admitted, event):
    admitted.append(("initial", event))
    cases = (
        ("a", "receipt-a", {"channel": "ch-a"}),
        ("b", "receipt-b", {"channel": "ch-a"}),
        ("failed", "receipt-failed", {"channel": "ch-a"}),
        ("clamped", "receipt-clamped", {"channel": "ch-a", "limit": 999}),
    )
    for label, receipt, arguments in cases:
        session.register_continuation_callback(
            _history_admission_callback(admitted, label),
            f"visible-read-{label}",
            tool_names=("get_channel_history",),
            tool_arguments=arguments,
            correlation_receipt=receipt,
        )


def _assert_history_continuations(session, admitted):
    assert [kind for kind, _event in admitted] == [
        "initial", "b", "clamped", "a",
    ]
    assert {event.provider_turn_id for _, event in admitted} == {
        admitted[0][1].provider_turn_id,
    }
    assert [event.tool_call_id for _, event in admitted[1:]] == [
        "read-b", "read-clamped", "read-a",
    ]
    assert session._continuation_admissions == []
    with pytest.raises(RuntimeError, match="no active Claude provider turn"):
        session.register_continuation_callback(
            lambda _event: None,
            "late-visible-read",
            tool_names=("get_channel_history",),
            tool_arguments={"channel": "ch-a"},
            correlation_receipt="receipt-late",
        )


def test_history_continuation_matches_exact_claude_tool_result(tmp_path):
    session = _make_session(tmp_path, audit=False)
    admitted = []

    async def drive():
        async def initial(event):
            await _register_history_continuations(session, admitted, event)

        session.register_admission_callback(initial, "initial")
        session._proc = _FakeProc(stdout_lines=_history_provider_lines())
        return await session._one_turn("hello")

    asyncio.run(drive())
    _assert_history_continuations(session, admitted)


def test_claude_receipt_continuation_rejects_wrong_arguments_before_exact_result(
    tmp_path,
):
    session = _make_session(tmp_path, audit=False)
    admitted = []

    async def callback(event):
        admitted.append(event.tool_call_id)

    def result(tool_use_id):
        return {
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "history\n[puffo:model-visible-read:receipt-a]",
            }]},
        }

    async def drive():
        provider_turn_id = "claude-turn-held"
        session._active_provider_turn_id = provider_turn_id
        session.register_continuation_callback(
            callback,
            "held-a",
            tool_names=("get_channel_history",),
            tool_arguments={"channel": "ch-a"},
            correlation_receipt="receipt-a",
        )
        session._active_puffo_tool_calls.update({
            "wrong": ("get_channel_history", {"channel": "ch-other"}),
            "exact": ("get_channel_history", {"channel": "ch-a"}),
        })
        await session._fire_matching_continuations(result("wrong"), provider_turn_id)
        assert admitted == []
        assert len(session._continuation_admissions) == 1
        await session._fire_matching_continuations(result("exact"), provider_turn_id)

    asyncio.run(drive())
    assert admitted == ["exact"]


def test_claude_turn_end_discards_old_history_continuation(tmp_path):
    session = _make_session(tmp_path, audit=False)
    admitted = []

    def encode(event):
        return (json.dumps(event) + "\n").encode()

    async def old_initial(_event):
        async def old_continuation(_admission):
            admitted.append("old")

        session.register_continuation_callback(
            old_continuation,
            "old",
            tool_names=("get_channel_history",),
            tool_arguments={"channel": "ch-a"},
            correlation_receipt="old-receipt",
        )

    async def new_initial(_event):
        async def new_continuation(_admission):
            admitted.append("new")

        session.register_continuation_callback(
            new_continuation,
            "new",
            tool_names=("get_channel_history",),
            tool_arguments={"channel": "ch-a"},
            correlation_receipt="new-receipt",
        )

    async def drive():
        session.register_admission_callback(old_initial, "first")
        session._proc = _FakeProc(stdout_lines=[
            encode({"type": "system", "session_id": "sess-history"}),
            encode({
                "type": "result",
                "subtype": "success",
                "session_id": "sess-history",
            }),
        ])
        await session._one_turn("first")
        assert session._continuation_admissions == []

        session.register_admission_callback(new_initial, "second")
        session._proc = _FakeProc(stdout_lines=[
            encode({"type": "system", "session_id": "sess-history"}),
                encode({
                    "type": "assistant",
                    "session_id": "sess-history",
                    "message": {"content": [
                        {
                            "type": "tool_use",
                            "id": "stale-call",
                            "name": "mcp__puffo__get_channel_history",
                            "input": {"channel": "ch-a"},
                        },
                        {
                            "type": "tool_use",
                            "id": "new-call",
                            "name": "mcp__puffo__get_channel_history",
                            "input": {"channel": "ch-a"},
                        },
                    ]},
                }),
                encode({
                    "type": "user",
                    "session_id": "sess-history",
                    "message": {"content": [{
                        "type": "tool_result",
                        "tool_use_id": "stale-call",
                        "content": (
                            "[puffo:model-visible-read:old-receipt]"
                        ),
                }]},
            }),
            encode({
                "type": "user",
                "session_id": "sess-history",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "new-call",
                    "content": (
                        "[puffo:model-visible-read:new-receipt]"
                    ),
                }]},
            }),
            encode({
                "type": "result",
                "subtype": "success",
                "session_id": "sess-history",
            }),
        ])
        await session._one_turn("second")

    asyncio.run(drive())
    assert admitted == ["new"]


def test_compact_unsupported_without_text_frame_and_rollover_clears(tmp_path):
    session = _make_session(tmp_path, audit=False)
    session._save_session_id("sess-roll")
    session._context_used_tokens = 123
    captured_proc = None

    class LiveFakeProc(_FakeProc):
        def __init__(self):
            super().__init__()
            self._wait_calls = 0

        async def wait(self):
            self._wait_calls += 1
            if not self._terminated:
                raise asyncio.TimeoutError
            self.returncode = 0
            return 0

        def terminate(self):
            super().terminate()

    async def install_live_proc():
        nonlocal captured_proc
        captured_proc = LiveFakeProc()
        session._proc = captured_proc

    asyncio.run(install_live_proc())

    compact = asyncio.run(session.compact_context())
    assert compact.completed is False
    assert session._proc is captured_proc
    assert bytes(captured_proc.stdin.buffer) == b""

    rolled = asyncio.run(session.rollover_context())
    assert rolled.completed is True
    assert rolled.previous_provider_session_id == "sess-roll"
    assert session.get_provider_session_id() is None
    assert asyncio.run(session.get_context_snapshot()).used_tokens == 0
    assert not session.session_file.exists()
    assert captured_proc._terminated is True
    assert bytes(captured_proc.stdin.buffer) == b""
    assert b"/compact" not in captured_proc.stdin.buffer


def test_valid_stream_event_admits_before_later_stream_failure(tmp_path):
    session = _make_session(tmp_path, audit=False)
    events = []

    async def admitted(event):
        events.append(event)

    class ValidThenFailure:
        def __init__(self):
            self.triggered = False
            self.emitted = False

        async def readline(self):
            if not self.triggered:
                await asyncio.Future()
            if not self.emitted:
                self.emitted = True
                return b'{"type":"assistant","message":{"content":[]}}\n'
            raise ConnectionResetError("later failure")

    async def drive():
        session.register_admission_callback(admitted, "failed-after-admit")
        proc = _FakeProc()
        reader = ValidThenFailure()
        proc.stdout = reader
        proc.stdin = _FakeStdin(on_write=lambda: setattr(reader, "triggered", True))
        session._proc = proc
        result = await session._one_turn("hello")
        return result, proc

    result, proc = asyncio.run(drive())
    assert result.metadata["stream_error"] == "readline_pipe"
    assert len(events) == 1
    assert events[0].planning_cycle_key == "failed-after-admit"
    submitted = bytes(proc.stdin.buffer)
    assert b'"type": "user"' in submitted
    assert b"/compact" not in submitted


def test_context_usage_control_response_overrides_result_estimate(tmp_path):
    session = _make_session(tmp_path, audit=False)

    async def drive():
        proc = _ContextUsageProc({
            "subtype": "success",
            "response": {
                "totalTokens": 142_000,
                "rawMaxTokens": 1_000_000,
                "autoCompactThreshold": 967_000,
            },
        })
        session._proc = proc
        return await session._one_turn("hi"), proc.frames

    out, frames = asyncio.run(drive())
    assert [frame["type"] for frame in frames] == ["user", "control_request"]
    assert frames[1]["request"] == {"subtype": "get_context_usage"}
    assert out.metadata["context_tokens"] == 142_000
    assert session.context_limits() == (1_000_000, 967_000)


def test_context_usage_invalid_total_keeps_result_estimate(tmp_path):
    session = _make_session(tmp_path, audit=False)

    async def drive():
        proc = _ContextUsageProc({
            "subtype": "success",
            "response": {
                "totalTokens": "unknown",
                "rawMaxTokens": 1_000_000,
            },
        })
        session._proc = proc
        return await session._one_turn("hi")

    out = asyncio.run(drive())
    assert out.metadata["context_tokens"] == 10
    assert session.context_limits() == (1_000_000, None)


def test_context_usage_error_keeps_result_fallback_and_disables_retry(tmp_path):
    session = _make_session(tmp_path, audit=False)

    async def drive():
        proc = _ContextUsageProc({
            "subtype": "error",
            "error": "unsupported request",
        })
        session._proc = proc
        result = await session._one_turn("hi")
        assert await session._refresh_context_usage() is None
        return result, proc.frames

    out, frames = asyncio.run(drive())
    assert out.metadata["context_tokens"] == 10
    assert session.context_limits() == (None, None)
    assert [frame["type"] for frame in frames] == ["user", "control_request"]


def test_context_usage_timeout_remains_retryable(tmp_path, monkeypatch):
    session = _make_session(tmp_path, audit=False)

    async def timeout(*_args):
        raise asyncio.TimeoutError

    async def drive():
        session._proc = _ContextUsageProc({})
        monkeypatch.setattr(session, "_read_context_usage_response", timeout)
        assert await session._refresh_context_usage() is None
        assert await session._refresh_context_usage() is None

    asyncio.run(drive())
    assert session._context_usage_supported is not False


def test_turn_ignores_late_context_control_response(tmp_path, monkeypatch):
    late_response = {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "ctx_timed_out",
            "response": {"totalTokens": 42},
        },
    }
    result = {
        "type": "result",
        "subtype": "success",
        "session_id": "sess-1",
        "result": "still works",
        "usage": {},
    }
    lines = [
        (json.dumps(late_response) + "\n").encode(),
        (json.dumps(result) + "\n").encode(),
    ]
    session = _make_session(tmp_path, audit=False)

    async def no_refresh():
        return None

    async def drive():
        session._proc = _FakeProc(stdout_lines=lines)
        monkeypatch.setattr(session, "_refresh_context_usage", no_refresh)
        return await session._one_turn("hi")

    assert asyncio.run(drive()).reply == "still works"


def test_context_usage_reader_ignores_unrelated_events(tmp_path):
    session = _make_session(tmp_path, audit=False)

    async def drive():
        reader = asyncio.StreamReader(limit=STREAM_READER_LIMIT_BYTES)
        events = [
            b"not-json\n",
            json.dumps({
                "type": "system",
                "subtype": "status",
                "session_id": "sess-2",
            }).encode() + b"\n",
            json.dumps({
                "type": "system",
                "subtype": "status",
                "session_id": "sess-2",
            }).encode() + b"\n",
            json.dumps({"type": "assistant", "message": {}}).encode() + b"\n",
            json.dumps({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "stale",
                    "response": {},
                },
            }).encode() + b"\n",
            json.dumps({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "wanted",
                    "response": {"rawMaxTokens": 200_000},
                },
            }).encode() + b"\n",
        ]
        for event in events:
            reader.feed_data(event)
        proc = type("Proc", (), {"stdout": reader})()
        return await session._read_context_usage_response(proc, "wanted")

    assert asyncio.run(drive()) == {"rawMaxTokens": 200_000}
    assert session._session_id == "sess-2"


@pytest.mark.parametrize(
    "response",
    [
        "invalid",
        {"subtype": "success", "request_id": "wanted", "response": []},
    ],
)
def test_context_usage_reader_rejects_invalid_response(tmp_path, response):
    session = _make_session(tmp_path, audit=False)

    async def drive():
        reader = asyncio.StreamReader(limit=STREAM_READER_LIMIT_BYTES)
        reader.feed_data(json.dumps({
            "type": "control_response",
            "response": response,
        }).encode() + b"\n")
        proc = type("Proc", (), {"stdout": reader})()
        await session._read_context_usage_response(proc, "wanted")

    with pytest.raises(RuntimeError, match="invalid"):
        asyncio.run(drive())


def test_context_usage_reader_rejects_closed_stream(tmp_path):
    session = _make_session(tmp_path, audit=False)

    async def drive():
        reader = asyncio.StreamReader()
        reader.feed_eof()
        proc = type("Proc", (), {"stdout": reader})()
        await session._read_context_usage_response(proc, "wanted")

    with pytest.raises(ConnectionError, match="stream closed"):
        asyncio.run(drive())


def test_warm_refreshes_context_usage_under_session_lock(tmp_path, monkeypatch):
    session = _make_session(tmp_path, audit=False)
    calls = []

    async def ensure(system_prompt):
        calls.append(("ensure", system_prompt))

    async def refresh():
        calls.append(("refresh", None))

    monkeypatch.setattr(session, "_ensure_running", ensure)
    monkeypatch.setattr(session, "_refresh_context_usage", refresh)

    asyncio.run(session.warm("system"))
    assert calls == [("ensure", "system"), ("refresh", None)]


@pytest.mark.parametrize("value", [None, 0, -1, True, 1.5, "200000"])
def test_context_limits_reject_invalid_sdk_values(tmp_path, value):
    session = _make_session(tmp_path, audit=False)
    session._context_usage = {"rawMaxTokens": value}
    assert session.context_limits() == (None, None)


# ── Test 2: stream overflow recovery ─────────────────────────────────────────


def test_one_turn_recovers_on_readline_overflow(tmp_path, caplog):
    """Stream overflow / protocol corruption surfaced as ValueError:
    turn must return an empty reply, audit the stream_error, and kill
    the subprocess so the next turn respawns.
    """
    overflow = ValueError("Separator is not found, and chunk exceed the limit")
    session = _make_session(tmp_path)

    async def drive():
        session._proc = _FakeProc(stdout_raises=overflow, returncode=137)
        return await session._one_turn("hello")

    with caplog.at_level(logging.ERROR):
        out = asyncio.run(drive())

    # Empty reply so the worker doesn't post anything user-visible;
    # metadata flag for the worker to surface.
    assert out.reply == ""
    assert out.metadata.get("stream_error") == "readline_limit"
    assert out.input_tokens == 0

    # _kill_proc sets self._proc to None.
    assert session._proc is None

    events = _read_audit_events(tmp_path / "audit.log")
    stream_errors = [e for e in events if e.get("event") == "session.stream_error"]
    assert len(stream_errors) == 1
    assert stream_errors[0]["phase"] == "readline_limit"
    assert stream_errors[0]["action"] == "respawned_claude_subprocess"

    # ERROR-level so operators see it without tailing DEBUG.
    assert any(
        "stream failure" in r.message and r.levelno == logging.ERROR
        for r in caplog.records
    ), "expected an ERROR log from _handle_stream_failure"


def test_one_turn_recovers_on_eof_mid_turn(tmp_path):
    """Subprocess dies mid-turn (stdout EOF before a result event).
    Same recovery contract as readline-overflow.
    """
    # No lines fed -> first readline() returns b'' (EOF).
    session = _make_session(tmp_path)

    async def drive():
        session._proc = _FakeProc(stdout_lines=[], returncode=1)
        return await session._one_turn("hello")

    out = asyncio.run(drive())
    assert out.reply == ""
    assert out.metadata.get("stream_error") == "eof_mid_turn"
    assert session._proc is None

    events = _read_audit_events(tmp_path / "audit.log")
    assert any(
        e.get("event") == "session.stream_error"
        and e.get("phase") == "eof_mid_turn"
        for e in events
    )


# ── Test 4: poisoned-session recovery ────────────────────────────────────────


def test_looks_like_poisoned_session_matches_image_dimension_error():
    """Verbatim API error strings match; ordinary chat does not."""
    assert _looks_like_poisoned_session(
        "API Error: An image in the conversation exceeds the dimension "
        "limit for many-image requests (2000px). Start a new session "
        "with fewer images."
    )
    # Case-insensitive.
    assert _looks_like_poisoned_session("START A NEW SESSION WITH FEWER IMAGES")
    # No false positive on normal replies.
    assert not _looks_like_poisoned_session("Sure, here's the image you asked about.")
    assert not _looks_like_poisoned_session("")


def test_one_turn_recovers_from_poisoned_session(tmp_path, caplog):
    """A reply carrying the oversized-image API error must: return an
    empty reply (so the channel sees nothing), flag
    ``poisoned_session`` in metadata, CLEAR the persisted session id
    (so the next spawn is fresh, not ``--resume`` onto the same
    poisoned transcript), kill the subprocess, and audit the reset.
    """
    poison = (
        "API Error: An image in the conversation exceeds the dimension "
        "limit for many-image requests (2000px). Start a new session "
        "with fewer images."
    )
    assistant = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": poison}]},
    }
    result = {
        "type": "result",
        "subtype": "success",
        "session_id": "sess-poisoned",
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    lines = [
        (json.dumps(assistant) + "\n").encode("utf-8"),
        (json.dumps(result) + "\n").encode("utf-8"),
    ]
    session = _make_session(tmp_path)
    # Pretend a prior turn persisted a session id — that's what makes
    # the next spawn try --resume onto the poisoned transcript.
    session._save_session_id("sess-poisoned")
    assert session.session_file.exists()

    async def drive():
        session._proc = _FakeProc(stdout_lines=lines)
        return await session._one_turn("look at this picture")

    with caplog.at_level(logging.ERROR):
        out = asyncio.run(drive())

    # Empty reply — the raw API error must not post as a bot message.
    assert out.reply == ""
    assert out.metadata.get("poisoned_session") is True
    # Session id cleared on disk AND in memory — next spawn is fresh.
    assert not session.session_file.exists()
    assert session._session_id == ""
    # Subprocess killed so the next turn respawns.
    assert session._proc is None

    events = _read_audit_events(tmp_path / "audit.log")
    poisoned = [e for e in events if e.get("event") == "session.poisoned"]
    assert len(poisoned) == 1
    assert poisoned[0]["action"] == "cleared_session_id_and_respawned_fresh"

    assert any(
        "poisoned" in r.message and r.levelno == logging.ERROR
        for r in caplog.records
    ), "expected an ERROR log naming the poisoned session"


def test_poison_recovery_reruns_the_turn_on_a_fresh_session(tmp_path):
    """The triggering message must NOT be dropped. After ``_one_turn``
    detects the poison + clears the session, ``_one_turn_with_poison_
    recovery`` re-ensures running (a fresh spawn) and re-sends the
    SAME message — which succeeds on the clean transcript. Without
    this rerun the message is lost forever when it's the only inbound
    one (no later turn ever pages it back in)."""
    poison_lines = [
        (
            json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "text",
                    "text": (
                        "An image in the conversation exceeds the "
                        "dimension limit for many-image requests (2000px)."
                    ),
                }]},
            }) + "\n"
        ).encode("utf-8"),
        (
            json.dumps({
                "type": "result", "subtype": "success",
                "session_id": "sess-poison",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }) + "\n"
        ).encode("utf-8"),
    ]
    clean_lines = [
        (
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "processed your message fine"}
                ]},
            }) + "\n"
        ).encode("utf-8"),
        (
            json.dumps({
                "type": "result", "subtype": "success",
                "session_id": "sess-fresh",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            }) + "\n"
        ).encode("utf-8"),
    ]
    session = _make_session(tmp_path)
    session._save_session_id("sess-poison")

    async def drive():
        # Turn 1: the poisoned proc.
        session._proc = _FakeProc(stdout_lines=poison_lines)
        # _ensure_running is what would respawn — patch it to inject
        # the fresh (clean) proc, modelling a no-session-id spawn.
        clean_proc = _FakeProc(stdout_lines=clean_lines)

        async def fake_ensure(_system_prompt):
            session._proc = clean_proc

        with patch.object(session, "_ensure_running", side_effect=fake_ensure):
            return await session._one_turn_with_poison_recovery(
                "look at this picture", "sysprompt",
            )

    out = asyncio.run(drive())

    # The rerun succeeded — the message produced a real reply, not an
    # empty drop.
    assert out.reply == "processed your message fine"
    assert out.metadata.get("poisoned_session") is None
    assert out.input_tokens == 2
    # Poisoned id cleared by turn 1; the fresh turn learned a new one.
    assert session._session_id == "sess-fresh"


def test_one_turn_normal_reply_keeps_session(tmp_path):
    """A clean reply leaves the persisted session id intact — the
    recovery path must not fire on ordinary turns."""
    assistant = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "all good, here you go"}]},
    }
    result = {
        "type": "result",
        "subtype": "success",
        "session_id": "sess-healthy",
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    lines = [
        (json.dumps(assistant) + "\n").encode("utf-8"),
        (json.dumps(result) + "\n").encode("utf-8"),
    ]
    session = _make_session(tmp_path)
    session._save_session_id("sess-healthy")

    async def drive():
        session._proc = _FakeProc(stdout_lines=lines)
        return await session._one_turn("hello")

    out = asyncio.run(drive())

    assert out.reply == "all good, here you go"
    assert out.metadata.get("poisoned_session") is None
    assert session.session_file.exists()
    assert session._session_id == "sess-healthy"


# ── PUF-264: pre-send byte cap + reactive Prompt-is-too-long rewrite ────────


def test_one_turn_pre_send_check_short_circuits_oversized_user_message(tmp_path):
    session = _make_session(tmp_path, audit=True)

    async def drive():
        session._proc = _FakeProc(stdout_lines=[])
        big = "x" * (MAX_USER_MESSAGE_BYTES + 1)
        return await session._one_turn(big)

    out = asyncio.run(drive())
    assert out.reply == REQUEST_TOO_LARGE_FRIENDLY
    assert out.metadata.get("request_too_large") == "pre_send"
    assert out.metadata.get("user_message_bytes") == MAX_USER_MESSAGE_BYTES + 1
    assert session._proc.stdin.buffer == bytearray()
    events = _read_audit_events(tmp_path / "audit.log")
    assert any(e.get("event") == "turn.request_too_large_pre_send" for e in events)


def test_one_turn_pre_send_check_passes_messages_at_or_below_cap(tmp_path):
    result_evt = {
        "type": "result",
        "subtype": "success",
        "session_id": "sess-1",
        "usage": {"input_tokens": 5, "output_tokens": 5},
        "result": "fine",
    }
    session = _make_session(tmp_path, audit=False)

    async def drive():
        session._proc = _FakeProc(stdout_lines=[
            (json.dumps(result_evt) + "\n").encode("utf-8"),
        ])
        at_cap = "x" * MAX_USER_MESSAGE_BYTES
        return await session._one_turn(at_cap)

    out = asyncio.run(drive())
    assert out.metadata.get("request_too_large") is None
    assert session._proc.stdin.buffer


def test_rewrite_if_request_too_large_matches_canonical_anthropic_string(tmp_path):
    session = _make_session(tmp_path, audit=True)
    raw = "API Error: Prompt is too long\n\nRequest ID: req_011CtestabcDEF"
    result = TurnResult(
        reply=raw, input_tokens=42, output_tokens=0, tool_calls=3,
        metadata={"some_prior_flag": True},
    )
    out = session._rewrite_if_request_too_large(result)
    assert out.reply == REQUEST_TOO_LARGE_FRIENDLY
    assert out.metadata.get("request_too_large") == "reactive"
    assert out.metadata.get("original_reply") == raw
    assert out.metadata.get("some_prior_flag") is True
    assert out.input_tokens == 42
    assert out.tool_calls == 3


def test_rewrite_if_request_too_large_matches_max_tokens_context_limit(tmp_path):
    session = _make_session(tmp_path, audit=False)
    raw = "input length and `max_tokens` exceed context limit: 199000 + 4000 > 200000"
    out = session._rewrite_if_request_too_large(TurnResult(reply=raw))
    assert out.reply == REQUEST_TOO_LARGE_FRIENDLY
    assert out.metadata.get("request_too_large") == "reactive"


def test_rewrite_if_request_too_large_matches_size_error_attachment_form(tmp_path):
    session = _make_session(tmp_path, audit=False)
    for raw in (
        "size error: request too large, try with a smaller file",
        "size error： request too large，try with a smaller file",
    ):
        out = session._rewrite_if_request_too_large(TurnResult(reply=raw))
        assert out.reply == REQUEST_TOO_LARGE_FRIENDLY, raw
        assert out.metadata.get("request_too_large") == "reactive"


def test_rewrite_if_request_too_large_passes_through_normal_replies(tmp_path):
    session = _make_session(tmp_path, audit=False)
    cases = [
        "Sure, here's the answer to your question.",
        "I will write a long prompt to test edge cases.",
        "API Error: Request rejected (429)",
        "",
    ]
    for raw in cases:
        result = TurnResult(reply=raw)
        out = session._rewrite_if_request_too_large(result)
        assert out is result, f"regex over-matched on: {raw!r}"


def test_looks_like_request_too_large_predicate():
    assert _looks_like_request_too_large("API Error: Prompt is too long")
    assert _looks_like_request_too_large(
        "API Error: Prompt is too long\n\nRequest ID: req_011CtestabcDEF"
    )
    assert _looks_like_request_too_large(
        "input length and `max_tokens` exceed context limit: 199000 + 4000 > 200000"
    )
    assert _looks_like_request_too_large(
        "size error: request too large, try with a smaller file"
    )
    assert _looks_like_request_too_large(
        "size error： request too large，try with a smaller file"
    )
    assert _looks_like_request_too_large("api error: PROMPT IS TOO LONG")
    assert _looks_like_request_too_large("SIZE ERROR: REQUEST TOO LARGE, try later")
    assert not _looks_like_request_too_large("Prompt is too longer than usual")
    assert not _looks_like_request_too_large("API Error: Request rejected (429)")
    assert not _looks_like_request_too_large(
        "Your previous request too large for the queue was retried."
    )
    assert not _looks_like_request_too_large("")
    assert not _looks_like_request_too_large("Sure, here's the answer.")


def test_one_turn_pre_send_check_counts_utf8_bytes_not_chars(tmp_path):
    session = _make_session(tmp_path, audit=False)
    big_cjk = "好" * (MAX_USER_MESSAGE_BYTES // 3 + 1)
    assert len(big_cjk) < MAX_USER_MESSAGE_BYTES
    assert len(big_cjk.encode("utf-8")) > MAX_USER_MESSAGE_BYTES

    async def drive():
        session._proc = _FakeProc(stdout_lines=[])
        return await session._one_turn(big_cjk)

    out = asyncio.run(drive())
    assert out.reply == REQUEST_TOO_LARGE_FRIENDLY
    assert out.metadata.get("request_too_large") == "pre_send"


def test_one_turn_pre_send_check_does_not_emit_turn_input_audit(tmp_path):
    session = _make_session(tmp_path, audit=True)

    async def drive():
        session._proc = _FakeProc(stdout_lines=[])
        big = "x" * (MAX_USER_MESSAGE_BYTES + 1)
        return await session._one_turn(big)

    asyncio.run(drive())
    events = _read_audit_events(tmp_path / "audit.log")
    assert any(e.get("event") == "turn.request_too_large_pre_send" for e in events)
    assert not any(e.get("event") == "turn.input" for e in events)


def test_run_turn_rewrites_canonical_too_long_reply(tmp_path):
    too_long = "API Error: Prompt is too long\n\nRequest ID: req_011CtestabcDEF"
    lines = [
        (json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": too_long}]},
        }) + "\n").encode("utf-8"),
        (json.dumps({
            "type": "result", "subtype": "success",
            "session_id": "sess-too-long",
            "usage": {"input_tokens": 100, "output_tokens": 0},
        }) + "\n").encode("utf-8"),
    ]
    session = _make_session(tmp_path, audit=True)

    async def drive():
        async def fake_ensure(_system_prompt):
            session._proc = _FakeProc(stdout_lines=list(lines))
        with patch.object(session, "_ensure_running", side_effect=fake_ensure):
            return await session.run_turn("hello", "sysprompt")

    out = asyncio.run(drive())
    assert out.reply == REQUEST_TOO_LARGE_FRIENDLY
    assert out.metadata.get("request_too_large") == "reactive"
    assert out.metadata.get("original_reply") == too_long
    assert out.input_tokens == 100
    events = _read_audit_events(tmp_path / "audit.log")
    assert any(e.get("event") == "turn.request_too_large_reactive" for e in events)


def test_run_turn_oversized_message_returns_friendly_without_burning_retries(
    tmp_path, monkeypatch,
):
    session = _make_session(tmp_path, audit=True)
    sleep_calls: list[float] = []

    async def track_sleep(secs):
        sleep_calls.append(secs)
        return None
    monkeypatch.setattr(asyncio, "sleep", track_sleep)

    async def drive():
        async def fake_ensure(_system_prompt):
            session._proc = _FakeProc(stdout_lines=[])
        with patch.object(session, "_ensure_running", side_effect=fake_ensure):
            big = "x" * (MAX_USER_MESSAGE_BYTES + 1)
            return await session.run_turn(big, "sysprompt")

    out = asyncio.run(drive())
    assert out.reply == REQUEST_TOO_LARGE_FRIENDLY
    assert out.metadata.get("request_too_large") == "pre_send"
    assert sleep_calls == []


def test_run_turn_auth_error_takes_precedence_over_too_large_rewrite(
    tmp_path, monkeypatch,
):
    combined = "API Error: 401 Invalid API key. Prompt is too long."
    lines_template = [
        (json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": combined}]},
        }) + "\n").encode("utf-8"),
        (json.dumps({
            "type": "result", "subtype": "success",
            "session_id": "sess-x",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }) + "\n").encode("utf-8"),
    ]

    async def no_sleep(_secs):
        return None
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    session = _make_session(tmp_path, audit=False)

    async def drive():
        async def fake_ensure(_system_prompt):
            session._proc = _FakeProc(stdout_lines=list(lines_template))
        with patch.object(session, "_ensure_running", side_effect=fake_ensure):
            return await session.run_turn("hello", "sysprompt")

    out = asyncio.run(drive())
    assert out.reply == ""
    assert out.metadata.get("auth_failed") is True
    assert out.metadata.get("request_too_large") is None


# ── pre-turn stdout drain ────────────────────────────────────────────────────


def _assistant_line(text: str) -> bytes:
    return (json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }) + "\n").encode("utf-8")


def _result_line(session_id: str = "sess-1", result: str = "") -> bytes:
    evt = {
        "type": "result", "subtype": "success",
        "session_id": session_id,
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }
    if result:
        evt["result"] = result
    return (json.dumps(evt) + "\n").encode("utf-8")


def test_one_turn_drains_pre_turn_cron_stdout(tmp_path):
    """Claude Code internal-cron ``assistant`` output buffered before the
    turn must NOT be folded into this turn's reply — it's drained + audited,
    and only turn-generated text is returned."""
    session = _make_session(tmp_path, audit=True)

    async def drive():
        session._proc = _FakeProc(
            pre_turn_lines=[_assistant_line("News check complete. 3 new items.")],
            stdout_lines=[_assistant_line("here is your answer"), _result_line()],
        )
        return await session._one_turn("what's up")

    out = asyncio.run(drive())

    assert out.reply == "here is your answer"
    assert "News check complete" not in out.reply
    assert out.metadata["assistant_text_parts"] == ["here is your answer"]

    events = _read_audit_events(tmp_path / "audit.log")
    pre = [e for e in events if e.get("event") == "turn.pre_drain"]
    assert len(pre) == 1
    assert pre[0]["event_type"] == "assistant"
    assert "text" not in pre[0]
    assert pre[0]["text_summary"]["length"] == len(
        "News check complete. 3 new items."
    )


def test_drain_consumes_stale_result_so_next_turn_doesnt_break_early(tmp_path):
    """A stale ``result`` event left in the buffer must be drained too —
    otherwise the read loop would break on it immediately and return an
    empty reply for the real turn."""
    session = _make_session(tmp_path, audit=False)

    async def drive():
        session._proc = _FakeProc(
            pre_turn_lines=[_result_line(session_id="sess-stale")],
            stdout_lines=[_assistant_line("real answer"), _result_line("sess-live")],
        )
        return await session._one_turn("hi")

    out = asyncio.run(drive())
    assert out.reply == "real answer"


def test_no_pre_turn_stdout_is_a_clean_noop(tmp_path):
    """With nothing buffered pre-turn, the drain is a no-op and the turn
    behaves exactly as before (regression guard)."""
    session = _make_session(tmp_path, audit=True)

    async def drive():
        session._proc = _FakeProc(
            stdout_lines=[_assistant_line("all good"), _result_line()],
        )
        return await session._one_turn("hello")

    out = asyncio.run(drive())
    assert out.reply == "all good"
    events = _read_audit_events(tmp_path / "audit.log")
    assert not [e for e in events if e.get("event") == "turn.pre_drain"]


def test_drain_stops_on_eof_when_subprocess_died_pre_turn(tmp_path):
    """Stdout EOF during the drain (subprocess exited pre-turn) exits
    the drain cleanly; the main loop then reports eof_mid_turn."""
    session = _make_session(tmp_path, audit=False)

    async def drive():
        reader = asyncio.StreamReader(limit=STREAM_READER_LIMIT_BYTES)
        reader.feed_data(_assistant_line("last words"))
        reader.feed_eof()
        proc = _FakeProc(stdout_lines=[])
        proc.stdout = reader
        session._proc = proc
        return await session._one_turn("hi")

    out = asyncio.run(drive())
    assert out.metadata.get("stream_error") == "eof_mid_turn"
    assert out.reply == ""


def test_drain_swallows_readline_error_and_hands_off_to_main_loop(tmp_path):
    """Oversized event / dead pipe surfacing as LimitOverrunError during
    the drain: the drain exits best-effort; the main read loop's own
    handler then records the stream_error."""
    session = _make_session(tmp_path, audit=False)

    async def drive():
        session._proc = _FakeProc(
            stdout_raises=asyncio.LimitOverrunError("event too big", 0),
        )
        return await session._one_turn("hi")

    out = asyncio.run(drive())
    assert out.metadata.get("stream_error") == "readline_limit"
    assert out.reply == ""


# ── Session rotation on model change (PUF-159) ───────────────────────────────
#
# A transcript born under one model must not be --resume'd under another:
# cross-family replay injects the old model's thinking blocks into the new
# provider's API (a kimi-born session resumed under an Anthropic model makes
# claude-code send clear_thinking_20251015 without thinking enabled → 400 on
# every turn until the session rotates).


def _write_session_file(tmp_path: Path, sid: str, model: str | None) -> Path:
    data: dict = {"session_id": sid, "updated_at": 1}
    if model is not None:
        data["model"] = model
    f = tmp_path / "session.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def _make_model_session(tmp_path: Path, model: str) -> ClaudeSession:
    return ClaudeSession(
        agent_id="test-agent",
        session_file=tmp_path / "session.json",
        build_command=lambda args: ["true"],
        cwd=str(tmp_path),
        env={},
        audit=None,
        model=model,
    )


def test_session_rotates_when_model_changed(tmp_path, caplog):
    _write_session_file(tmp_path, "sess-kimi", "kimi-k3")
    with caplog.at_level(logging.INFO):
        session = _make_model_session(tmp_path, "claude-opus-4-7")
    assert session._session_id == ""  # fresh session, no --resume
    assert not (tmp_path / "session.json").exists()  # stale file dropped
    assert "rotating CLI session" in caplog.text


def test_session_kept_when_model_unchanged(tmp_path):
    _write_session_file(tmp_path, "sess-opus", "claude-opus-4-7")
    session = _make_model_session(tmp_path, "claude-opus-4-7")
    assert session._session_id == "sess-opus"
    assert (tmp_path / "session.json").exists()


def test_legacy_session_file_without_model_is_kept(tmp_path):
    # Pre-PUF-159 files carry no model — keep them (one gratuitous reset per
    # fleet on upgrade would be worse); the model lands on the next save.
    _write_session_file(tmp_path, "sess-legacy", None)
    session = _make_model_session(tmp_path, "claude-opus-4-7")
    assert session._session_id == "sess-legacy"


def test_session_kept_when_no_model_configured(tmp_path):
    # model="" (legacy caller) → scoping disabled entirely.
    _write_session_file(tmp_path, "sess-any", "kimi-k3")
    session = _make_model_session(tmp_path, "")
    assert session._session_id == "sess-any"


def test_save_records_model(tmp_path):
    session = _make_model_session(tmp_path, "claude-opus-4-7")
    session._save_session_id("sess-new")
    data = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))
    assert data["session_id"] == "sess-new"
    assert data["model"] == "claude-opus-4-7"


def test_save_omits_model_when_unset(tmp_path):
    session = _make_model_session(tmp_path, "")
    session._save_session_id("sess-new")
    data = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))
    assert "model" not in data
