"""Local git audit layer for the agent memory tree (M3).

Every successful semantic memory write is recorded as one commit in a
git repository living at the memory root. The repo is strictly
machine-local: this module only ever runs ``init`` / ``config`` /
``add`` / ``commit`` / ``rev-parse`` inside the memory root, and the
init step sets repo-local identity (``user.name`` / ``user.email``)
plus ``commit.gpgsign=false`` so commits are hermetic regardless of
the operator's global git configuration.

Everything degrades gracefully: a missing git binary, a failed init,
or a failed commit is logged and reported to the caller (``False`` /
``None``) — memory writes never fail because the audit layer did.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Local plumbing commands finish in milliseconds; the bound only
# guards against a wedged git process.
_GIT_TIMEOUT = 30

# Env vars that relocate git's repo/work-tree/index. A poisoned value
# in the daemon's environment could otherwise redirect an audit commit
# into an attacker-chosen repo, so they are scrubbed from every run.
_GIT_LOCATION_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def git_available() -> bool:
    """True when a ``git`` binary is on PATH."""
    return shutil.which("git") is not None


def _scrubbed_env() -> dict[str, str]:
    env = dict(os.environ)
    for var in _GIT_LOCATION_ENV:
        env.pop(var, None)
    return env


def _run_git(
    memory_root: Path, args: list[str], *, pin_repo: bool = True,
) -> subprocess.CompletedProcess | None:
    """Run one git command against the audit repo at ``memory_root``.

    The environment is scrubbed of git location overrides
    (``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE``), and — when
    ``pin_repo`` — ``--git-dir``/``--work-tree`` are passed explicitly
    so the command can only ever touch ``<root>/.git``: never an
    enclosing repo, never a repo an env var points at.
    ``pin_repo=False`` is used only for ``git init``, which must run
    before ``.git`` exists. ``None`` on any launch/timeout failure;
    callers also check ``returncode``."""
    root = Path(memory_root)
    cmd = ["git"]
    if pin_repo:
        cmd += [
            f"--git-dir={root / '.git'}",
            f"--work-tree={root}",
            "-c", f"safe.directory={root}",
        ]
    cmd += args
    try:
        return subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=_scrubbed_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("memory git %s failed to run: %s", args[:1], exc)
        return None


def ensure_memory_git(memory_root: str | Path) -> bool:
    """Initialise the local audit repo at ``memory_root`` (idempotent).

    Existing ``.git/`` → no-op True. Otherwise ``git init`` plus
    repo-local config. Returns False (degrade, logged) when git is
    unavailable or any init step fails.
    """
    memory_root = Path(memory_root)
    if (memory_root / ".git").exists():
        return True
    if not git_available():
        logger.warning(
            "git is not installed; memory changes at %s will not be "
            "audit-committed", memory_root,
        )
        return False
    # ``init`` runs before ``.git`` exists, so it can't pin --git-dir;
    # the config steps that follow do (the repo is present by then).
    steps = [
        (["init", "--quiet"], False),
        (["config", "user.name", "puffo-agent"], True),
        (["config", "user.email", "memory@puffo.local"], True),
        (["config", "commit.gpgsign", "false"], True),
    ]
    for step, pin_repo in steps:
        proc = _run_git(memory_root, step, pin_repo=pin_repo)
        if proc is None or proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() if proc else "launch failed"
            logger.warning(
                "memory git init failed at %s (%s): %s",
                memory_root, " ".join(step), detail,
            )
            return False
    return True


def format_commit_message(tool: str, paths: list[str], reason: str = "") -> str:
    """Audit commit message: subject ``memory: <tool> <logical path>``,
    body ``tool:`` line plus a ``reason:`` line only when the semantic
    caller supplied one."""
    first = paths[0] if paths else ""
    subject = f"memory: {tool} {first}".rstrip()
    body = [f"tool: {tool}"]
    if reason:
        body.append(f"reason: {reason}")
    return subject + "\n\n" + "\n".join(body) + "\n"


def commit_memory_change(
    memory_root: str | Path, paths: list[str], message: str,
) -> str | None:
    """Stage exactly ``paths`` (explicit pathspecs — stray files in the
    tree are never swept in) and commit with ``message``. Returns the
    short commit id, or ``None`` on any failure (caller decides whether
    that warrants a warning)."""
    memory_root = Path(memory_root)
    if not paths:
        return None
    # Only ever commit into our OWN audit repo. If the memory root sits
    # inside an enclosing git repo but has no ``.git`` of its own, an
    # unguarded add/commit would land in that outer repo — refuse.
    if not (memory_root / ".git").is_dir():
        logger.warning(
            "memory git commit skipped at %s: no local .git audit repo",
            memory_root,
        )
        return None
    add = _run_git(memory_root, ["add", "--", *paths])
    if add is None or add.returncode != 0:
        detail = (add.stderr or add.stdout).strip() if add else "launch failed"
        logger.warning(
            "memory git add failed at %s for %s: %s",
            memory_root, paths, detail,
        )
        return None
    commit = _run_git(memory_root, ["commit", "--quiet", "-m", message])
    if commit is None or commit.returncode != 0:
        detail = (commit.stderr or commit.stdout).strip() if commit else "launch failed"
        logger.warning(
            "memory git commit failed at %s for %s: %s",
            memory_root, paths, detail,
        )
        return None
    head = _run_git(memory_root, ["rev-parse", "--short", "HEAD"])
    if head is None or head.returncode != 0:
        return None
    return head.stdout.strip() or None
