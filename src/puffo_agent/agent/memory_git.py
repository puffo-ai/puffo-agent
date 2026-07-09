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
import re
import shutil
import subprocess
from pathlib import Path

from .memory import (
    BRIEFING_DIR,
    IMPORTS_DIR,
    NOTES_DIR,
    RECOLLECTION_DIR,
)
from .memory_errors import MemoryHistoryError

logger = logging.getLogger(__name__)

# Local plumbing commands finish in milliseconds; the bound only
# guards against a wedged git process.
_GIT_TIMEOUT = 30

# Env vars that relocate git's repo/work-tree/index. A poisoned value
# in the daemon's environment could otherwise redirect an audit commit
# into an attacker-chosen repo, so they are scrubbed from every run.
_GIT_LOCATION_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

# ── M4 read-only history query bounds ────────────────────────────────
HISTORY_DEFAULT_LIMIT = 20
HISTORY_MAX_LIMIT = 100
# include_diff attaches a diff per returned commit, so its limit is
# tighter than a metadata-only query.
HISTORY_DIFF_MAX_LIMIT = 20
HISTORY_DIFF_BYTE_CAP = 4000
# Unified-context lines for include_diff excerpts. Small on purpose —
# the diff is a bounded audit excerpt, not a full patch.
_HISTORY_DIFF_CONTEXT = 3

# The four memory areas a history query may be scoped to.
_HISTORY_SCOPES = (BRIEFING_DIR, NOTES_DIR, RECOLLECTION_DIR, IMPORTS_DIR)

# git log field/record separators (ASCII US / RS): control chars that
# never occur in a memory path or commit subject, so record parsing is
# unambiguous and no user-supplied filter value can be mistaken for a
# delimiter. The %b body is the LAST field, so the trailing --numstat
# block is peeled off it in Python (see _split_body_numstat).
_HISTORY_REC_SEP = "\x1e"
_HISTORY_FIELD_SEP = "\x1f"
_HISTORY_LOG_FORMAT = "%x1e%H%x1f%h%x1f%cI%x1f%an%x1f%s%x1f%b"

# A --numstat line: "<added>\t<deleted>\t<path>" where added/deleted are
# digits or "-" (binary files).
_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")


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


# ── M4 read-only audit history queries ───────────────────────────────
#
# These NEVER init the tree or the repo (unlike the M3 read tools' lazy
# ensure): a status/history read on an uninitialised root reports the
# truth (unavailable / not initialised) rather than materialising it.
# Only whitelisted, glued/``--``-guarded git args are ever built — no
# arbitrary flags, refs, revision ranges, or rollback are exposed.


def _history_unavailable() -> MemoryHistoryError:
    return MemoryHistoryError(
        "memory_history_unavailable",
        message="git is not installed, so the memory audit history is unavailable.",
        suggestion=(
            "Install git to enable memory history; memory reads and writes "
            "still work without it."
        ),
    )


def _history_not_initialized(root: Path) -> MemoryHistoryError:
    return MemoryHistoryError(
        "memory_history_not_initialized",
        message=f"No local memory audit repo exists yet at {root}.",
        suggestion="History appears once the first memory write is audit-committed.",
    )


def _history_read_failed(detail: str) -> MemoryHistoryError:
    return MemoryHistoryError(
        "memory_history_read_failed",
        message=f"Reading the memory audit history failed: {detail}.",
        suggestion="Retry; if it persists, check the memory git repo.",
    )


def _history_invalid_query(detail: str, suggestion: str) -> MemoryHistoryError:
    return MemoryHistoryError(
        "memory_invalid_history_query", message=detail, suggestion=suggestion,
    )


def _history_too_large(detail: str, suggestion: str) -> MemoryHistoryError:
    return MemoryHistoryError(
        "memory_history_query_too_large", message=detail, suggestion=suggestion,
    )


