"""PUF-267: codex thread-wedging visibility + auto-rotation.

Unit tests targeting the three-layer defensive surface added to
``CodexSession``:

  Layer 1 — counter-based rotation after N consecutive turn timeouts /
            failures
  Layer 2 — propagation of the timeout/failure outcome to per-agent
            ``runtime.health = "codex_thread_wedged"``
  Layer 3 — verbatim ``"agent thread limit reached"`` triggers immediate
            rotation without waiting for the counter

Most tests poke ``_propagate_turn_outcome`` + ``_reset_conversation``
directly; the bottom-of-file ``run_turn`` wiring tests drive the full
fake-codex App Server subprocess (mirroring ``test_codex_session.py``)
to confirm the bookkeeping is actually invoked from the production
turn path.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
import time
from pathlib import Path

import pytest

from puffo_agent.agent.adapters.codex_session import (
    CODEX_THREAD_WEDGED_THRESHOLD,
    CodexSession,
)


def _make_session(tmp_path: Path, agent_id: str = "codex-agent-puf267") -> CodexSession:
    session_file = tmp_path / "agents" / agent_id / "codex_session.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    return CodexSession(
        agent_id=agent_id,
        session_file=session_file,
        argv=["true"],
        cwd=str(tmp_path),
        env={},
        permission_mode="bypassPermissions",
        model="",
    )


def _seed_runtime(tmp_path: Path, agent_id: str, *, health: str = "ok") -> None:
    """Write a runtime.json under PUFFO_AGENT_HOME=tmp_path so the
    flip helpers can find an agent to mutate."""
    from puffo_agent.portal.state import RuntimeState
    rs = RuntimeState(
        status="running", started_at=int(time.time()), health=health,
    )
    rs.save(agent_id)


@pytest.fixture
def env_home(tmp_path, monkeypatch):
    """Point PUFFO_AGENT_HOME at tmp_path so RuntimeState.load/.save
    resolve there."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    return tmp_path


# ── Counter mechanics ────────────────────────────────────────────────


def test_success_resets_counter(env_home):
    s = _make_session(env_home)
    s._conversation_id = "thr_active"
    s._consecutive_thread_failures = 1
    rotated = s._propagate_turn_outcome(outcome="success")
    assert rotated is False
    assert s._consecutive_thread_failures == 0
    # Successful outcome must NOT clear an in-use conversation_id.
    assert s._conversation_id == "thr_active"


def test_single_timeout_below_threshold_does_not_rotate(env_home):
    s = _make_session(env_home)
    s._conversation_id = "thr_active"
    rotated = s._propagate_turn_outcome(outcome="timeout")
    assert rotated is False
    assert s._consecutive_thread_failures == 1
    assert s._conversation_id == "thr_active"


def test_threshold_consecutive_timeouts_rotate_and_flip(env_home):
    """At exactly CODEX_THREAD_WEDGED_THRESHOLD consecutive non-success
    outcomes the conversation must rotate AND the per-agent runtime
    health flips to ``codex_thread_wedged``."""
    from puffo_agent.portal.state import RuntimeState
    aid = "codex-puf267-a"
    _seed_runtime(env_home, aid, health="ok")
    s = _make_session(env_home, agent_id=aid)
    s._conversation_id = "thr_wedged"
    s._save_conversation_id("thr_wedged")
    # First non-success below threshold → no rotation.
    rotated_first = s._propagate_turn_outcome(outcome="timeout")
    assert rotated_first is False
    assert s._conversation_id == "thr_wedged"
    # Threshold hit on the next tick → rotate.
    assert CODEX_THREAD_WEDGED_THRESHOLD == 2
    rotated = s._propagate_turn_outcome(outcome="timeout")
    assert rotated is True
    assert s._conversation_id == ""
    # Persisted session file reflects the empty conversation id.
    assert s._load_conversation_id() == ""
    rs = RuntimeState.load(aid)
    assert rs is not None
    assert rs.health == "codex_thread_wedged"
    assert "automatic on the next inbound message" in rs.error


def test_threshold_consecutive_turn_failed_rotate_and_flip(env_home):
    aid = "codex-puf267-b"
    _seed_runtime(env_home, aid, health="ok")
    s = _make_session(env_home, agent_id=aid)
    s._conversation_id = "thr_wedged"
    s._save_conversation_id("thr_wedged")
    s._propagate_turn_outcome(outcome="turn_failed", err_text="boom")
    rotated = s._propagate_turn_outcome(outcome="turn_failed", err_text="boom")
    assert rotated is True
    from puffo_agent.portal.state import RuntimeState
    rs = RuntimeState.load(aid)
    assert rs.health == "codex_thread_wedged"


