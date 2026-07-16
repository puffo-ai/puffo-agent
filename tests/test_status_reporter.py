"""Unit tests for puffo_agent.agent.status_reporter.StatusReporter.

These cover state-machine behaviour and best-effort error handling
by mocking at the ``PuffoCoreHttpClient.post`` level — the signed-HTTP
machinery is covered in test_http_client.py.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.status_reporter import StatusReporter
from puffo_agent.crypto.http_client import HttpError


class FakeHttp:
    """Captures every ``post(path, body)`` call so tests can assert the
    exact wire-shape sent. Tests set ``side_effect`` to simulate
    HttpError or unexpected exceptions.
    """

    def __init__(self, keyless: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.side_effect: BaseException | None = None
        # Mirrors ``PuffoCoreHttpClient.keyless``. Native agents (the
        # default) leave this False; keyless bridge agents set it True so
        # the reporter skips every signed status POST.
        self.keyless = keyless

    async def post(self, path: str, body: dict | None = None):
        self.calls.append((path, body or {}))
        if self.side_effect is not None:
            raise self.side_effect
        return {}


@pytest.mark.asyncio
async def test_begin_turn_posts_start_and_returns_run_id():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)

    run_id = await rep.begin_turn("msg_42")

    assert run_id.startswith("run_")
    assert len(http.calls) == 1
    path, body = http.calls[0]
    assert path == "/messages/msg_42/processing/start"
    assert body == {"run_id": run_id}
    # Reporter caches busy state so the next heartbeat echoes it.
    assert rep._current_status == "busy"
    assert rep._current_message_id == "msg_42"


@pytest.mark.asyncio
async def test_begin_turn_swallows_http_error():
    http = FakeHttp()
    http.side_effect = HttpError(403, "not a member")
    rep = StatusReporter(http, heartbeat_interval_s=999)

    # Caller's turn must proceed even when the indicator can't reach
    # the UI.
    run_id = await rep.begin_turn("msg_outsider")

    assert run_id.startswith("run_")
    # State stays idle since the server rejected the start.
    assert rep._current_status == "idle"


@pytest.mark.asyncio
async def test_end_turn_success_flips_status_idle():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    # Pretend begin_turn already succeeded.
    rep._current_status = "busy"
    rep._current_message_id = "msg_5"

    await rep.end_turn("msg_5", "run_xyz", succeeded=True)

    assert len(http.calls) == 1
    path, body = http.calls[0]
    assert path == "/messages/msg_5/processing/end"
    assert body == {"run_id": "run_xyz", "succeeded": True}
    assert rep._current_status == "idle"
    assert rep._current_message_id is None


@pytest.mark.asyncio
async def test_end_turn_failure_carries_error_text():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    rep._current_status = "busy"
    rep._current_message_id = "msg_5"

    await rep.end_turn(
        "msg_5",
        "run_xyz",
        succeeded=False,
        error_text="adapter died",
    )

    path, body = http.calls[0]
    assert body == {
        "run_id": "run_xyz",
        "succeeded": False,
        "error_text": "adapter died",
    }
    assert rep._current_status == "error"


@pytest.mark.asyncio
async def test_end_turn_truncates_overlong_error_text():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    overlong = "x" * 2000

    await rep.end_turn(
        "msg_x", "run_x", succeeded=False, error_text=overlong
    )

    _, body = http.calls[0]
    # Server caps at 1024 chars; reporter must truncate to match.
    assert len(body["error_text"]) == 1024


@pytest.mark.asyncio
async def test_end_turn_swallows_http_error():
    http = FakeHttp()
    http.side_effect = HttpError(500, "boom")
    rep = StatusReporter(http, heartbeat_interval_s=999)
    rep._current_status = "busy"

    # Cached state stays "busy" since the server-side reset never
    # happened.
    await rep.end_turn("msg_5", "run_xyz", succeeded=True)
    assert rep._current_status == "busy"


@pytest.mark.asyncio
async def test_report_error_flips_status_red():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    rep._current_status = "busy"
    rep._current_message_id = "msg_a"

    await rep.report_error("fatal: lost identity")

    path, body = http.calls[0]
    assert path == "/agents/me/heartbeat"
    assert body == {"status": "error", "error_text": "fatal: lost identity"}
    assert rep._current_status == "error"
    # error clears current_message_id client-side.
    assert rep._current_message_id is None


@pytest.mark.asyncio
async def test_heartbeat_loop_sends_immediately_then_on_interval():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=10.0)

    task = asyncio.create_task(rep.run_heartbeat_loop())
    # Let the immediate-on-startup heartbeat fire.
    await asyncio.sleep(0.05)
    rep.stop()
    await task

    assert len(http.calls) >= 1
    path, body = http.calls[0]
    assert path == "/agents/me/heartbeat"
    assert body == {"status": "idle"}


@pytest.mark.asyncio
async def test_heartbeat_busy_carries_current_message_id():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=10.0)
    rep._current_status = "busy"
    rep._current_message_id = "msg_long_running"

    task = asyncio.create_task(rep.run_heartbeat_loop())
    await asyncio.sleep(0.05)
    rep.stop()
    await task

    _, body = http.calls[0]
    assert body == {"status": "busy", "current_message_id": "msg_long_running"}


@pytest.mark.asyncio
async def test_heartbeat_swallows_429_silently():
    http = FakeHttp()
    http.side_effect = HttpError(429, "rate limited")
    rep = StatusReporter(http, heartbeat_interval_s=10.0)

    task = asyncio.create_task(rep.run_heartbeat_loop())
    await asyncio.sleep(0.05)
    rep.stop()
    await task

    # Loop survived the 429.
    assert len(http.calls) >= 1


@pytest.mark.asyncio
async def test_heartbeat_loop_stops_promptly_on_signal():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=600.0)

    task = asyncio.create_task(rep.run_heartbeat_loop())
    await asyncio.sleep(0.05)
    rep.stop()
    # Must exit promptly even though the next tick is 10 minutes away.
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_heartbeat_interval_clamped_to_minimum():
    # Clamp to 10s to match the server's rate-limit window.
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=0.5)
    assert rep._interval == 10.0


# ─── end_turn_batch ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_end_turn_batch_posts_all_runs_in_one_call():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    rep._current_status = "busy"
    rep._current_message_id = "msg_a"

    await rep.end_turn_batch([
        {"run_id": "run_a", "message_id": "msg_a", "succeeded": True},
        {"run_id": "run_b", "message_id": "msg_b", "succeeded": True},
        {"run_id": "run_c", "message_id": "msg_c", "succeeded": True},
    ])

    assert len(http.calls) == 1
    path, body = http.calls[0]
    assert path == "/messages/processing/end:batch"
    assert body == {"runs": [
        {"run_id": "run_a", "message_id": "msg_a", "succeeded": True},
        {"run_id": "run_b", "message_id": "msg_b", "succeeded": True},
        {"run_id": "run_c", "message_id": "msg_c", "succeeded": True},
    ]}
    assert rep._current_status == "idle"
    assert rep._current_message_id is None


@pytest.mark.asyncio
async def test_end_turn_batch_failure_flips_status_to_error():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)

    await rep.end_turn_batch([
        {"run_id": "run_a", "message_id": "msg_a", "succeeded": True},
        {"run_id": "run_b", "message_id": "msg_b", "succeeded": False,
         "error_text": "claude rate limit"},
    ])

    _path, body = http.calls[0]
    # error_text only emitted on the failing run, capped at 1024 chars.
    assert body["runs"][0] == {
        "run_id": "run_a", "message_id": "msg_a", "succeeded": True,
    }
    assert body["runs"][1] == {
        "run_id": "run_b", "message_id": "msg_b", "succeeded": False,
        "error_text": "claude rate limit",
    }
    assert rep._current_status == "error"


@pytest.mark.asyncio
async def test_end_turn_batch_empty_is_no_op():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    await rep.end_turn_batch([])
    assert http.calls == []


@pytest.mark.asyncio
async def test_end_turn_batch_swallows_http_error():
    http = FakeHttp()
    http.side_effect = HttpError(500, "boom")
    rep = StatusReporter(http, heartbeat_interval_s=999)

    # No raise — telemetry must not break the agent loop.
    await rep.end_turn_batch([
        {"run_id": "run_a", "message_id": "msg_a", "succeeded": True},
    ])
    assert len(http.calls) == 1


# ── Local-only envelope skip path ──────────────────────────────────


@pytest.mark.asyncio
async def test_begin_turn_skips_http_for_local_only_envelope():
    """Daemon-minted synthetic envelopes (intro-prompt-...) have no
    server-side row, so we skip ``/messages/<id>/processing/start``
    (which used to 404 + WARN per nudge) — but push an immediate busy
    heartbeat so the agent shows in-progress while composing its intro,
    not idle until the next scheduled beat."""
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)

    run_id = await rep.begin_turn("intro-prompt-ch_xxx-1778641626040")

    assert run_id.startswith("run_")
    # No per-message processing POST — just a busy heartbeat.
    assert not any(p.endswith("/processing/start") for p, _ in http.calls)
    assert len(http.calls) == 1
    path, body = http.calls[0]
    assert path == "/agents/me/heartbeat"
    assert body["status"] == "busy"
    assert "current_message_id" not in body   # synthetic id isn't sent
    assert rep._current_status == "busy"
    assert rep._current_message_id is None


@pytest.mark.asyncio
async def test_end_turn_skips_http_for_local_only_envelope():
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    rep._current_status = "busy"
    rep._current_message_id = "intro-prompt-ch_a-1"

    await rep.end_turn("intro-prompt-ch_a-1", "run_x", succeeded=True)

    # No /processing/end POST — just an idle heartbeat.
    assert not any("/processing/" in p for p, _ in http.calls)
    assert len(http.calls) == 1
    path, body = http.calls[0]
    assert path == "/agents/me/heartbeat"
    assert body["status"] == "idle"
    assert rep._current_status == "idle"
    assert rep._current_message_id is None


@pytest.mark.asyncio
async def test_end_turn_batch_filters_local_only_runs():
    """A thread batch can contain a synthetic intro envelope mixed
    with real follow-up messages. ``end_turn_batch`` must keep the
    real ones in the wire payload and drop the local-only entries
    so the server doesn't see a phantom message id."""
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)

    await rep.end_turn_batch([
        {"run_id": "run_intro", "message_id": "intro-prompt-ch_1-1", "succeeded": True},
        {"run_id": "run_real",  "message_id": "env_real", "succeeded": True},
    ])

    assert len(http.calls) == 1
    _, body = http.calls[0]
    msg_ids = [r["message_id"] for r in body["runs"]]
    assert msg_ids == ["env_real"]