def _history_env_ok(memory_root: str | Path) -> Path:
    """Guard every history entry point: a git binary AND a local audit
    repo must both exist. Returns the root; raises the matching
    MemoryHistoryError otherwise. Never creates anything."""
    root = Path(memory_root)
    if not git_available():
        raise _history_unavailable()
    if not (root / ".git").is_dir():
        raise _history_not_initialized(root)
    return root


def _porcelain_paths(text: str) -> list[str]:
    """Work-tree/index paths from ``git status --porcelain`` output.
    Rename/copy entries (``old -> new``) report the new path."""
    out: list[str] = []
    for line in text.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        out.append(entry)
    return out


def history_status(
    memory_root: str | Path, path: str | None = None,
) -> dict:
    """Read-only health of the local audit repo: HEAD id, clean/dirty
    work tree, and — when ``path`` is given (a logical memory path,
    pre-validated by the caller) — whether that path is tracked and
    when it last changed. Operates on committed history and tolerates a
    dirty work tree (dirtiness is reported via ``clean`` /
    ``uncommitted_paths``, never raised)."""
    root = _history_env_ok(memory_root)
    # HEAD short id — None (not an error) on an unborn/empty repo, where
    # ``--verify --quiet`` exits nonzero with empty output.
    head = _run_git(
        root, ["rev-parse", "--verify", "--quiet", "--short", "HEAD"],
    )
    if head is None:
        raise _history_read_failed("git rev-parse failed to run")
    if head.returncode == 0:
        head_id = head.stdout.strip() or None
    elif not head.stdout.strip():
        head_id = None
    else:
        raise _history_read_failed(
            (head.stderr or head.stdout).strip() or "git rev-parse failed"
        )

    status = _run_git(root, ["status", "--porcelain"])
    if status is None or status.returncode != 0:
        detail = (
            (status.stderr or status.stdout).strip()
            if status else "git status failed to run"
        )
        raise _history_read_failed(detail or "git status failed")
    uncommitted = _porcelain_paths(status.stdout)

    result: dict = {
        "git_enabled": True,
        "repo_initialized": True,
        "head_commit_id": head_id,
        "clean": not uncommitted,
        "uncommitted_paths": uncommitted,
    }
    if path is not None:
        result["path"] = path
        if head_id is None:
            # No commits yet: nothing is tracked (a bare `git log` on an
            # unborn HEAD would exit nonzero — treat as not-tracked).
            result.update({
                "path_tracked": False,
                "last_changed_commit_id": None,
                "last_changed_at": None,
            })
        else:
            log = _run_git(
                root, ["log", "-1", "--format=%h%x1f%cI", "--", path],
            )
            if log is None or log.returncode != 0:
                detail = (
                    (log.stderr or log.stdout).strip()
                    if log else "git log failed to run"
                )
                raise _history_read_failed(detail or "git log failed")
            out = log.stdout.strip()
            if out:
                short, _, iso = out.partition(_HISTORY_FIELD_SEP)
                result.update({
                    "path_tracked": True,
                    "last_changed_commit_id": short or None,
                    "last_changed_at": iso or None,
                })
            else:
                result.update({
                    "path_tracked": False,
                    "last_changed_commit_id": None,
                    "last_changed_at": None,
                })
    return result


def _parse_commit_body(body: str) -> tuple[str | None, str | None]:
    """Pull ``operation`` (from the ``tool:`` line) and ``reason`` (from
    the ``reason:`` line) out of a commit body written by
    ``format_commit_message``. Either may be absent."""
    operation: str | None = None
    reason: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if operation is None and stripped.startswith("tool:"):
            operation = stripped[len("tool:"):].strip() or None
        elif reason is None and stripped.startswith("reason:"):
            reason = stripped[len("reason:"):].strip() or None
    return operation, reason


