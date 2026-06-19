"""PUF-311 Codex health-probe round-trip check.

``CodexSession.health_probe`` is the load-bearing primitive — when
``on_refresh_success`` has just eagerly cleared ``runtime.health =
auth_failed``, the worker uses this probe to verify the codex
App Server can actually reach OpenAI before marking the agent
healthy. The probe must return True iff the JSON-RPC handshake +
``thread/start`` round-trip succeeded (proxy for "auth + transport
both work") and False on any failure — including the load-bearing
case where the host token is still broken after the refresher's
optimistic clear.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.adapters.codex_session import CodexSession


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
        agent_id="codex-probe-001",
        session_file=tmp_path / "codex_session.json",
        argv=_argv_for(fake),
        cwd=str(tmp_path),
    )


# ── Happy path ────────────────────────────────────────────────────────────


def test_probe_passes_when_thread_start_succeeds(tmp_path):
    """The probe's load-bearing definition: a fresh JSON-RPC handshake
    + ``thread/start`` returning a valid conversation id is sufficient
    to confirm the round-trip works."""
    cs = _make_session(tmp_path, '''\
absorb_initialize()
msg = r()
w({"jsonrpc": "2.0", "id": msg["id"],
   "result": {"thread": {"id": "conv_healthy"}}})

while True:
    line = sys.stdin.readline()
    if not line:
        break
''')

    async def _run():
        try:
            return await cs.health_probe()
        finally:
            await cs.aclose()

    assert asyncio.run(_run()) is True


# ── Auth still broken: thread/start fails ─────────────────────────────────


def test_probe_fails_when_thread_start_errors(tmp_path):
    """The d2d2-class case: host token still broken post-eager-clear.
    ``thread/start`` returns an auth-class error; ``_bootstrap_session``
    raises; the probe captures the exception and returns False so the
    worker can reassert runtime.health = auth_failed."""
    cs = _make_session(tmp_path, '''\
absorb_initialize()
msg = r()
w({"jsonrpc": "2.0", "id": msg["id"],
   "error": {"code": -32603, "message": "refresh token was revoked"}})

while True:
    line = sys.stdin.readline()
    if not line:
        break
''')

    async def _run():
        try:
            return await cs.health_probe()
        finally:
            await cs.aclose()

    assert asyncio.run(_run()) is False


def test_probe_fails_when_subprocess_cannot_spawn(tmp_path):
    """Defense-in-depth: a missing codex binary (PATH gap, post-
    upgrade staging issue) raises ``RuntimeError`` from ``_spawn``.
    The probe swallows it and returns False — the worker decides
    whether that warrants reasserting auth_failed."""
    cs = CodexSession(
        agent_id="codex-probe-noexec",
        session_file=tmp_path / "codex_session.json",
        argv=["/nonexistent/codex/binary"],
        cwd=str(tmp_path),
    )

    async def _run():
        try:
            return await cs.health_probe()
        finally:
            await cs.aclose()

    assert asyncio.run(_run()) is False


# ── Existing session: probe is idempotent ─────────────────────────────────


def test_probe_idempotent_when_subprocess_already_running(tmp_path):
    """A session that's already ``_ensure_running``-warm shouldn't
    pay a second spawn cost. ``_ensure_running`` returns early when
    the proc is alive AND a conversation id is set — the probe's
    second call is a single check + immediate True."""
    cs = _make_session(tmp_path, '''\
absorb_initialize()
msg = r()
w({"jsonrpc": "2.0", "id": msg["id"],
   "result": {"thread": {"id": "conv_idemp"}}})

while True:
    line = sys.stdin.readline()
    if not line:
        break
''')

    async def _run():
        try:
            first = await cs.health_probe()
            second = await cs.health_probe()
            return first, second, cs._conversation_id
        finally:
            await cs.aclose()

    first, second, cid = asyncio.run(_run())
    assert first is True
    assert second is True
    assert cid == "conv_idemp"
