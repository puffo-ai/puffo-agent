"""Shared parsing + invocation helpers for the ``hermes`` CLI harness.

Used by ``cli-docker`` (spawn inside container) and ``cli-local``
(spawn directly on host). Knows nothing about how the process is
launched; each adapter owns its own subprocess + env + IPC.
"""

from __future__ import annotations

import asyncio
import re

HERMES_NO_RESUME_SIGNATURE = "No previous CLI session found to continue"

_HERMES_SESSION_ID_RE = re.compile(r"^session_id:\s*(\S+)\s*$")
_HERMES_RESUMED_SESSION_RE = re.compile(r"^↻\s*Resumed session\s+(\S+).*$")
_HERMES_MODEL_NORMALISED_RE = re.compile(r"^⚠️\s+Normalized model .*$")
# Bare provider name + period — Normalised-model banner continuation.
_HERMES_MODEL_NORMALISED_TAIL_RE = re.compile(r"^[a-z0-9\-]+\.$")
_HERMES_TOOL_REPAIR_RE = re.compile(
    r"^🔧\s+Auto-repaired tool name:\s*'([^']+)'\s*->\s*'([^']+)'\s*$"
)


def hermes_model_id(model: str) -> str:
    """``runtime.model`` → ``<provider>/<model>``. Strips Claude-Code
    ``[1m]`` suffix; prepends ``anthropic/`` if no provider given."""
    base = (model or "").split("[", 1)[0].strip()
    if not base:
        return "anthropic/claude-opus-4-6"
    return base if "/" in base else f"anthropic/{base}"


def stitch_hermes_prompt(system_prompt: str, user_message: str) -> str:
    """Hermes has no ``--system`` flag; inline above the user message
    on the first turn. ``--continue`` carries it forward."""
    if not system_prompt:
        return user_message
    return f"{system_prompt}\n\n---\n\n{user_message}"


def parse_hermes_reply(stdout_text: str) -> tuple[str, str, list[str]]:
    """Return (reply, session_id, tool_calls). ``tool_calls`` is the
    list of pre-repair tool names from any 🔧 banners — partial
    signal only; the authoritative source is the MCP log written by
    the puffo MCP server.
    """
    session_id = ""
    tool_calls: list[str] = []
    content: list[str] = []
    for line in stdout_text.splitlines():
        m = _HERMES_SESSION_ID_RE.match(line)
        if m:
            session_id = m.group(1)
            continue
        m = _HERMES_RESUMED_SESSION_RE.match(line)
        if m:
            session_id = session_id or m.group(1)
            continue
        if _HERMES_MODEL_NORMALISED_RE.match(line):
            continue
        if _HERMES_MODEL_NORMALISED_TAIL_RE.match(line):
            continue
        m = _HERMES_TOOL_REPAIR_RE.match(line)
        if m:
            tool_calls.append(m.group(1))
            continue
        content.append(line)
    return "\n".join(content).strip(), session_id, tool_calls


async def run_cmd(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    check: bool = False,
    stdin: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Spawn ``cmd``, return (rc, stdout, stderr). ``check`` raises
    on non-zero exit; ``stdin`` pipes bytes if given."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(stdin)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stderr: {stderr.decode('utf-8', errors='replace').strip()[:500]}"
        )
    return proc.returncode, stdout, stderr
