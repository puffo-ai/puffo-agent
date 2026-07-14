"""Collect each runtime's current usage-budget snapshot for the machine.

Claude Code exposes its plan budget (5h session + weekly limits) only via the
interactive ``/usage`` slash command, which ``claude -p '/usage'
--output-format json`` runs non-interactively. We parse that prose into
structured fields. Codex has no equivalent budget source today.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from ..._proc import no_window_kwargs
from ...agent.cli_bin import resolve_claude_bin
from ..state import AgentConfig, discover_agents

logger = logging.getLogger(__name__)

USAGE_PROBE_TIMEOUT_SECONDS = 60

_SESSION_RE = re.compile(
    r"Current session:\s*(\d+)%\s*used\s*[·|]\s*resets\s+(.+)", re.IGNORECASE
)
_WEEK_RE = re.compile(
    r"Current week \(([^)]+)\):\s*(\d+)%\s*used\s*[·|]\s*resets\s+(.+)", re.IGNORECASE
)


def parse_claude_usage(text: str) -> dict | None:
    """Parse ``/usage`` prose into ``{session, weekly, weekly_by_model}``.
    ``None`` when the text carries no budget line (auth error, format drift)."""
    out: dict = {}
    if m := _SESSION_RE.search(text):
        out["session"] = {"used_pct": int(m.group(1)), "resets_at": m.group(2).strip()}
    models = []
    for m in _WEEK_RE.finditer(text):
        label, pct, resets = m.group(1).strip(), int(m.group(2)), m.group(3).strip()
        entry = {"used_pct": pct, "resets_at": resets}
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
        entry = {"used_pct": w["usedPercent"], "resets_at": w.get("resetsAt")}
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
        from .reporter import get_reporter

        if parsed := parse_codex_rate_limits(get_reporter().latest_codex_rate_limits()):
            snapshot["codex"] = parsed
    return snapshot or None