@pytest.mark.asyncio
async def test_end_turn_batch_all_local_only_skips_http():
    """All-synthetic batches don't hit the processing endpoint — but
    still push an idle heartbeat so the in-progress flag clears."""
    http = FakeHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    rep._current_status = "busy"

    await rep.end_turn_batch([
        {"run_id": "run_a", "message_id": "intro-prompt-ch_a-1", "succeeded": True},
        {"run_id": "run_b", "message_id": "intro-prompt-ch_b-1", "succeeded": True},
    ])

    assert not any("/processing/" in p for p, _ in http.calls)
    assert len(http.calls) == 1
    path, body = http.calls[0]
    assert path == "/agents/me/heartbeat"
    assert body["status"] == "idle"
    assert rep._current_status == "idle"


# ── Keyless (bridge) transport: skip signed load_identity ──────────
#
# A keyless bridge agent has no local signing identity; every signed
# ``post`` would raise "identity not found" inside PuffoCoreHttpClient.
# The reporter must never reach the wire for these agents. The fake
# below makes ANY post raise, so a leaked POST fails loudly instead of
# silently — proving the guards short-circuit before ``self._http.post``.


class _ExplodingKeylessHttp:
    """Keyless http whose ``post`` mimics the real failure — the signed
    path raising because ``load_identity`` can't find a keyless agent's
    identity file. A correctly-guarded reporter never calls ``post``, so
    this exception must never surface."""

    keyless = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def post(self, path: str, body: dict | None = None):
        self.calls.append((path, body or {}))
        raise RuntimeError("identity not found: agent-abc1")


