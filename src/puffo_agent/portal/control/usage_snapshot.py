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
from ...agent._usage_markers import DRAINED_RUNTIME_ERROR
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


def agent_harnesses() -> dict[str, str]:
    """``{agent_id: harness}`` for this machine's agents."""
    out: dict[str, str] = {}
    for agent_id in discover_agents():
        try:
            out[agent_id] = AgentConfig.load(agent_id).runtime.harness or "claude-code"
        except Exception:  # noqa: BLE001 — a broken agent.yml shouldn't block the rest
            continue
    return out


def machine_harnesses() -> set[str]:
    """Harnesses in use by this machine's agents (drives which /usage to probe)."""
    return set(agent_harnesses().values())


def drained_harnesses(snapshot: dict) -> dict[str, int | None]:
    """``{harness: resets_at}`` per spent harness. The reset is the time
    at which EVERY exhausted window has cleared (their max) — the soonest
    reset would promise capacity while another window is still spent —
    and ``None`` when any exhausted window has no known reset.
    Weekly spent counts even with session headroom."""
    out: dict[str, int | None] = {}
    for harness, budgets in (snapshot or {}).items():
        if not isinstance(budgets, dict):
            continue
        resets: list[int] = []
        spent = False
        all_known = True
        for window in ("session", "weekly"):
            entry = budgets.get(window)
            if not isinstance(entry, dict):
                continue
            if (entry.get("used_pct") or 0) < 100:
                continue
            spent = True
            if isinstance(entry.get("resets_at"), int):
                resets.append(entry["resets_at"])
            else:
                all_known = False
        if spent:
            out[harness] = max(resets) if resets and all_known else None
    return out


_live_workers_provider = None


def set_live_workers(provider) -> None:
    """Daemon registers ``lambda: self.workers`` so the snapshot flip can
    reach in-memory worker state (the worker heartbeat overwrites a
    disk-only flip within seconds, and the status reporter reads memory)."""
    global _live_workers_provider
    _live_workers_provider = provider


def _live_workers() -> dict:
    if _live_workers_provider is None:
        return {}
    try:
        return dict(_live_workers_provider())
    except Exception:  # noqa: BLE001 — fall back to the on-disk path
        return {}


def _apply_to_live_worker(worker, agent_id: str, spent_reset) -> None:
    """In-memory flip through the worker's own ENTER/CLEAR so it survives
    the heartbeat, reaches the status reporter, and DMs once per episode.
    ``spent_reset`` is ``(True, resets_at)`` or ``(False, None)``."""
    is_spent, resets_at = spent_reset
    if is_spent:
        if worker.runtime.health in ("ok", "unknown", ""):
            worker._enter_drained(agent_id, resets_at)
    elif worker.runtime.health == "drained":
        worker._clear_drained(worker.runtime, agent_id, logger)


def apply_drained_health(snapshot: dict) -> None:
    """Snapshot → ``runtime.health``: spent → ``drained``, recovered → ``ok``.
    Agents with a live worker are updated in memory (worker ENTER/CLEAR);
    the rest through the on-disk state. Other reds untouched."""
    from ..state import RuntimeState

    spent = drained_harnesses(snapshot)
    live = _live_workers()
    for agent_id, harness in agent_harnesses().items():
        if harness not in (snapshot or {}):
            continue
        worker = live.get(agent_id)
        if worker is not None:
            try:
                _apply_to_live_worker(
                    worker, agent_id, (harness in spent, spent.get(harness)),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "usage: live drained flip failed for %s: %s", agent_id, exc,
                )
            continue
        try:
            rs = RuntimeState.load(agent_id)
        except Exception:  # noqa: BLE001
            continue
        if rs is None:
            continue
        if harness in spent:
            if rs.health in ("ok", "unknown", ""):
                rs.health = "drained"
                rs.error = DRAINED_RUNTIME_ERROR
            else:
                continue
        elif rs.health == "drained":
            rs.health = "ok"
            rs.error = ""
        else:
            continue
        try:
            rs.save(agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("usage: drained flip failed for %s: %s", agent_id, exc)


async def predicted_reset_epoch(harness: str) -> int | None:
    """Probe /usage and return the spent window's reset epoch for
    ``harness``, or ``None``. Used to put a predicted reset time in the
    drained DM when the error body carried none."""
    try:
        snapshot = await collect_usage_snapshot(Path.home())
    except Exception:  # noqa: BLE001 — the DM matters more than its timestamp
        return None
    if not snapshot:
        return None
    return drained_harnesses(snapshot).get(harness)


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
