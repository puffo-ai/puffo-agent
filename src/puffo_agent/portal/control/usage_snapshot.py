"""Collect each runtime's current usage-budget snapshot for the machine.

Both harnesses are probed on demand — the daemon runs this on a slow cadence
(and on the ``refresh_usage`` command). Claude Code exposes its plan budget only
via the interactive ``/usage`` slash command, which ``claude -p '/usage'
--output-format json`` runs non-interactively; we parse that prose. Codex only
emits its budget (an ``account/rateLimits/updated`` frame) *after a turn*, so we
spawn a throwaway app-server and run one trivial turn to read it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from ..._proc import no_window_kwargs
from ...agent.cli_bin import resolve_claude_bin, resolve_codex_bin
from ..state import AgentConfig, discover_agents

logger = logging.getLogger(__name__)

USAGE_PROBE_TIMEOUT_SECONDS = 60
# Wider ceiling than claude's: codex pays a cold app-server spawn plus a turn.
CODEX_PROBE_TIMEOUT_SECONDS = 90

_SESSION_RE = re.compile(
    r"Current session:\s*(\d+)%\s*used\s*[·|]\s*resets\s+(.+)", re.IGNORECASE
)
_WEEK_RE = re.compile(
    r"Current week \(([^)]+)\):\s*(\d+)%\s*used\s*[·|]\s*resets\s+(.+)", re.IGNORECASE
)


_RESETS_RE = re.compile(
    r"^(\w{3})\s+(\d{1,2}),\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def _claude_resets_to_epoch(prose: str) -> int | None:
    """Claude's ``/usage`` reset time is a year-less, named-tz phrase like
    ``Jul 20, 5pm (America/Los_Angeles)``. Parse to a unix epoch (matching
    codex's ``resetsAt``); ``None`` on any format/tz miss so the caller omits
    the field rather than shipping an unparseable string."""
    m = _RESETS_RE.match(prose.strip())
    if not m:
        return None
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        month = _MONTHS[m.group(1).lower()]
        day, hour = int(m.group(2)), int(m.group(3))
        minute = int(m.group(4) or 0)
        if m.group(5).lower() == "pm" and hour != 12:
            hour += 12
        elif m.group(5).lower() == "am" and hour == 12:
            hour = 0
        tz = ZoneInfo(m.group(6).strip())
        now = datetime.now(tz)
        dt = datetime(now.year, month, day, hour, minute, tzinfo=tz)
        # Year-less: a reset that lands in the past means it's next year
        # (weekly/session windows only ever reset in the near future).
        if dt.timestamp() < now.timestamp() - 86400:
            dt = dt.replace(year=now.year + 1)
        return int(dt.timestamp())
    except Exception:  # noqa: BLE001 — unknown tz / format drift → omit the field
        return None


def _budget_entry(used_pct: int, resets_prose: str) -> dict:
    entry: dict = {"used_pct": used_pct}
    epoch = _claude_resets_to_epoch(resets_prose)
    if epoch is not None:
        entry["resets_at"] = epoch
    return entry


def parse_claude_usage(text: str) -> dict | None:
    """Parse ``/usage`` prose into ``{session, weekly, weekly_by_model}``.
    ``None`` when the text carries no budget line (auth error, format drift)."""
    out: dict = {}
    if m := _SESSION_RE.search(text):
        out["session"] = _budget_entry(int(m.group(1)), m.group(2))
    models = []
    for m in _WEEK_RE.finditer(text):
        label = m.group(1).strip()
        entry = _budget_entry(int(m.group(2)), m.group(3))
        if label.lower() == "all models":
            out["weekly"] = entry
        else:
            models.append({"model": label, **entry})
    if models:
        out["weekly_by_model"] = models
    return out or None


def parse_codex_rate_limits(raw: dict | None) -> dict | None:
    """Normalise a codex ``account/rateLimits/updated`` payload into the same
    ``{session, weekly}`` shape as claude-code. primary/secondary carry the
    window, so classify by ``windowDurationMins`` (~300 = 5h, ~10080 = weekly)
    rather than their slot. ``resets_at`` stays a unix epoch."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for slot in ("primary", "secondary"):
        w = raw.get(slot)
        if not isinstance(w, dict) or "usedPercent" not in w:
            continue
        entry = {"used_pct": w["usedPercent"]}
        if isinstance(w.get("resetsAt"), int):
            entry["resets_at"] = w["resetsAt"]
        mins = w.get("windowDurationMins") or 0
        out["session" if mins <= 1440 else "weekly"] = entry
    return out or None


def machine_harnesses() -> set[str]:
    """Harnesses in use by this machine's agents (drives which /usage to probe)."""
    harnesses = set()
    for agent_id in discover_agents():
        try:
            harnesses.add(AgentConfig.load(agent_id).runtime.harness or "claude-code")
        except Exception:  # noqa: BLE001 — a broken agent.yml shouldn't block the rest
            continue
    return harnesses


async def _run_claude_usage(claude_bin: str, host_home: Path) -> str | None:
    # HOME=host_home so claude reads the operator's login + computes /usage from
    # the operator's local sessions (mirrors credential_refresh's probe).
    env = {**os.environ, "HOME": str(host_home)}
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "-p", "/usage", "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(host_home),
            **no_window_kwargs(),
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=USAGE_PROBE_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
        logger.debug("usage: claude /usage probe failed: %s", exc)
        return None
    try:
        return json.loads(stdout.decode("utf-8", "replace")).get("result")
    except (ValueError, AttributeError):
        return None


def _extract_thread_id(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    for k in ("threadId", "thread_id", "conversationId", "id"):
        if result.get(k):
            return str(result[k])
    thread = result.get("thread")
    if isinstance(thread, dict):
        return thread.get("id") or thread.get("threadId")
    if isinstance(thread, str):
        return thread
    return None


async def _drive_codex_probe(proc) -> dict | None:
    """Run the JSON-RPC handshake + one trivial turn against a codex app-server
    and return the ``rateLimits`` payload from the post-turn frame. Split from
    the spawn so tests can drive it with a fake process."""

    async def send(obj: dict) -> None:
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        await proc.stdin.drain()

    await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "clientInfo": {"name": "puffo-agent", "version": "0"},
        "capabilities": {}, "protocolVersion": "2025-06-18"}})
    await send({"jsonrpc": "2.0", "id": 2, "method": "thread/start", "params": {}})

    turn_sent = False
    while True:
        line = await proc.stdout.readline()
        if not line:
            return None
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        method = (msg.get("method") or "").replace(".", "/").lower()
        if method.startswith("account/ratelimits/updated"):
            return (msg.get("params") or {}).get("rateLimits")
        # thread/start ACK carries the id; fire the throwaway turn that makes
        # codex emit the budget frame.
        if msg.get("id") == 2 and "result" in msg and not turn_sent:
            thread_id = _extract_thread_id(msg["result"])
            if not thread_id:
                return None
            await send({"jsonrpc": "2.0", "id": 3, "method": "turn/start", "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "ignore this message"}]}})
            turn_sent = True


async def _probe_codex_rate_limits(codex_bin: str, host_home: Path) -> dict | None:
    """Spawn a throwaway codex app-server, run one trivial turn, and capture the
    account budget. Costs one tiny turn (codex has no turn-free budget source).
    ``None`` on any spawn/timeout/parse failure so the caller can fall back."""
    try:
        proc = await asyncio.create_subprocess_exec(
            codex_bin, "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HOME": str(host_home)},
            cwd=str(host_home),
            **no_window_kwargs(),
        )
    except (FileNotFoundError, OSError) as exc:
        logger.debug("usage: codex app-server spawn failed: %s", exc)
        return None
    try:
        return await asyncio.wait_for(
            _drive_codex_probe(proc), timeout=CODEX_PROBE_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, OSError) as exc:
        logger.debug("usage: codex probe failed: %s", exc)
        return None
    finally:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


async def collect_usage_snapshot(host_home: Path) -> dict | None:
    """Per-harness budget snapshot for the machine, or ``None`` if nothing to
    report. Shape: ``{"claude-code": {session, weekly, ...}}``."""
    harnesses = machine_harnesses()
    snapshot: dict = {}
    if "claude-code" in harnesses:
        claude_bin = resolve_claude_bin()
        if claude_bin and (text := await _run_claude_usage(claude_bin, host_home)):
            if parsed := parse_claude_usage(text):
                snapshot["claude-code"] = parsed
    if "codex" in harnesses:
        raw = None
        if codex_bin := resolve_codex_bin():
            raw = await _probe_codex_rate_limits(codex_bin, host_home)
        if raw is None:
            # Probe failed — fall back to the last frame a live codex agent saw.
            from .reporter import get_reporter

            raw = get_reporter().latest_codex_rate_limits()
        if parsed := parse_codex_rate_limits(raw):
            snapshot["codex"] = parsed
    return snapshot or None