def _split_body_numstat(rest: str) -> tuple[str, list[str], int, int]:
    """Peel the trailing ``--numstat`` block off the last (%b) field of
    a log record. Returns ``(body, changed_paths, insertions,
    deletions)``. The numstat lines are contiguous at the tail, so we
    walk backward collecting rows that match ``_NUMSTAT_RE`` until the
    first non-numstat line — everything above (minus trailing blanks) is
    the commit body."""
    lines = rest.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    insertions = 0
    deletions = 0
    changed: list[str] = []
    i = len(lines)
    while i > 0:
        m = _NUMSTAT_RE.match(lines[i - 1])
        if not m:
            break
        i -= 1
        added, removed, pth = m.group(1), m.group(2), m.group(3)
        changed.append(pth)
        if added != "-":
            insertions += int(added)
        if removed != "-":
            deletions += int(removed)
    changed.reverse()
    body_lines = lines[:i]
    while body_lines and body_lines[-1] == "":
        body_lines.pop()
    return "\n".join(body_lines), changed, insertions, deletions


def _parse_history_records(output: str) -> list[dict]:
    """Parse ``git log`` record-separated output into commit dicts.
    ``_full`` (the full %H hash) is kept for a follow-up diff and popped
    before the result is returned to the caller."""
    commits: list[dict] = []
    for raw in output.split(_HISTORY_REC_SEP):
        if not raw.strip():
            continue
        fields = raw.split(_HISTORY_FIELD_SEP, 5)
        if len(fields) < 6:
            continue
        full, short, iso, actor, subject, rest = fields
        body, changed, insertions, deletions = _split_body_numstat(rest)
        operation, reason = _parse_commit_body(body)
        commits.append({
            "_full": full,
            "commit_id": short,
            "time": iso,
            "actor": actor,
            "operation": operation,
            "reason": reason,
            "message": subject,
            "changed_paths": changed,
            "summary": {
                "files_changed": len(changed),
                "insertions": insertions,
                "deletions": deletions,
            },
        })
    return commits


def _history_diff(
    root: Path, full_hash: str, pathspec: list[str],
) -> tuple[str, bool]:
    """Bounded diff excerpt for one commit, scoped to the same
    pathspec. ``full_hash`` is git-generated (%H), never user input.
    Byte-capped at ``HISTORY_DIFF_BYTE_CAP``; returns ``(diff,
    truncated)``. A diff we can't read degrades to an empty excerpt
    rather than failing the whole query."""
    args = [
        "show", "--format=", f"--unified={_HISTORY_DIFF_CONTEXT}",
        full_hash, "--", *pathspec,
    ]
    proc = _run_git(root, args)
    if proc is None or proc.returncode != 0:
        return "", False
    data = proc.stdout.encode("utf-8")
    if len(data) > HISTORY_DIFF_BYTE_CAP:
        return data[:HISTORY_DIFF_BYTE_CAP].decode("utf-8", errors="ignore"), True
    return proc.stdout, False


