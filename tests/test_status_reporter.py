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

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.side_effect: BaseException | None = None

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
