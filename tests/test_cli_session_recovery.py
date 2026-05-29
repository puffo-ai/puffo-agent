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
    def __init__(self):
        self.buffer = bytearray()
        self._closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

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
    """
    def __init__(
        self,
        stdout_lines: list[bytes] | None = None,
        stdout_raises: BaseException | None = None,
        returncode: int = 0,
    ):
        self.stdin = _FakeStdin()
        if stdout_raises is not None:
            self.stdout = _RaisingReader(stdout_raises)
        else:
            reader = asyncio.StreamReader(limit=STREAM_READER_LIMIT_BYTES)
            for line in stdout_lines or []:
                reader.feed_data(line)
            reader.feed_eof()
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
    """user_message above MAX_USER_MESSAGE_BYTES must NOT spawn the
    claude subprocess. We assert no stdin write happened + the friendly
    reply landed + metadata flags the case so the worker can audit."""
    session = _make_session(tmp_path, audit=True)

    async def drive():
        # _FakeProc with no stdout — if we ever read it the test hangs,
        # which is the correct failure mode if the pre-send check
        # regresses.
        session._proc = _FakeProc(stdout_lines=[])
        big = "x" * (MAX_USER_MESSAGE_BYTES + 1)
        return await session._one_turn(big)

    out = asyncio.run(drive())
    assert out.reply == REQUEST_TOO_LARGE_FRIENDLY
    assert out.metadata.get("request_too_large") == "pre_send"
    assert out.metadata.get("user_message_bytes") == MAX_USER_MESSAGE_BYTES + 1
    # Crucially: no stdin write happened (subprocess wasn't fed).
    assert session._proc.stdin.buffer == bytearray()
    events = _read_audit_events(tmp_path / "audit.log")
    assert any(
        e.get("event") == "turn.request_too_large_pre_send"
        for e in events
    )


def test_one_turn_pre_send_check_passes_messages_at_or_below_cap(tmp_path):
    """A user_message exactly at the cap must NOT short-circuit — only
    strictly-over triggers. Pins the boundary."""
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
    # The subprocess was actually fed.
    assert session._proc.stdin.buffer  # non-empty


def test_rewrite_if_request_too_large_matches_canonical_anthropic_string(tmp_path):
    """The exact verbatim ``API Error: Prompt is too long`` message
    Claude Code surfaces when Anthropic returns the input-too-long
    error must be detected + replaced with the friendly version. Raw
    string preserved in metadata for operator forensics."""
    session = _make_session(tmp_path, audit=True)
    raw = (
        "API Error: Prompt is too long\n\n"
        "Request ID: req_011CtestabcDEF"
    )
    result = TurnResult(
        reply=raw, input_tokens=42, output_tokens=0, tool_calls=3,
        metadata={"some_prior_flag": True},
    )
    out = session._rewrite_if_request_too_large(result)
    assert out.reply == REQUEST_TOO_LARGE_FRIENDLY
    assert out.metadata.get("request_too_large") == "reactive"
    assert out.metadata.get("original_reply") == raw
    assert out.metadata.get("some_prior_flag") is True
    # Token counts + tool_calls preserved so cost accounting + metrics
    # still reflect the turn that actually happened (per operator
    # review on PR #53).
    assert out.input_tokens == 42
    assert out.tool_calls == 3


def test_rewrite_if_request_too_large_matches_max_tokens_context_limit(tmp_path):
    """Alternate Anthropic shape (parseable numeric form). Must also
    trigger the rewrite so an operator never sees the raw API error."""
    session = _make_session(tmp_path, audit=False)
    raw = "input length and `max_tokens` exceed context limit: 199000 + 4000 > 200000"
    result = TurnResult(reply=raw)
    out = session._rewrite_if_request_too_large(result)
    assert out.reply == REQUEST_TOO_LARGE_FRIENDLY
    assert out.metadata.get("request_too_large") == "reactive"


def test_rewrite_if_request_too_large_matches_size_error_attachment_form(tmp_path):
    """Third Anthropic surface: the file-attachment path emits
    ``size error: request too large, try with a smaller file``.
    Some localised CLI builds use fullwidth ``：`` / ``，`` — both
    must trigger the rewrite so the operator sees the friendly copy
    instead of the raw CLI surface."""
    session = _make_session(tmp_path, audit=False)
    for raw in (
        "size error: request too large, try with a smaller file",
        "size error： request too large，try with a smaller file",
    ):
        out = session._rewrite_if_request_too_large(TurnResult(reply=raw))
        assert out.reply == REQUEST_TOO_LARGE_FRIENDLY, raw
        assert out.metadata.get("request_too_large") == "reactive"


def test_rewrite_if_request_too_large_passes_through_normal_replies(tmp_path):
    """Regression guard: a normal reply or a turn that mentions
    'prompt' or 'long' in passing must NOT trigger the rewrite. The
    regex is anchored on the verbatim Anthropic strings."""
    session = _make_session(tmp_path, audit=False)
    cases = [
        "Sure, here's the answer to your question.",
        "I will write a long prompt to test edge cases.",
        "API Error: Request rejected (429)",  # rate-limit, NOT too-long
        "",
    ]
    for raw in cases:
        result = TurnResult(reply=raw)
        out = session._rewrite_if_request_too_large(result)
        assert out is result, f"regex over-matched on: {raw!r}"


def test_looks_like_request_too_large_predicate():
    """Source-of-truth test for the regex behavior. Mirrors
    ``test_looks_like_poisoned_session_matches_image_dimension_error``
    — the predicate is the single point a downstream caller relies on."""
    assert _looks_like_request_too_large("API Error: Prompt is too long")
    assert _looks_like_request_too_large(
        "API Error: Prompt is too long\n\nRequest ID: req_011CtestabcDEF"
    )
    assert _looks_like_request_too_large(
        "input length and `max_tokens` exceed context limit: 199000 + 4000 > 200000"
    )
    # Third surface: file-attachment path. Both ASCII and fullwidth
    # punctuation forms must match.
    assert _looks_like_request_too_large(
        "size error: request too large, try with a smaller file"
    )
    assert _looks_like_request_too_large(
        "size error： request too large，try with a smaller file"
    )
    # Case-insensitive (Anthropic's casing is stable but the binary
    # has surfaced it in mixed shapes historically).
    assert _looks_like_request_too_large("api error: PROMPT IS TOO LONG")
    assert _looks_like_request_too_large("SIZE ERROR: REQUEST TOO LARGE, try later")
    # Word boundary on "long" — "longer" / "longish" must not match.
    assert not _looks_like_request_too_large("Prompt is too longer than usual")
    # Different API error class — rate-limit shape must not over-match.
    assert not _looks_like_request_too_large("API Error: Request rejected (429)")
    # The phrase "request too large" without the size-error prefix
    # must NOT trigger — covers the assistant casually referencing the
    # error class in prose.
    assert not _looks_like_request_too_large(
        "Your previous request too large for the queue was retried."
    )
    # Empty / no marker.
    assert not _looks_like_request_too_large("")
    assert not _looks_like_request_too_large("Sure, here's the answer.")


def test_one_turn_pre_send_check_counts_utf8_bytes_not_chars(tmp_path):
    """3-byte UTF-8 chars (CJK, emoji etc.) hit the cap at ~60k chars,
    not 180k chars. Pins ``encode("utf-8")`` semantics so a future
    refactor can't silently swap it for ``len(str)`` and let attachment
    payloads through."""
    session = _make_session(tmp_path, audit=False)
    big_cjk = "好" * (MAX_USER_MESSAGE_BYTES // 3 + 1)
    # The two guards that motivate the byte-based check.
    assert len(big_cjk) < MAX_USER_MESSAGE_BYTES
    assert len(big_cjk.encode("utf-8")) > MAX_USER_MESSAGE_BYTES

    async def drive():
        session._proc = _FakeProc(stdout_lines=[])
        return await session._one_turn(big_cjk)

    out = asyncio.run(drive())
    assert out.reply == REQUEST_TOO_LARGE_FRIENDLY
    assert out.metadata.get("request_too_large") == "pre_send"


def test_one_turn_pre_send_check_does_not_emit_turn_input_audit(tmp_path):
    """A short-circuited pre-send must NOT write ``turn.input`` to the
    audit log — no turn actually reached claude, and a misleading
    ``turn.input`` entry would confuse operator forensics about whether
    the payload was actually sent."""
    session = _make_session(tmp_path, audit=True)

    async def drive():
        session._proc = _FakeProc(stdout_lines=[])
        big = "x" * (MAX_USER_MESSAGE_BYTES + 1)
        return await session._one_turn(big)

    asyncio.run(drive())
    events = _read_audit_events(tmp_path / "audit.log")
    assert any(e.get("event") == "turn.request_too_large_pre_send" for e in events)
    assert not any(e.get("event") == "turn.input" for e in events), (
        "pre-send short-circuit must skip turn.input audit (no turn happened)"
    )


def test_run_turn_rewrites_canonical_too_long_reply(tmp_path):
    """End-to-end wiring through ``run_turn``: a real claude reply
    carrying the verbatim Anthropic error must come out as the friendly
    string + ``request_too_large=reactive`` metadata. Pins the call to
    ``_rewrite_if_request_too_large`` at the non-auth-error return path
    — a refactor that drops the call would let the raw API error leak
    to the user without breaking the unit tests."""
    too_long = (
        "API Error: Prompt is too long\n\n"
        "Request ID: req_011CtestabcDEF"
    )
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
    # Token counts read from the result event survive the rewrite.
    assert out.input_tokens == 100
    events = _read_audit_events(tmp_path / "audit.log")
    assert any(
        e.get("event") == "turn.request_too_large_reactive"
        for e in events
    )


def test_run_turn_oversized_message_returns_friendly_without_burning_retries(
    tmp_path, monkeypatch,
):
    """End-to-end pre-send path through ``run_turn``: an oversized
    message must short-circuit on the FIRST attempt with the friendly
    reply + ``pre_send`` metadata, NOT bleed into the auth-retry budget
    (which would waste ~45s sleeping while the user already has a
    correct answer). Pins the cheap-failure contract for the operator
    side."""
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
    # No retry sleeps — the friendly reply isn't an auth error, so the
    # loop returns on the first attempt with zero backoff calls.
    assert sleep_calls == []


def test_run_turn_auth_error_takes_precedence_over_too_large_rewrite(
    tmp_path, monkeypatch,
):
    """A reply that matches BOTH ``_looks_like_auth_error`` AND
    ``_looks_like_request_too_large`` must take the auth-retry path
    (exhausts retries → empty reply + ``auth_failed=True``) rather than
    short-circuit through the too-large rewrite. Pins the order of the
    two checks at ``run_turn``'s return site — swapping them would let
    a real auth failure be silently rewritten to the user-facing
    friendly message."""
    # Real Anthropic surfaces don't combine these, but the wiring is
    # what's under test, not the surface.
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

    # Skip the AUTH_RETRY_BACKOFFS_SECONDS waits so the test takes ms
    # not ~45s.
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
    # Auth-error path won: empty reply (shell suppresses post) +
    # auth_failed metadata for the worker to flip runtime.health.
    assert out.reply == ""
    assert out.metadata.get("auth_failed") is True
    # If too-large rewrite had taken precedence the reply would be the
    # friendly string and request_too_large would be set.
    assert out.metadata.get("request_too_large") is None
