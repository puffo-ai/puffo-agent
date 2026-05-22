"""Shared parsing + invocation helpers for the ``hermes`` CLI harness.

``hermes chat --quiet -q "<prompt>"`` is the one-shot turn entry point.
Used by both adapters: ``cli-docker`` (spawn inside the container) and
``cli-local`` (spawn directly on the host).

What lives here:

* Banner / metadata regexes that filter ``--quiet`` stdout down to
  the actual model reply.
* The "no previous session" stdout signature that signals a stale
  ``--continue``.
* The ``--model`` translation (strip Claude-Code ``[1m]`` suffix,
  inject default ``anthropic/`` prefix when caller didn't pick one).
* First-turn system-prompt inlining (hermes has no ``--system`` flag).

What does NOT live here: anything that knows about how the process
gets spawned (``docker exec`` vs direct subprocess, env vars,
credentials, MCP registration). Each adapter does that itself.
"""

from __future__ import annotations

import asyncio
import re

# Exact stdout line hermes emits when ``--continue`` is passed but
# its session store has nothing to resume. Detected to clear the
# adapter-side sentinel and retry the turn fresh.
HERMES_NO_RESUME_SIGNATURE = "No previous CLI session found to continue"

# Banner / metadata lines from ``hermes --quiet``. Skip-matching
# these isolates the actual response text.
_HERMES_SESSION_ID_RE = re.compile(r"^session_id:\s*(\S+)\s*$")
_HERMES_RESUMED_SESSION_RE = re.compile(r"^↻\s*Resumed session\s+(\S+).*$")
_HERMES_MODEL_NORMALISED_RE = re.compile(r"^⚠️\s+Normalized model .*$")
# Continuation line of the "Normalized model" banner. Match a bare
# provider name followed by a period so we don't eat reply text
# that happens to start with one.
_HERMES_MODEL_NORMALISED_TAIL_RE = re.compile(r"^[a-z0-9\-]+\.$")


def hermes_model_id(model: str) -> str:
    """Translate ``runtime.model`` into ``<provider>/<model>`` form.
    Strips Claude-Code ``[1m]`` suffixes; prepends ``anthropic/`` if
    absent; empty → default.
    """
    base = (model or "").split("[", 1)[0].strip()
    if not base:
        return "anthropic/claude-opus-4-6"
    return base if "/" in base else f"anthropic/{base}"


def stitch_hermes_prompt(system_prompt: str, user_message: str) -> str:
    """First-turn system-prompt inlining. Hermes has no ``--system``
    flag, so we glue the system block onto the user message on turn
    one. Subsequent turns rely on ``--continue`` and skip this.
    """
    if not system_prompt:
        return user_message
    return f"{system_prompt}\n\n---\n\n{user_message}"


def parse_hermes_reply(stdout_text: str) -> tuple[str, str]:
    """Pull (reply, session_id) out of ``hermes chat --quiet`` stdout.
    Filter banner lines and capture session_id from whichever marker
    emits it (may be absent on fresh sessions).
    """
    session_id = ""
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
        content.append(line)
    return "\n".join(content).strip(), session_id


async def run_cmd(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    check: bool = False,
    stdin: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Spawn ``cmd``, await completion, return (rc, stdout, stderr).
    ``check=True`` raises on non-zero exit. ``stdin`` passes bytes to
    the subprocess if provided.
    """
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
