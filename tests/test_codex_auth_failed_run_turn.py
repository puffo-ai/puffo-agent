"""Codex auth-failure run_turn behavior — when the App Server emits
a ``turn/failed`` (or top-level ``error``) notification carrying an
auth-class err_text, ``CodexSession.run_turn`` must raise
``AgentAPIError(is_auth=True)`` instead of a bare ``RuntimeError``.
This is the load-bearing handoff into the worker's PUF-283
``auth_failed`` substrate at worker.py:1141-1152."""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.adapters.codex_session import CodexSession
from puffo_agent.agent.core import AgentAPIError


FAKE_HEADER = textwrap.dedent('''\
    import json, sys

    def w(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    def r():
        line = sys.stdin.readline()
        return json.loads(line) if line else None

    def absorb_initialize():
        msg = r()
        assert msg["method"] == "initialize"
        w({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
''')


def _write_fake(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake_codex.py"
    path.write_text(FAKE_HEADER + "\n" + body, encoding="utf-8")
    return path


def _argv_for(fake: Path) -> list[str]:
    return [sys.executable, str(fake)]


def _make_session(tmp_path: Path, body: str) -> CodexSession:
    fake = _write_fake(tmp_path, body)
    return CodexSession(
        agent_id="codex-test-001",
        session_file=tmp_path / "codex_session.json",
        argv=_argv_for(fake),
        cwd=str(tmp_path),
    )


# ── Auth-class turn/failed: raises AgentAPIError(is_auth=True) ─────────────


@pytest.mark.parametrize("err_message", [
    "refresh token was revoked",
    "token_invalidated",
    "websocket /responses returned 401 Unauthorized",
])
def test_auth_class_turn_failed_raises_agent_api_error_auth(
    tmp_path, err_message,
):
    """When ``turn/failed`` carries an auth-class err_text, run_turn
    converts the inner ``RuntimeError`` into ``AgentAPIError(is_auth=
    True)`` so the worker's PUF-283 substrate fires the operator DM
    instead of treating the failure as a generic adapter exception."""
    cs = _make_session(tmp_path, f'''\
absorb_initialize()
# Resolve thread/start so _bootstrap_session completes.
msg = r()
w({{"jsonrpc": "2.0", "id": msg["id"],
   "result": {{"thread": {{"id": "conv_auth"}}}}}})

# turn/start: ACK with running so _send_raw_request resolves; the
# turn completion comes via the subsequent turn/failed notification.
msg = r()
turn_id = msg["id"]
w({{"jsonrpc": "2.0", "id": turn_id,
   "result": {{"turn": {{"id": "t1", "status": "running"}}}}}})
w({{"jsonrpc": "2.0", "method": "turn/failed",
   "params": {{"error": {{"message": {err_message!r}}}}}}})

while True:
    line = sys.stdin.readline()
    if not line:
        break
''')

    async def _run():
        try:
            await cs.warm("sp")
            await cs.run_turn("hi", "sp")
        finally:
            await cs.aclose()

    with pytest.raises(AgentAPIError) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.is_auth is True
    assert "codex auth failed" in str(excinfo.value)
    assert err_message.lower() in str(excinfo.value).lower()


# ── Non-auth turn/failed: still raises RuntimeError ────────────────────────


def test_non_auth_turn_failed_raises_runtime_error(tmp_path):
    """Generic turn/failed (e.g. model not supported) must NOT be
    coerced to AgentAPIError — the worker's auth_failed substrate
    would mis-fire an OAuth-expired DM for a quota / model issue."""
    cs = _make_session(tmp_path, '''\
absorb_initialize()
msg = r()
w({"jsonrpc": "2.0", "id": msg["id"],
   "result": {"thread": {"id": "conv_nonauth"}}})

msg = r()
turn_id = msg["id"]
w({"jsonrpc": "2.0", "id": turn_id,
   "result": {"turn": {"id": "t1", "status": "running"}}})
w({"jsonrpc": "2.0", "method": "turn/failed",
   "params": {"error": {"message": "model not supported"}}})

while True:
    line = sys.stdin.readline()
    if not line:
        break
''')

    async def _run():
        try:
            await cs.warm("sp")
            await cs.run_turn("hi", "sp")
        finally:
            await cs.aclose()

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_run())
    # Must NOT be an AgentAPIError subclass — that would be a false
    # auth_failed flip.
    assert not isinstance(excinfo.value, AgentAPIError)
    assert "model not supported" in str(excinfo.value)


def test_invalid_thread_id_not_classified_as_auth(tmp_path):
    """Regression-pin: ``invalid thread id ... found 0`` was 矩阵's
    initial (later-corrected) auth-guess in the d2d2 case. PUF-310's
    pattern set excludes it; this test pins that exclusion at the
    run_turn integration layer."""
    cs = _make_session(tmp_path, '''\
absorb_initialize()
msg = r()
w({"jsonrpc": "2.0", "id": msg["id"],
   "result": {"thread": {"id": "conv_thread_err"}}})

msg = r()
turn_id = msg["id"]
w({"jsonrpc": "2.0", "id": turn_id,
   "result": {"turn": {"id": "t1", "status": "running"}}})
w({"jsonrpc": "2.0", "method": "turn/failed",
   "params": {"error": {"message":
       "invalid thread id: invalid length: expected length 32, found 0"}}})

while True:
    line = sys.stdin.readline()
    if not line:
        break
''')

    async def _run():
        try:
            await cs.warm("sp")
            await cs.run_turn("hi", "sp")
        finally:
            await cs.aclose()

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_run())
    assert not isinstance(excinfo.value, AgentAPIError)