def test_success_after_rotation_clears_wedged_health(env_home):
    aid = "codex-puf267-c"
    _seed_runtime(env_home, aid, health="ok")
    s = _make_session(env_home, agent_id=aid)
    s._conversation_id = "thr_wedged"
    s._save_conversation_id("thr_wedged")
    # Drive the rotation.
    for _ in range(CODEX_THREAD_WEDGED_THRESHOLD):
        s._propagate_turn_outcome(outcome="timeout")
    from puffo_agent.portal.state import RuntimeState
    assert RuntimeState.load(aid).health == "codex_thread_wedged"
    # Simulate the next ensure_running starting a fresh thread.
    s._conversation_id = "thr_fresh"
    # Next turn succeeds → counter resets + health clears.
    rotated = s._propagate_turn_outcome(outcome="success")
    assert rotated is False
    assert s._consecutive_thread_failures == 0
    rs = RuntimeState.load(aid)
    assert rs.health == "ok"
    assert rs.error == ""


# ── Layer 3: verbatim "agent thread limit reached" ────────────────────


def test_thread_limit_verbatim_string_rotates_immediately(env_home):
    """Codex's explicit ``agent thread limit reached`` error rotates on
    the FIRST occurrence — no counter wait. The string match is
    case-insensitive."""
    aid = "codex-puf267-d"
    _seed_runtime(env_home, aid, health="ok")
    s = _make_session(env_home, agent_id=aid)
    s._conversation_id = "thr_wedged"
    s._save_conversation_id("thr_wedged")
    rotated = s._propagate_turn_outcome(
        outcome="turn_failed",
        err_text="codex turn failed: agent thread limit reached",
    )
    assert rotated is True
    assert s._conversation_id == ""
    from puffo_agent.portal.state import RuntimeState
    rs = RuntimeState.load(aid)
    assert rs.health == "codex_thread_wedged"
    assert "thread-limit verbatim" in rs.error


def test_thread_limit_case_insensitive(env_home):
    aid = "codex-puf267-e"
    _seed_runtime(env_home, aid, health="ok")
    s = _make_session(env_home, agent_id=aid)
    s._conversation_id = "thr_wedged"
    s._save_conversation_id("thr_wedged")
    rotated = s._propagate_turn_outcome(
        outcome="turn_failed",
        err_text="AGENT Thread LIMIT Reached",
    )
    assert rotated is True


def test_other_turn_failed_does_not_immediate_rotate(env_home):
    """A turn-failed without the verbatim thread-limit string follows
    the counter path — single occurrence does NOT rotate."""
    aid = "codex-puf267-f"
    _seed_runtime(env_home, aid, health="ok")
    s = _make_session(env_home, agent_id=aid)
    s._conversation_id = "thr_wedged"
    s._save_conversation_id("thr_wedged")
    rotated = s._propagate_turn_outcome(
        outcome="turn_failed",
        err_text="codex turn failed: some other error",
    )
    assert rotated is False
    assert s._conversation_id == "thr_wedged"
    from puffo_agent.portal.state import RuntimeState
    rs = RuntimeState.load(aid)
    assert rs.health == "ok"


# ── Precedence guards ────────────────────────────────────────────────


def test_does_not_overwrite_auth_failed(env_home):
    aid = "codex-puf267-g"
    _seed_runtime(env_home, aid, health="auth_failed")
    s = _make_session(env_home, agent_id=aid)
    s._conversation_id = "thr_wedged"
    s._save_conversation_id("thr_wedged")
    for _ in range(CODEX_THREAD_WEDGED_THRESHOLD):
        s._propagate_turn_outcome(outcome="timeout")
    from puffo_agent.portal.state import RuntimeState
    rs = RuntimeState.load(aid)
    assert rs.health == "auth_failed", (
        "codex_thread_wedged must not downgrade auth_failed"
    )


def test_does_not_overwrite_api_error_abandoned(env_home):
    aid = "codex-puf267-h"
    _seed_runtime(env_home, aid, health="api_error_abandoned")
    s = _make_session(env_home, agent_id=aid)
    s._conversation_id = "thr_wedged"
    for _ in range(CODEX_THREAD_WEDGED_THRESHOLD):
        s._propagate_turn_outcome(outcome="timeout")
    from puffo_agent.portal.state import RuntimeState
    assert RuntimeState.load(aid).health == "api_error_abandoned"


def test_does_not_overwrite_refresh_broken(env_home):
    aid = "codex-puf267-i"
    _seed_runtime(env_home, aid, health="refresh_broken")
    s = _make_session(env_home, agent_id=aid)
    s._conversation_id = "thr_wedged"
    for _ in range(CODEX_THREAD_WEDGED_THRESHOLD):
        s._propagate_turn_outcome(outcome="timeout")
    from puffo_agent.portal.state import RuntimeState
    assert RuntimeState.load(aid).health == "refresh_broken"


