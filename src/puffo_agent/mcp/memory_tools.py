"""Semantic memory MCP tools (M3) over the M2 ``MemoryStore``.

Ten tools: six semantic writes (``create_note``, ``patch_note``,
``append_note``, ``create_briefing_topic``, ``patch_briefing_topic``,
``append_recollection``) and four read/search tools
(``read_memory_file``, ``read_memory_files``, ``search_memory``,
``search_imports``). Nine are agent-facing; ``append_recollection``
registers only under the maintenance scope (recollection/ is
daemon-owned), so the agent-facing server exposes the other nine. The
agent works with memory
concepts — notes, briefing topics, recollections — never physical
paths; semantic names are normalized onto logical paths
(``notes/<name>.md``, ``briefing/<name>.md``, dated
``recollection/YYYY/MM/YYYY-MM-DD.md``) and every write goes through
the M2 store, which keeps path grammar, scope rules, size limits, and
atomicity centralized.

Every successful write is committed to the LOCAL git repo at the
memory root (``memory_git``), with the caller's ``reason`` recorded in
the commit body. Writes return the doc's result envelope::

    {ok, tool, changed, paths, commit_id,
     post_effects: {briefing_rebuilt, provider_reload}, warnings}

``provider_reload`` mapping for the fat agent — which has no hot
in-session provider reload; a "reload" means the M1
``refresh_agent.flag`` was written, so the worker rebuilds provider
prompt artifacts (CLAUDE.md / AGENTS.md) on the next batch and the
provider picks them up at next spawn/turn:

- ``"not_needed"`` — non-briefing write, or ``changed: false``;
- ``"requested"`` — briefing changed and the refresh flag was written;
- ``"failed"`` — briefing changed but the flag write failed (or no
  workspace is configured): ``ok`` stays true and a
  ``memory_provider_reload_failed`` warning is attached — post-effect
  failures never masquerade as write failures.

``briefing_rebuilt`` is true iff the write touched ``briefing/`` and
changed it (the store revalidated the compiled-total budget before the
write committed, and the rebuild was triggered via the refresh flag).

Expected failures (validation, scope, size, patch mismatches) surface
as structured tool errors whose text is the JSON envelope
``{ok: false, error: {code, message, operation, path, suggestion,
causes}}`` with M3 error codes; truly unexpected exceptions propagate
as plain runtime errors.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date as date_type, datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..agent import memory_git
from ..agent.memory import (
    BRIEFING_DIR,
    NOTES_DIR,
    RECOLLECTION_DIR,
    ensure_memory_tree,
    request_prompt_refresh,
    safe_memory_scope,
)
from ..agent.memory_errors import MemoryHistoryError, MemoryStoreError
from ..agent.memory_store import (
    MemoryStore,
)

logger = logging.getLogger(__name__)

NAME_MAX_LENGTH = 100
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 50
SEARCH_SNIPPET_LIMIT = 200
SEARCH_PER_FILE_LIMIT = 3
SEARCH_FILE_READ_LIMIT = 128 * 1024
SEARCH_TOTAL_READ_LIMIT = 4 * 1024 * 1024

# search_memory scans these, in this fixed order (deterministic
# results). imports/ is deliberately absent — it is searchable only
# via search_imports.
_SEARCH_SCOPES = (BRIEFING_DIR, NOTES_DIR, RECOLLECTION_DIR)

_NAME_SUGGESTION = (
    "Use a short flat name like 'puffo-memory-mcp': letters, digits, "
    "dots, dashes; no slashes, no leading dot, at most "
    f"{NAME_MAX_LENGTH} characters."
)


@dataclass
class MemoryToolsConfig:
    """Config for the M3 memory tools.

    ``maintenance`` is store-level and NOT agent-selectable:
    ``build_server()`` always constructs it as False; the flag exists
    so a future daemon-maintenance context can flip it and unlock
    ``recollection/`` writes.
    """

    memory_root: str
    workspace: str = ""
    maintenance: bool = False


# ── error envelopes ──────────────────────────────────────────────────


def _tool_error(
    *,
    code: str,
    message: str,
    operation: str,
    path: str,
    suggestion: str,
    causes: list[dict],
    size: int | None = None,
    limit: int | None = None,
) -> ToolError:
    """Structured tool error: the text IS the JSON envelope, so the
    agent sees the M3 error shape instead of a transport failure."""
    err: dict = {
        "code": code,
        "message": message,
        "operation": operation,
        "path": path,
    }
    if size is not None:
        err["size"] = size
    if limit is not None:
        err["limit"] = limit
    err["suggestion"] = suggestion
    err["causes"] = causes
    return ToolError(json.dumps({"ok": False, "error": err}))


def _args_error(
    operation: str,
    message: str,
    suggestion: str,
    *,
    code: str = "memory_invalid_arguments",
    path: str = "",
) -> ToolError:
    return _tool_error(
        code=code,
        message=message,
        operation=operation,
        path=path,
        suggestion=suggestion,
        causes=[{"layer": "memory_tools", "code": code, "message": message}],
    )


def _store_error(operation: str, exc: MemoryStoreError) -> ToolError:
    return _tool_error(
        code=exc.code,
        message=f"{operation} failed for {exc.path}: {exc.code}.",
        operation=operation,
        path=exc.path,
        suggestion=exc.suggestion,
        size=exc.size,
        limit=exc.limit,
        causes=[
            {"layer": "memory_store", "code": exc.code, "message": str(exc)},
        ],
    )


def _history_error(operation: str, exc: MemoryHistoryError) -> ToolError:
    """Map an M4 history-query error to the same structured envelope as
    ``_store_error``, tagged with a ``memory_git`` cause layer. History
    errors carry no path, so ``path`` is empty."""
    return _tool_error(
        code=exc.code,
        message=f"{operation} failed: {exc.message}",
        operation=operation,
        path="",
        suggestion=exc.suggestion,
        causes=[
            {"layer": "memory_git", "code": exc.code, "message": str(exc)},
        ],
    )


def _validate_history_path(
    cfg: MemoryToolsConfig, operation: str, path: str,
) -> str:
    """Validate a history path through the store's logical-path grammar
    (grammar/scope only — a history query can legitimately reference a
    since-deleted file, so there is no existence check). Store errors
    (``memory_invalid_path`` / ``memory_path_out_of_scope``) re-raise as
    tool errors."""
    try:
        _, logical = MemoryStore(cfg.memory_root)._validate_logical_path(path)
    except MemoryStoreError as exc:
        raise _store_error(operation, exc) from exc
    return str(logical)


def _briefing_refresh_pending(cfg: MemoryToolsConfig) -> bool:
    """Whether a briefing change is awaiting a provider rebuild. In the
    fat-agent model there is one provider-reload signal — the
    ``refresh_agent.flag`` under ``<workspace>/.puffo-agent/`` — so both
    ``briefing.dirty`` and ``briefing.provider_reload_required`` derive
    from its presence. No workspace ⇒ nothing pending."""
    if not cfg.workspace:
        return False
    from ..portal.state import refresh_agent_flag_path

    return refresh_agent_flag_path(Path(cfg.workspace)).is_file()


# ── semantic name / date handling ────────────────────────────────────


def _invalid_name(operation: str, name: object) -> ToolError:
    message = f"{operation}: {name!r} is not a valid memory name."
    return _args_error(
        operation, message, _NAME_SUGGESTION, code="memory_invalid_name",
    )


def _normalize_name(operation: str, name: object) -> str:
    """Normalize a semantic name to a safe flat file stem.

    Lowercase; one trailing ``.md`` dropped; spaces/underscores become
    dashes (runs collapsed); leading/trailing dashes and trailing dots
    stripped. The result must match ``^[a-z0-9][a-z0-9._-]*$`` and be
    at most ``NAME_MAX_LENGTH`` chars — so slashes, hidden (dot-led)
    names, and traversal shapes are all rejected, and the M2 store
    re-validates the final logical path regardless (defense in depth).
    """
    if not isinstance(name, str):
        raise _invalid_name(operation, name)
    n = name.strip().lower()
    if n.endswith(".md"):
        n = n[:-3]
    n = re.sub(r"[ _]+", "-", n)
    n = re.sub(r"-{2,}", "-", n)
    n = n.strip("-").rstrip(".")
    if not n or len(n) > NAME_MAX_LENGTH or not _NAME_RE.fullmatch(n):
        raise _invalid_name(operation, name)
    return n


def _recollection_path(operation: str, date: object) -> str:
    """Map an optional ``YYYY-MM-DD`` string (default: today UTC) to
    the dated logical path ``recollection/YYYY/MM/YYYY-MM-DD.md``."""
    if date in (None, ""):
        day = datetime.now(timezone.utc).date()
    else:
        if not isinstance(date, str) or not _DATE_RE.fullmatch(date):
            raise _args_error(
                operation,
                f"{operation}: date must be a YYYY-MM-DD string, got {date!r}.",
                "Pass date as YYYY-MM-DD, or omit it for today (UTC).",
            )
        try:
            day = date_type.fromisoformat(date)
        except ValueError:
            raise _args_error(
                operation,
                f"{operation}: {date!r} is not a real calendar date.",
                "Pass date as YYYY-MM-DD, or omit it for today (UTC).",
            ) from None
    return (
        f"{RECOLLECTION_DIR}/{day.year:04d}/{day.month:02d}/"
        f"{day.isoformat()}.md"
    )


def _validate_patches(operation: str, patches: object) -> list[dict]:
    if not isinstance(patches, (list, tuple)) or not patches:
        raise _args_error(
            operation,
            f"{operation}: patches must be a non-empty list.",
            "Pass patches as [{old_text, new_text}, ...].",
        )
    out: list[dict] = []
    for patch in patches:
        if (
            not isinstance(patch, dict)
            or not isinstance(patch.get("old_text"), str)
            or not isinstance(patch.get("new_text"), str)
        ):
            raise _args_error(
                operation,
                f"{operation}: each patch needs string old_text and new_text.",
                "Pass patches as [{old_text, new_text}, ...].",
            )
        if patch["old_text"] == "":
            # An empty old_text has no single, unambiguous match point;
            # reject it here rather than letting it reach the store.
            raise _args_error(
                operation,
                f"{operation}: old_text must be a non-empty string.",
                "old_text must be text that appears exactly once in the "
                "file; it cannot be empty.",
            )
        out.append(
            {"old_text": patch["old_text"], "new_text": patch["new_text"]}
        )
    return out


# ── shared write pipeline ────────────────────────────────────────────


def _ensure(cfg: MemoryToolsConfig) -> None:
    """Idempotent per-call init: memory tree + local audit repo. Runs
    lazily on each tool call — never at registration/build time, so
    ``build_server()`` stays side-effect free."""
    root = Path(cfg.memory_root)
    ensure_memory_tree(root)
    memory_git.ensure_memory_git(root)


def _store(cfg: MemoryToolsConfig) -> MemoryStore:
    # No workspace_dir: the tools layer owns the briefing post-effect
    # (and its provider_reload/warning mapping) instead of the store.
    return MemoryStore(
        cfg.memory_root, workspace_dir="", maintenance=cfg.maintenance,
    )


def _run_write(cfg: MemoryToolsConfig, tool: str, op, reason: str) -> dict:
    """Primitive → git commit → post-effects → result envelope."""
    if (
        not isinstance(reason, str)
        or "\x00" in reason
        or len(reason.encode("utf-8")) > 1024
    ):
        raise _args_error(
            tool,
            f"{tool}: reason must be text no longer than 1024 bytes.",
            "Use a short audit reason without NUL characters.",
        )
    _ensure(cfg)
    store = _store(cfg)
    root = Path(cfg.memory_root)
    with store.write_transaction():
        try:
            res = op(store)
        except MemoryStoreError as exc:
            raise _store_error(tool, exc) from exc
        logical = res["path"]
        changed = bool(res["changed"])
        warnings: list[dict] = []
        commit_id = None
        if changed:
            if memory_git.git_available() and (root / ".git").is_dir():
                message = memory_git.format_commit_message(tool, [logical], reason)
                commit_id = memory_git.commit_memory_change(root, [logical], message)
                if commit_id is None:
                    warnings.append({
                        "code": "memory_git_commit_failed",
                        "message": (
                            "Memory changed, but the audit commit failed; "
                            "the change is saved but not committed."
                        ),
                    })
            else:
                logger.info(
                    "memory git unavailable; %s %s left uncommitted",
                    tool, logical,
                )

    briefing_rebuilt = False
    provider_reload = "not_needed"
    if changed and logical.startswith(f"{BRIEFING_DIR}/"):
        briefing_rebuilt = True
        reload_ok = request_prompt_refresh(
            cfg.workspace, f"memory_tools.{tool}:{logical}",
        )
        provider_reload = "requested" if reload_ok else "failed"
        if not reload_ok:
            warnings.append({
                "code": "memory_provider_reload_failed",
                "message": (
                    "Memory changed, but the provider reload request "
                    "failed. The next provider spawn will load the "
                    "updated briefing."
                ),
            })

    return {
        "ok": True,
        "tool": tool,
        "changed": changed,
        "paths": [logical],
        "commit_id": commit_id,
        "post_effects": {
            "briefing_rebuilt": briefing_rebuilt,
            "provider_reload": provider_reload,
        },
        "warnings": warnings,
    }


# ── deterministic search ─────────────────────────────────────────────


def _scope_files(root: Path, scope: str, pattern: str) -> list[tuple[str, Path]]:
    """(logical path, physical path) pairs under one scope, sorted for
    deterministic scan order; hidden segments and symlinks skipped."""
    base = safe_memory_scope(root, scope)
    if base is None:
        return []
    resolved_root = root.resolve()
    out: list[tuple[str, Path]] = []
    for p in sorted(base.rglob(pattern)):
        try:
            if not p.resolve().is_relative_to(resolved_root):
                continue
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        if any(seg.startswith(".") for seg in rel.split("/")):
            continue
        if p.is_symlink() or not p.is_file():
            continue
        out.append((rel, p))
    return out


def _scan_files(
    files: list[tuple[str, str, Path]],
    query: str,
    limit: int,
    byte_cap: int | None = None,
) -> tuple[list[dict], bool]:
    """Case-insensitive substring scan, line by line. ≤
    ``SEARCH_PER_FILE_LIMIT`` matches per file, ``limit`` total;
    returns ``(results, truncated)``."""
    needle = query.lower()
    results: list[dict] = []
    remaining = SEARCH_TOTAL_READ_LIMIT
    truncated = False
    for rel, scope, path in files:
        cap = min(byte_cap or SEARCH_FILE_READ_LIMIT, remaining)
        if cap <= 0:
            return results, True
        with path.open("rb") as handle:
            data = handle.read(cap + 1)
        source_truncated = len(data) > cap
        truncated = truncated or source_truncated
        data = data[:cap]
        remaining -= len(data)
        text = data.decode("utf-8", errors="ignore")
        per_file = 0
        for lineno, line in enumerate(text.splitlines(), start=1):
            if needle not in line.lower():
                continue
            if len(results) >= limit:
                return results, True
            snippet = line.strip()
            if len(snippet) > SEARCH_SNIPPET_LIMIT:
                snippet = snippet[:SEARCH_SNIPPET_LIMIT]
            results.append({
                "path": rel,
                "scope": scope,
                "line": lineno,
                "snippet": snippet,
            })
            per_file += 1
            if per_file >= SEARCH_PER_FILE_LIMIT:
                break
    return results, truncated


def _validate_search_args(operation: str, query: object, limit: object) -> int:
    if not isinstance(query, str) or not query.strip():
        raise _args_error(
            operation,
            f"{operation}: query must be a non-empty string.",
            "Pass the text to look for.",
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise _args_error(
            operation,
            f"{operation}: limit must be a positive integer, got {limit!r}.",
            f"Use 1 ≤ limit ≤ {SEARCH_MAX_LIMIT} (default "
            f"{SEARCH_DEFAULT_LIMIT}).",
        )
    return min(limit, SEARCH_MAX_LIMIT)


# ── registration ─────────────────────────────────────────────────────


def register_memory_tools(mcp: FastMCP, cfg: MemoryToolsConfig) -> None:
    """Register semantic memory tools in their established public order."""
    from .memory_tool_registration import (
        register_briefing_and_recollection_tools,
        register_memory_history_tools,
        register_memory_read_and_search_tools,
        register_memory_status_tools,
        register_note_tools,
    )

    register_note_tools(mcp, cfg)
    register_briefing_and_recollection_tools(mcp, cfg)
    register_memory_read_and_search_tools(mcp, cfg)
    register_memory_status_tools(mcp, cfg)
    register_memory_history_tools(mcp, cfg)
