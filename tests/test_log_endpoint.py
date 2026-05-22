"""PUF-238: /v1/agents/{id}/log endpoint — reads audit.log, tail +
since delta polling, missing-file empty state, malformed-line
preservation, MAX_TAIL cap."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from _bridge_support import (
    isolated_home, make_user, pair_request_body, signed_headers,
    write_test_agent,
)
from puffo_agent.portal.api.server import build_app
from puffo_agent.portal.state import DaemonConfig

pytestmark = pytest.mark.asyncio

_HOST = {"Host": "127.0.0.1:63387"}


@pytest_asyncio.fixture
async def client():
    isolated_home()
    cfg = DaemonConfig().bridge
    app = build_app(cfg)
    server = TestServer(app)
    async with TestClient(server) as c:
        yield c


async def _pair(client, user):
    body = pair_request_body(user)
    h = signed_headers(user, "POST", "/v1/pair", body); h.update(_HOST)
    r = await client.post("/v1/pair", data=body, headers=h)
    assert r.status == 200, await r.text()


def _audit_log_path(home: str, agent_id: str) -> Path:
    return Path(home) / "agents" / agent_id / "workspace" / ".puffo-agent" / "audit.log"


def _write_audit_lines(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


# ────────────────────────────────────────────────────────────────────
# Missing-file empty state
# ────────────────────────────────────────────────────────────────────


async def test_log_missing_file_returns_empty_with_note(client):
    user = make_user()
    await _pair(client, user)
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-fresh")
    h = signed_headers(user, "GET", "/v1/agents/agt-fresh/log"); h.update(_HOST)
    r = await client.get("/v1/agents/agt-fresh/log", headers=h)
    assert r.status == 200
    j = await r.json()
    assert j["agent_id"] == "agt-fresh"
    assert j["lines"] == []
    assert j["next_cursor"] == 0
    assert "note" in j


# ────────────────────────────────────────────────────────────────────
# Tail mode (default)
# ────────────────────────────────────────────────────────────────────


async def test_log_tail_returns_last_n_lines(client):
    user = make_user()
    await _pair(client, user)
    home = os.environ["PUFFO_AGENT_HOME"]
    write_test_agent(home, "agt-tail")
    log_path = _audit_log_path(home, "agt-tail")
    _write_audit_lines(log_path, [
        {"ts": f"2026-05-22T00:00:0{i}Z", "agent": "agt-tail", "event": "turn", "n": i}
        for i in range(10)
    ])

    h = signed_headers(user, "GET", "/v1/agents/agt-tail/log?tail=3"); h.update(_HOST)
    r = await client.get("/v1/agents/agt-tail/log?tail=3", headers=h)
    j = await r.json()
    assert len(j["lines"]) == 3
    # Last 3 events: 7, 8, 9 (zero-indexed).
    assert [line["n"] for line in j["lines"]] == [7, 8, 9]
    assert j["next_cursor"] == log_path.stat().st_size


async def test_log_default_tail_is_200(client):
    user = make_user()
    await _pair(client, user)
    home = os.environ["PUFFO_AGENT_HOME"]
    write_test_agent(home, "agt-default")
    log_path = _audit_log_path(home, "agt-default")
    _write_audit_lines(log_path, [
        {"ts": "t", "agent": "agt-default", "event": "e", "n": i}
        for i in range(250)
    ])

    h = signed_headers(user, "GET", "/v1/agents/agt-default/log"); h.update(_HOST)
    r = await client.get("/v1/agents/agt-default/log", headers=h)
    j = await r.json()
    assert len(j["lines"]) == 200
    # Last 200 of 250 → events 50..249.
    assert j["lines"][0]["n"] == 50
    assert j["lines"][-1]["n"] == 249


async def test_log_tail_capped_at_max(client):
    user = make_user()
    await _pair(client, user)
    home = os.environ["PUFFO_AGENT_HOME"]
    write_test_agent(home, "agt-cap")
    log_path = _audit_log_path(home, "agt-cap")
    _write_audit_lines(log_path, [
        {"ts": "t", "agent": "agt-cap", "event": "e", "n": i}
        for i in range(2500)
    ])

    # Request 5000 — should be capped to 2000.
    h = signed_headers(user, "GET", "/v1/agents/agt-cap/log?tail=5000"); h.update(_HOST)
    r = await client.get("/v1/agents/agt-cap/log?tail=5000", headers=h)
    j = await r.json()
    assert len(j["lines"]) == 2000


async def test_log_invalid_tail_falls_back_to_default(client):
    user = make_user()
    await _pair(client, user)
    home = os.environ["PUFFO_AGENT_HOME"]
    write_test_agent(home, "agt-bad-tail")
    log_path = _audit_log_path(home, "agt-bad-tail")
    _write_audit_lines(log_path, [
        {"ts": "t", "agent": "agt-bad-tail", "event": "e", "n": i}
        for i in range(5)
    ])

    h = signed_headers(user, "GET", "/v1/agents/agt-bad-tail/log?tail=banana"); h.update(_HOST)
    r = await client.get("/v1/agents/agt-bad-tail/log?tail=banana", headers=h)
    j = await r.json()
    # Bad tail param → default 200; only 5 lines on disk, so all 5.
    assert len(j["lines"]) == 5


# ────────────────────────────────────────────────────────────────────
# Since (delta polling)
# ────────────────────────────────────────────────────────────────────


async def test_log_since_returns_delta_after_cursor(client):
    user = make_user()
    await _pair(client, user)
    home = os.environ["PUFFO_AGENT_HOME"]
    write_test_agent(home, "agt-delta")
    log_path = _audit_log_path(home, "agt-delta")

    # Initial state: 3 events.
    _write_audit_lines(log_path, [
        {"ts": "t", "agent": "agt-delta", "event": "first", "n": i}
        for i in range(3)
    ])
    h1 = signed_headers(user, "GET", "/v1/agents/agt-delta/log"); h1.update(_HOST)
    r1 = await client.get("/v1/agents/agt-delta/log", headers=h1)
    j1 = await r1.json()
    cursor = j1["next_cursor"]
    assert len(j1["lines"]) == 3

    # Append 2 more events.
    _write_audit_lines(log_path, [
        {"ts": "t", "agent": "agt-delta", "event": "later", "n": i}
        for i in range(3, 5)
    ])

    h2 = signed_headers(user, "GET", f"/v1/agents/agt-delta/log?since={cursor}")
    h2.update(_HOST)
    r2 = await client.get(f"/v1/agents/agt-delta/log?since={cursor}", headers=h2)
    j2 = await r2.json()
    # Delta returns only the 2 new lines.
    assert len(j2["lines"]) == 2
    assert all(line["event"] == "later" for line in j2["lines"])
    # Cursor advances to current EOF.
    assert j2["next_cursor"] == log_path.stat().st_size


async def test_log_since_empty_when_caller_at_eof(client):
    user = make_user()
    await _pair(client, user)
    home = os.environ["PUFFO_AGENT_HOME"]
    write_test_agent(home, "agt-eof")
    log_path = _audit_log_path(home, "agt-eof")
    _write_audit_lines(log_path, [{"ts": "t", "agent": "agt-eof", "event": "x"}])
    size = log_path.stat().st_size

    h = signed_headers(user, "GET", f"/v1/agents/agt-eof/log?since={size}")
    h.update(_HOST)
    r = await client.get(f"/v1/agents/agt-eof/log?since={size}", headers=h)
    j = await r.json()
    assert j["lines"] == []
    assert j["next_cursor"] == size
    # Empty delta still gets the empty-state note so the client can
    # render distinctly from "haven't loaded yet."
    assert "note" in j


async def test_log_since_past_eof_resets_to_zero(client):
    # Rotation / archive simulation: file shrinks below the cursor.
    user = make_user()
    await _pair(client, user)
    home = os.environ["PUFFO_AGENT_HOME"]
    write_test_agent(home, "agt-rotate")
    log_path = _audit_log_path(home, "agt-rotate")
    _write_audit_lines(log_path, [{"ts": "t", "agent": "agt-rotate", "event": "post-rotation"}])

    # Caller cursor pretends a much larger file.
    h = signed_headers(user, "GET", "/v1/agents/agt-rotate/log?since=999999")
    h.update(_HOST)
    r = await client.get("/v1/agents/agt-rotate/log?since=999999", headers=h)
    j = await r.json()
    # Cursor was past EOF → reset to 0; full file returned.
    assert len(j["lines"]) == 1
    assert j["lines"][0]["event"] == "post-rotation"


# ────────────────────────────────────────────────────────────────────
# Malformed lines + auth
# ────────────────────────────────────────────────────────────────────


async def test_log_malformed_line_preserved_as_raw_event(client):
    user = make_user()
    await _pair(client, user)
    home = os.environ["PUFFO_AGENT_HOME"]
    write_test_agent(home, "agt-malformed")
    log_path = _audit_log_path(home, "agt-malformed")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # One valid JSON line + one bare text line that JSON can't parse.
    log_path.write_text(
        json.dumps({"ts": "t", "agent": "agt-malformed", "event": "good"}) + "\n"
        + "this is not JSON\n",
        encoding="utf-8",
    )

    h = signed_headers(user, "GET", "/v1/agents/agt-malformed/log"); h.update(_HOST)
    r = await client.get("/v1/agents/agt-malformed/log", headers=h)
    j = await r.json()
    assert len(j["lines"]) == 2
    assert j["lines"][0]["event"] == "good"
    assert j["lines"][1]["event"] == "_raw"
    assert "this is not JSON" in j["lines"][1]["msg"]


async def test_log_unknown_agent_returns_404(client):
    user = make_user()
    await _pair(client, user)
    h = signed_headers(user, "GET", "/v1/agents/agt-nope/log"); h.update(_HOST)
    r = await client.get("/v1/agents/agt-nope/log", headers=h)
    assert r.status == 404


async def test_log_unpaired_caller_returns_401(client):
    # No /v1/pair first — middleware should reject unsigned-paired calls.
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-noauth")
    r = await client.get(
        "/v1/agents/agt-noauth/log",
        headers={**_HOST},
    )
    assert r.status == 401
