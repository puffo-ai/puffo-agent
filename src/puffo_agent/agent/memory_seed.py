"""First-boot seeding of an agent's memory from a git remote.

A cloud agent is created with an identity but an empty brain: the Hub mints its
keys, and its persona and standing memory live somewhere else. This closes that
gap — on start, an agent whose config names a ``memory_remote`` clones it once.

**It seeds; it never syncs.** The clone happens only when the memory tree is
*empty*. An agent that has written anything is left alone, unconditionally. That
single rule is what makes this safe to run on every boot: there is no state in
which it can overwrite a note the agent wrote, and no flag anyone has to
remember to unset.

The remote is expected to hold one directory per agent::

    <repo>/<name>/memory/…      ← copied into the agent's memory tree

so one repo serves a fleet, and `tools/seed_cloud_agent.py` publishes into it.

**Memory only — `profile.md` is deliberately not seeded here.** The agent store
owns it: `lifecycle._deliver_pending_config` byte-compares `profile.md` against
the stored config and materialises the stored copy on resume, so anything
written into the sandbox is reverted the first time the agent idles. A profile
must be set where the store will keep it — the create dialog's PROFILE field, or
`PUT /agents/{slug}`. Memory has no such competing writer, which is why this
works for memory and would not for a profile.

Failure is never fatal. An unreachable remote, a missing directory, or a broken
key leaves the agent running with an empty memory and a warning — degraded, not
dead, which is the right trade for a convenience that runs at boot.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .._proc import no_window_kwargs

logger = logging.getLogger(__name__)


def memory_seed_name(agent_id: str) -> str:
    """`desk-6332-b73d5e96` -> `desk`; `planner-2d35d73e` -> `planner`.

    The remote is keyed by the name a human uses, not the slug: a cloud agent
    gets a fresh slug on every create, and its memory should follow the agent
    rather than the identity it happens to be wearing. Both slug shapes in the
    fleet are handled, and an already-short name passes through.
    """
    import re

    name = re.sub(r"-\d{3,}-[0-9a-f]{6,}$", "", agent_id)
    name = re.sub(r"-[0-9a-f]{8,}$", "", name)
    return name or agent_id

#: Names that do not count as memory content when deciding "is this empty?".
#: `.git` is the agent's own local history; the tree scaffolding is created
#: unconditionally by `ensure_memory_tree` and says nothing about content.
_NOT_CONTENT = {".git", ".gitkeep", ".DS_Store"}

_CLONE_TIMEOUT_S = 120


def memory_is_empty(memory_root: Path) -> bool:
    """True when the tree holds no content the agent would miss."""
    if not memory_root.is_dir():
        return True
    for path in memory_root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in _NOT_CONTENT or ".git" in path.parts:
            continue
        return False
    return True


def _clone(remote: str, into: Path) -> None:
    subprocess.run(
        ["git", "clone", "--depth", "1", "-q", remote, str(into)],
        check=True,
        capture_output=True,
        timeout=_CLONE_TIMEOUT_S,
        **no_window_kwargs(),
    )


def seed_from_remote(
    *,
    memory_root: Path,
    remote: str,
    name: str,
) -> str:
    """Seed this agent's memory from ``remote``, once.

    Returns a short status for the caller's log — ``"seeded"``,
    ``"skip:has-memory"``, ``"skip:no-such-agent"``, ``"skip:no-remote"``, or
    ``"failed:<reason>"``. Never raises: a boot-time convenience must not be
    able to stop an agent from starting.
    """
    if not remote:
        return "skip:no-remote"
    memory_root = Path(memory_root)
    if not memory_is_empty(memory_root):
        return "skip:has-memory"

    tmp = Path(tempfile.mkdtemp(prefix="puffo-memory-seed-"))
    try:
        try:
            _clone(remote, tmp / "repo")
        except subprocess.TimeoutExpired:
            return "failed:clone-timeout"
        except subprocess.CalledProcessError as exc:
            # stderr can carry a host key or an auth hint; keep it short and
            # out of the message body so a token never lands in a log line.
            tail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            return f"failed:clone ({tail[-1][:120] if tail else 'no output'})"

        src = tmp / "repo" / name
        if not src.is_dir():
            return "skip:no-such-agent"

        copied = 0
        src_memory = src / "memory"
        if src_memory.is_dir():
            memory_root.mkdir(parents=True, exist_ok=True)
            for item in src_memory.iterdir():
                if item.name in _NOT_CONTENT:
                    continue
                dest = memory_root / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
                copied += 1

        return "seeded" if copied else "skip:nothing-to-copy"
    except OSError as exc:
        return f"failed:{type(exc).__name__}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
