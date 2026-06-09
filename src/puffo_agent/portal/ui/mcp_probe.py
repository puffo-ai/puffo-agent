"""Enumerate the MCP server subprocesses the agents are running.

MCP servers are grandchildren of the daemon — a claude/codex session
spawns them (often via ``npx`` → ``node``) — so we walk our own process
tree, keep the ``node`` processes whose command line names an MCP
package, and attribute each to its agent via the owning session's cwd
(which lives under ``~/.puffo-agent/agents/<id>/``).

``McpProbe`` is stateful only to cache ``psutil.Process`` objects across
samples so ``cpu_percent()`` reports the delta since the last poll.
"""
from __future__ import annotations

import os
import re
from typing import Optional

try:
    import psutil
except Exception:  # pragma: no cover - psutil is a hard dep, defensive only
    psutil = None  # type: ignore[assignment]

from ..state import agents_dir

_SESSION_NAMES = {"claude.exe", "codex.exe", "claude", "codex"}


def agent_id_from_cwd(cwd: str, root: str) -> Optional[str]:
    """First path segment under ``<agents_root>/`` is the agent id."""
    low = cwd.replace("\\", "/").lower()
    root = root.replace("\\", "/").lower().rstrip("/")
    marker = root + "/"
    i = low.find(marker)
    if i < 0:
        return None
    rest = cwd.replace("\\", "/")[i + len(marker):]
    seg = rest.split("/", 1)[0]
    return seg or None


def server_name(cmdline: str) -> str:
    """Best-effort readable name for an MCP server from its command."""
    m = re.search(r"@([\w.-]+)/([\w.-]+)", cmdline)
    if m:
        scope, pkg = m.group(1), m.group(2)
        if pkg in ("mcp", "server"):
            return scope
        return pkg.replace("server-", "")
    m = re.search(r"mcp-server-([\w.-]+)", cmdline.lower())
    if m:
        return m.group(1)
    tok = cmdline.strip().split()[-1] if cmdline.strip() else "mcp"
    return tok.replace("\\", "/").split("/")[-1][:30] or "mcp"


def _is_mcp_node(name: str, cmdline_lc: str) -> bool:
    return "node" in name and (
        "mcp" in cmdline_lc or "modelcontextprotocol" in cmdline_lc
    )


class McpProbe:
    def __init__(self) -> None:
        self._cache: dict[int, "psutil.Process"] = {}
        self._name_cache: dict[str, str] = {}

    def _display_name(self, agent_id: str) -> str:
        if agent_id in self._name_cache:
            return self._name_cache[agent_id]
        name = agent_id
        try:
            from ..state import AgentConfig
            cfg = AgentConfig.load(agent_id)
            name = cfg.display_name or agent_id
        except Exception:
            pass
        self._name_cache[agent_id] = name
        return name

    def sample(self) -> list[dict]:
        if psutil is None:
            return []
        try:
            me = psutil.Process(os.getpid())
            kids = me.children(recursive=True)
        except Exception:
            return []
        root = str(agents_dir())

        sessions: dict[int, str] = {}
        for d in kids:
            try:
                if (d.name() or "").lower() in _SESSION_NAMES:
                    aid = agent_id_from_cwd(d.cwd() or "", root)
                    if aid:
                        sessions[d.pid] = aid
            except Exception:
                continue

        rows: list[dict] = []
        alive: set[int] = set()
        for p in kids:
            try:
                name = (p.name() or "").lower()
                cmd = " ".join(p.cmdline() or "")
                if not _is_mcp_node(name, cmd.lower()):
                    continue
                pid = p.pid
                alive.add(pid)
                proc = self._cache.get(pid)
                if proc is None:
                    proc = p
                    self._cache[pid] = proc
                    proc.cpu_percent(None)  # prime; first reading is 0.0
                aid = self._owner(p, sessions)
                rows.append({
                    "agent": aid,
                    "agent_name": self._display_name(aid) if aid != "?" else "?",
                    "server": server_name(cmd),
                    "pid": pid,
                    "status": p.status(),
                    "cpu": proc.cpu_percent(None),
                    "mem_mb": p.memory_info().rss / (1024 * 1024),
                })
            except Exception:
                continue

        for pid in list(self._cache):
            if pid not in alive:
                del self._cache[pid]
        rows.sort(key=lambda r: (r["agent"], r["server"]))
        return rows

    @staticmethod
    def _owner(proc: "psutil.Process", sessions: dict[int, str]) -> str:
        cur = proc
        for _ in range(12):
            try:
                if cur.pid in sessions:
                    return sessions[cur.pid]
                cur = psutil.Process(cur.ppid())
            except Exception:
                break
        return "?"