# ── _reset_conversation persistence ──────────────────────────────────


def test_reset_conversation_clears_persisted_file(env_home):
    """The rotation must clear BOTH the in-memory id AND the on-disk
    session_file so a daemon restart between turns doesn't re-load
    the wedged id from disk."""
    s = _make_session(env_home)
    s._conversation_id = "thr_to_clear"
    s._save_conversation_id("thr_to_clear")
    assert s._load_conversation_id() == "thr_to_clear"
    s._reset_conversation()
    assert s._conversation_id == ""
    assert s._load_conversation_id() == ""


# ── PR #55 review polish ─────────────────────────────────────────────


def test_codex_thread_wedged_cleared_after_daemon_restart(env_home):
    """Operator-prescribed (PR #55 review item 1): a fresh session with
    in-memory counter=0 must still always clear an on-disk
    ``codex_thread_wedged`` flag on the first successful turn.

    Mirrors the PR #52 v1 stale-state bug class: gating the clear on
    counter>0 would leave runtime.health stuck on the wedged value
    forever after a daemon restart that happens between rotation and
    the next success."""
    from puffo_agent.portal.state import RuntimeState
    aid = "codex-puf267-restart"
    _seed_runtime(env_home, aid, health="codex_thread_wedged")
    s = _make_session(env_home, agent_id=aid)
    # Brand new session — no in-memory streak. This is the daemon-
    # restart-then-success path.
    assert s._consecutive_thread_failures == 0
    rotated = s._propagate_turn_outcome(outcome="success")
    assert rotated is False
    rs = RuntimeState.load(aid)
    assert rs is not None
    assert rs.health == "ok"
    assert rs.error == ""


def test_counter_resets_after_rotation_to_avoid_thrash(env_home):
    """Operator-prescribed (PR #55 review item 2): after rotation fires
    the counter must reset to 0 so the freshly-rotated thread gets a
    fair THRESHOLD-budget — otherwise a broken App Server would
    rotate on every single subsequent turn (counter-thrash)."""
    aid = "codex-puf267-thrash"
    _seed_runtime(env_home, aid, health="ok")
    s = _make_session(env_home, agent_id=aid)
    s._conversation_id = "thr_1"
    # Drive the rotation by hitting THRESHOLD non-success outcomes.
    for _ in range(CODEX_THREAD_WEDGED_THRESHOLD):
        s._propagate_turn_outcome(outcome="timeout")
    # Counter must have reset.
    assert s._consecutive_thread_failures == 0
    # Simulate _ensure_running starting a fresh thread.
    s._conversation_id = "thr_2"
    # A single subsequent timeout must NOT re-rotate — the fresh
    # thread has its own THRESHOLD budget.
    rotated = s._propagate_turn_outcome(outcome="timeout")
    assert rotated is False
    assert s._conversation_id == "thr_2"
    assert s._consecutive_thread_failures == 1


# ── Layer 3 helper (Item 6) ──────────────────────────────────────────


def test_looks_like_codex_thread_limit_helper():
    """Tuple-shape extensibility (PR #55 review item 6): the helper
    matches the verbatim string case-insensitively and rejects
    unrelated turn-failed errors."""
    from puffo_agent.agent.adapters.codex_session import (
        _looks_like_codex_thread_limit,
    )
    assert _looks_like_codex_thread_limit(
        "codex turn failed: agent thread limit reached",
    )
    assert _looks_like_codex_thread_limit("AGENT THREAD LIMIT REACHED")
    assert not _looks_like_codex_thread_limit("rate limit exceeded")
    assert not _looks_like_codex_thread_limit("")


# ── run_turn wiring tests (PR #55 review item 4) ─────────────────────
#
# Confirm the production turn path actually invokes
# _propagate_turn_outcome at the success / turn_failed sites. Uses the
# same fake-codex-app-server pattern as test_codex_session.py so the
# wire shape matches what real codex emits. Timeout coverage is
# omitted because TURN_TIMEOUT_SECONDS = 600s makes the wire-level
# path impractical to exercise; the bookkeeping for the timeout outcome
# is fully covered by the direct ``_propagate_turn_outcome(outcome=
# "timeout")`` tests above.