@pytest.mark.asyncio
async def test_keyless_begin_turn_no_http_returns_run_id():
    http = _ExplodingKeylessHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)

    run_id = await rep.begin_turn("msg_42")

    assert run_id.startswith("run_")
    assert http.calls == []  # never touched the signed wire
    assert rep._current_status == "busy"
    assert rep._current_message_id == "msg_42"


@pytest.mark.asyncio
async def test_keyless_end_turn_no_http_flips_status():
    http = _ExplodingKeylessHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    rep._current_status = "busy"
    rep._current_message_id = "msg_5"

    await rep.end_turn("msg_5", "run_x", succeeded=True)
    assert http.calls == []
    assert rep._current_status == "idle"
    assert rep._current_message_id is None

    rep._current_status = "busy"
    await rep.end_turn("msg_5", "run_y", succeeded=False, error_text="boom")
    assert http.calls == []
    assert rep._current_status == "error"


@pytest.mark.asyncio
async def test_keyless_end_turn_batch_no_http_flips_status():
    http = _ExplodingKeylessHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)

    await rep.end_turn_batch([
        {"run_id": "run_a", "message_id": "msg_a", "succeeded": True},
        {"run_id": "run_b", "message_id": "msg_b", "succeeded": True},
    ])
    assert http.calls == []
    assert rep._current_status == "idle"
    assert rep._current_message_id is None

    await rep.end_turn_batch([
        {"run_id": "run_c", "message_id": "msg_c", "succeeded": False},
    ])
    assert http.calls == []
    assert rep._current_status == "error"