def _normalize_history_filter(value: object, name: str) -> str | None:
    """Empty/absent → None (no filter); a non-empty string passes
    through; anything else is an invalid query."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _history_invalid_query(
            f"{name} must be a string.",
            f"Pass {name} as a string, or omit it.",
        )
    return value


def query_history(
    memory_root: str | Path,
    *,
    path: str | None = None,
    scopes: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    actor: str | None = None,
    operation: str | None = None,
    query: str | None = None,
    limit: int = HISTORY_DEFAULT_LIMIT,
    include_diff: bool = False,
) -> dict:
    """Bounded read-only audit query over the local git history — NOT a
    ``git log`` passthrough. Only the whitelisted filters are honoured,
    each built as a glued option (``--since=…``) or a ``--``-guarded
    pathspec, so no value can be reinterpreted as a git flag, ref, or
    revision range. ``operation`` (substring of the recorded ``tool:``)
    and ``query`` (case-insensitive substring over subject + reason +
    changed paths — never a diff grep) are applied as Python
    post-filters. Returns ``{entries, truncated}``; ``include_diff``
    attaches a byte-capped ``diff`` excerpt per entry."""
    root = _history_env_ok(memory_root)

    # -- arg validation (memory_invalid_history_query) -----------------
    since = _normalize_history_filter(since, "since")
    until = _normalize_history_filter(until, "until")
    actor = _normalize_history_filter(actor, "actor")
    operation = _normalize_history_filter(operation, "operation")
    query = _normalize_history_filter(query, "query")
    if path is not None and not isinstance(path, str):
        raise _history_invalid_query(
            "path must be a string.",
            "Pass a logical memory path, or omit it.",
        )
    path = path or None
    if scopes is not None:
        if not isinstance(scopes, (list, tuple)):
            raise _history_invalid_query(
                "scopes must be a list of memory areas.",
                "Pass scopes like ['briefing','notes'], or omit it.",
            )
        for sc in scopes:
            if sc not in _HISTORY_SCOPES:
                raise _history_invalid_query(
                    f"unknown scope {sc!r}.",
                    "Pick scopes from: briefing, notes, recollection, imports.",
                )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise _history_invalid_query(
            f"limit must be a positive integer, got {limit!r}.",
            f"Use 1 <= limit <= {HISTORY_MAX_LIMIT}.",
        )

    # -- size bounds (memory_history_query_too_large) ------------------
    if limit > HISTORY_MAX_LIMIT:
        raise _history_too_large(
            f"limit {limit} exceeds the maximum {HISTORY_MAX_LIMIT}.",
            f"Use limit <= {HISTORY_MAX_LIMIT}.",
        )
    if include_diff and limit > HISTORY_DIFF_MAX_LIMIT:
        raise _history_too_large(
            f"include_diff caps limit at {HISTORY_DIFF_MAX_LIMIT}; got {limit}.",
            f"Use limit <= {HISTORY_DIFF_MAX_LIMIT} with include_diff, or "
            "drop include_diff.",
        )

    # -- pathspec: always after `--`, one entry per scope or the path --
    if path is not None:
        pathspec = [path]
    elif scopes:
        pathspec = [f"{sc}/" for sc in scopes]
    else:
        pathspec = []

    # -- git log: glued options only; no ref/revision-range ever built.
    # Over-fetch a bounded window so the Python post-filters can drop
    # commits and still fill `limit` / detect truncation.
    fetch = HISTORY_MAX_LIMIT + 1
    args = [
        "log", f"--max-count={fetch}", f"--format={_HISTORY_LOG_FORMAT}",
        "--numstat",
    ]
    if since is not None:
        args.append(f"--since={since}")
    if until is not None:
        args.append(f"--until={until}")
    if actor is not None:
        args.append(f"--author={actor}")
    args.append("--")
    args.extend(pathspec)

    proc = _run_git(root, args)
    if proc is None or proc.returncode != 0:
        # An unborn/empty repo makes `git log` exit nonzero with a
        # "does not have any commits yet" message — that's simply no
        # history, not a read failure.
        stderr = (proc.stderr if proc else "") or ""
        if proc is not None and "does not have any commits" in stderr:
            return {"entries": [], "truncated": False}
        detail = (
            (proc.stderr or proc.stdout).strip()
            if proc else "git log failed to run"
        )
        raise _history_read_failed(detail or "git log failed")

    commits = _parse_history_records(proc.stdout)

    def _passes(c: dict) -> bool:
        if operation is not None:
            if operation.lower() not in (c["operation"] or "").lower():
                return False
        if query is not None:
            haystack = " ".join([
                c["message"] or "",
                c["reason"] or "",
                " ".join(c["changed_paths"]),
            ]).lower()
            if query.lower() not in haystack:
                return False
        return True

    filtered = [c for c in commits if _passes(c)]
    truncated = len(filtered) > limit
    entries = filtered[:limit]

    if include_diff:
        for entry in entries:
            entry["diff"], entry["diff_truncated"] = _history_diff(
                root, entry["_full"], pathspec,
            )
    for entry in entries:
        entry.pop("_full", None)

    return {"entries": entries, "truncated": truncated}