_FAKE_HEADER = textwrap.dedent('''\
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
    path.write_text(_FAKE_HEADER + "\n" + body, encoding="utf-8")
    return path


def _wiring_session(tmp_path: Path, fake: Path, agent_id: str) -> CodexSession:
    session_file = tmp_path / "agents" / agent_id / "codex_session.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    return CodexSession(
        agent_id=agent_id,
        session_file=session_file,
        argv=[sys.executable, str(fake)],
        cwd=str(tmp_path),
    )


_WIRING_SUCCESS = '''\
absorb_initialize()
msg = r()
w({"jsonrpc": "2.0", "id": msg["id"], "result": {"thread": {"id": "conv_w1"}}})
msg = r()
turn_id = msg["id"]
w({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
   "params": {"threadId": "t", "turnId": "u", "itemId": "m", "delta": "ok"}})
w({"jsonrpc": "2.0", "id": turn_id, "result": None})
w({"jsonrpc": "2.0", "method": "turn/completed", "params": {}})
while True:
    line = sys.stdin.readline()
    if not line:
        break
'''


def test_run_turn_success_clears_wedged_via_wiring(env_home):
    """``run_turn`` success path must invoke
    ``_propagate_turn_outcome(outcome='success')``, which in turn
    always clears an on-disk ``codex_thread_wedged`` flag."""
    aid = "codex-puf267-wire-success"
    _seed_runtime(env_home, aid, health="codex_thread_wedged")
    fake = _write_fake(env_home, _WIRING_SUCCESS)
    cs = _wiring_session(env_home, fake, aid)

    async def _run():
        await cs.warm("sys")
        result = await cs.run_turn("hi", "sys")
        await cs.aclose()
        return result

    result = asyncio.run(_run())
    assert result.reply == "ok"
    assert cs._consecutive_thread_failures == 0
    from puffo_agent.portal.state import RuntimeState
    rs = RuntimeState.load(aid)
    assert rs.health == "ok"
    assert rs.error == ""


_WIRING_TURN_FAILED = '''\
absorb_initialize()
msg = r()
w({"jsonrpc": "2.0", "id": msg["id"], "result": {"thread": {"id": "conv_w2"}}})
msg = r()
turn_id = msg["id"]
w({"jsonrpc": "2.0", "id": turn_id, "result": None})
w({"jsonrpc": "2.0", "method": "turn/failed",
   "params": {"error": {"message": "model overloaded"}}})
while True:
    line = sys.stdin.readline()
    if not line:
        break
'''


def test_run_turn_failed_ticks_counter_via_wiring(env_home):
    """A single ``turn/failed`` without the thread-limit string must
    tick the counter (no rotation yet) via the production
    ``run_turn`` path."""
    aid = "codex-puf267-wire-fail"
    _seed_runtime(env_home, aid, health="ok")
    fake = _write_fake(env_home, _WIRING_TURN_FAILED)
    cs = _wiring_session(env_home, fake, aid)

    async def _run():
        await cs.warm("sys")
        try:
            await cs.run_turn("hi", "sys")
        except RuntimeError:
            pass
        await cs.aclose()

    asyncio.run(_run())
    assert cs._consecutive_thread_failures == 1
    # Below THRESHOLD → no rotation, no health flip yet.
    assert cs._conversation_id == "conv_w2"
    from puffo_agent.portal.state import RuntimeState
    assert RuntimeState.load(aid).health == "ok"


_WIRING_THREAD_LIMIT = '''\
absorb_initialize()
msg = r()
w({"jsonrpc": "2.0", "id": msg["id"], "result": {"thread": {"id": "conv_w3"}}})
msg = r()
turn_id = msg["id"]
w({"jsonrpc": "2.0", "id": turn_id, "result": None})
w({"jsonrpc": "2.0", "method": "turn/failed",
   "params": {"error": {"message": "agent thread limit reached"}}})
while True:
    line = sys.stdin.readline()
    if not line:
        break
'''


def test_run_turn_thread_limit_rotates_via_wiring(env_home):
    """Layer 3: a ``turn/failed`` carrying the verbatim
    ``"agent thread limit reached"`` string must rotate the
    conversation on the FIRST occurrence via the production
    ``run_turn`` path."""
    aid = "codex-puf267-wire-limit"
    _seed_runtime(env_home, aid, health="ok")
    fake = _write_fake(env_home, _WIRING_THREAD_LIMIT)
    cs = _wiring_session(env_home, fake, aid)

    async def _run():
        await cs.warm("sys")
        try:
            await cs.run_turn("hi", "sys")
        except RuntimeError:
            pass
        await cs.aclose()

    asyncio.run(_run())
    # Verbatim hit → conversation cleared + counter reset.
    assert cs._conversation_id == ""
    assert cs._consecutive_thread_failures == 0
    from puffo_agent.portal.state import RuntimeState
    rs = RuntimeState.load(aid)
    assert rs.health == "codex_thread_wedged"
    assert "thread-limit verbatim" in rs.error