@pytest.mark.asyncio
async def test_keyless_end_turn_batch_empty_still_no_op():
    http = _ExplodingKeylessHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    await rep.end_turn_batch([])
    assert http.calls == []


@pytest.mark.asyncio
async def test_keyless_report_error_no_http_sets_error():
    http = _ExplodingKeylessHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)

    await rep.report_error("fatal")
    assert http.calls == []
    assert rep._current_status == "error"
    assert rep._current_message_id is None


@pytest.mark.asyncio
async def test_keyless_heartbeat_loop_is_immediate_noop():
    http = _ExplodingKeylessHttp()
    rep = StatusReporter(http, heartbeat_interval_s=10.0)

    # The loop must return at once (not spin waiting on the interval) and
    # never touch the signed heartbeat route.
    await asyncio.wait_for(rep.run_heartbeat_loop(), timeout=1.0)
    assert http.calls == []


@pytest.mark.asyncio
async def test_keyless_send_heartbeat_guarded_directly():
    """Defense-in-depth: even a direct ``_send_heartbeat`` call (e.g. a
    future caller) must not POST for a keyless agent."""
    http = _ExplodingKeylessHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    rep._current_status = "busy"
    rep._current_message_id = "msg_z"

    await rep._send_heartbeat()
    assert http.calls == []


@pytest.mark.asyncio
async def test_native_reporter_defaults_to_signed_path():
    """A reporter over a non-keyless http client (FakeHttp default, and
    every native agent) keeps firing signed POSTs — the guard is strictly
    opt-in on ``http.keyless``."""
    http = FakeHttp()  # keyless=False by default
    rep = StatusReporter(http, heartbeat_interval_s=999)
    assert rep._keyless is False

    run_id = await rep.begin_turn("msg_native")
    assert http.calls == [
        ("/messages/msg_native/processing/start", {"run_id": run_id}),
    ]


@pytest.mark.asyncio
async def test_reporter_keyless_defaults_false_without_attr():
    """An http fake lacking the ``keyless`` attribute entirely resolves to
    native (False) via ``getattr(..., False)`` — no AttributeError."""

    class _BareHttp:
        def __init__(self):
            self.calls = []

        async def post(self, path, body=None):
            self.calls.append((path, body or {}))
            return {}

    http = _BareHttp()
    rep = StatusReporter(http, heartbeat_interval_s=999)
    assert rep._keyless is False
    await rep.begin_turn("msg_bare")
    assert len(http.calls) == 1
