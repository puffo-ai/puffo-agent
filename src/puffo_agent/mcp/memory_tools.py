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
    IMPORTS_DIR,
    NOTES_DIR,
    RECOLLECTION_DIR,
    ensure_memory_tree,
    request_prompt_refresh,
)
from ..agent.memory_errors import MemoryHistoryError, MemoryStoreError
from ..agent.memory_store import (
    IMPORTS_READ_LIMIT,
    LIST_DEFAULT_LIMIT,
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
    _ensure(cfg)
    try:
        res = op(_store(cfg))
    except MemoryStoreError as exc:
        raise _store_error(tool, exc) from exc
    logical = res["path"]
    changed = bool(res["changed"])
    warnings: list[dict] = []

    commit_id = None
    root = Path(cfg.memory_root)
    if changed:
        # Only attempt an audit commit when git is available AND our own
        # ``.git`` audit repo exists — never stage into an enclosing
        # repo the memory root happens to sit inside.
        if memory_git.git_available() and (root / ".git").is_dir():
            message = memory_git.format_commit_message(
                tool, [logical], reason,
            )
            commit_id = memory_git.commit_memory_change(
                root, [logical], message,
            )
            if commit_id is None:
                warnings.append({
                    "code": "memory_git_commit_failed",
                    "message": (
                        "Memory changed, but the audit commit failed; "
                        "the change is saved but not committed."
                    ),
                })
        else:
            # Graceful degrade — the doc reserves warnings for
            # post-effect failures; git being absent is just logged
            # (by ensure_memory_git).
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
    base = root / scope
    if not base.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for p in sorted(base.rglob(pattern)):
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
    for rel, scope, path in files:
        data = path.read_bytes()
        if byte_cap is not None:
            data = data[:byte_cap]
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
    return results, False


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
    """Register the M3 semantic memory tools on ``mcp``.

    Nine tools always register; ``append_recollection`` registers only
    when ``cfg.maintenance`` is set, since recollection/ writes need the
    maintenance scope the agent-facing server never grants."""

    # -- write tools ---------------------------------------------------

    @mcp.tool()
    async def create_note(name: str, body: str, reason: str = "") -> dict:
        """Create a durable note at notes/<name>.md.

        Notes are searchable long-term memory — detail that should
        survive but not be injected into every prompt. ``name`` is a
        semantic name (normalized to a flat lowercase slug), not a
        path. Fails if the note already exists — change an existing
        note with patch_note or append_note. ``reason`` (optional) is
        recorded in the memory audit log.
        """
        slug = _normalize_name("create_note", name)
        path = f"{NOTES_DIR}/{slug}.md"
        return _run_write(
            cfg, "create_note",
            lambda s: s.create_memory_file(path, body), reason,
        )

    @mcp.tool()
    async def patch_note(
        name: str, patches: list[dict], reason: str = "",
    ) -> dict:
        """Patch an existing note at notes/<name>.md by exact text
        replacement.

        ``patches`` is a list of ``{old_text, new_text}``; each
        old_text must match the current content exactly once, and the
        list applies all-or-nothing. ``reason`` (optional) is recorded
        in the memory audit log.
        """
        slug = _normalize_name("patch_note", name)
        checked = _validate_patches("patch_note", patches)
        path = f"{NOTES_DIR}/{slug}.md"
        return _run_write(
            cfg, "patch_note",
            lambda s: s.patch_memory_file(path, checked), reason,
        )

    @mcp.tool()
    async def append_note(name: str, text: str, reason: str = "") -> dict:
        """Append text to the end of an existing note at
        notes/<name>.md. ``reason`` (optional) is recorded in the
        memory audit log.
        """
        slug = _normalize_name("append_note", name)
        path = f"{NOTES_DIR}/{slug}.md"
        return _run_write(
            cfg, "append_note",
            lambda s: s.append_memory_file(path, text), reason,
        )

    @mcp.tool()
    async def create_briefing_topic(
        name: str, body: str, reason: str = "",
    ) -> dict:
        """Create a briefing topic at briefing/<name>.md.

        Briefing topics are ALWAYS loaded into your prompt, so keep
        them small — the store enforces per-file and compiled-total
        budgets and rejects oversized writes. A successful change
        marks the provider prompt artifacts for rebuild;
        ``post_effects.provider_reload`` reports "requested" when the
        rebuild flag was written (the provider picks it up at its next
        spawn/turn) and "failed" (with a warning) when it could not
        be. ``reason`` (optional) is recorded in the memory audit log.
        """
        slug = _normalize_name("create_briefing_topic", name)
        path = f"{BRIEFING_DIR}/{slug}.md"
        return _run_write(
            cfg, "create_briefing_topic",
            lambda s: s.create_memory_file(path, body), reason,
        )

    @mcp.tool()
    async def patch_briefing_topic(
        name: str, patches: list[dict], reason: str = "",
    ) -> dict:
        """Patch an existing briefing topic at briefing/<name>.md by
        exact text replacement (same rules as patch_note).

        A successful change marks the provider prompt artifacts for
        rebuild; ``post_effects.provider_reload`` reports "requested"
        when the rebuild flag was written (picked up at the provider's
        next spawn/turn) and "failed" (with a warning) when it could
        not be. ``reason`` (optional) is recorded in the memory audit
        log.
        """
        slug = _normalize_name("patch_briefing_topic", name)
        checked = _validate_patches("patch_briefing_topic", patches)
        path = f"{BRIEFING_DIR}/{slug}.md"
        return _run_write(
            cfg, "patch_briefing_topic",
            lambda s: s.patch_memory_file(path, checked), reason,
        )

    async def append_recollection(
        text: str,
        date: str = "",
        source_message_ids: list[str] | None = None,
        related_paths: list[str] | None = None,
        reason: str = "",
    ) -> dict:
        """Append an entry to the dated recollection journal at
        recollection/YYYY/MM/YYYY-MM-DD.md (``date`` defaults to today
        UTC; the file is created with a ``# <date>`` header when
        missing).

        recollection/ is daemon-owned maintenance memory: ordinary
        conversation turns do NOT have write scope here and get a
        structured memory_scope_readonly error — put durable detail in
        notes/ instead. ``source_message_ids`` / ``related_paths``
        (optional) are recorded as sources:/related: lines; ``reason``
        (optional) goes to the memory audit log.
        """
        path = _recollection_path("append_recollection", date)
        day = path.rsplit("/", 1)[-1][:-3]
        lines = [text]
        if source_message_ids:
            lines.append(
                "sources: " + ", ".join(str(i) for i in source_message_ids)
            )
        if related_paths:
            lines.append(
                "related: " + ", ".join(str(p) for p in related_paths)
            )
        entry = "\n" + "\n".join(lines) + "\n"

        def op(store: MemoryStore) -> dict:
            if store.get_memory_file_status(path)["exists"]:
                return store.append_memory_file(path, entry)
            return store.create_memory_file(path, f"# {day}\n" + entry)

        return _run_write(cfg, "append_recollection", op, reason)

    # recollection/ is daemon-owned maintenance memory; the agent-facing
    # server (maintenance=False) has no write scope there, so an agent
    # calling append_recollection would only ever get a
    # memory_scope_readonly error. Register the tool solely under the
    # explicit maintenance scope, where it actually works — a future
    # maintenance server flips ``maintenance`` on.
    if cfg.maintenance:
        mcp.tool()(append_recollection)

    # -- read tools ----------------------------------------------------

    @mcp.tool()
    async def read_memory_file(path: str) -> dict:
        """Read one memory file by logical path (briefing/…, notes/…,
        recollection/…, imports/…). The body is bounded by the area's
        file limit; ``truncated`` flags a cut read."""
        _ensure(cfg)
        try:
            return _store(cfg).read_memory_file(path)
        except MemoryStoreError as exc:
            raise _store_error("read_memory_file", exc) from exc

    @mcp.tool()
    async def read_memory_files(paths: list[str]) -> dict:
        """Read up to 16 memory files in one call (pure read, never
        writes). Each entry is a read result or a per-path error — one
        bad path does not fail the batch."""
        _ensure(cfg)
        try:
            results = _store(cfg).read_memory_files(paths)
        except MemoryStoreError as exc:
            raise _store_error("read_memory_files", exc) from exc
        return {"ok": True, "results": results}

    # -- search tools --------------------------------------------------

    @mcp.tool()
    async def search_memory(
        query: str,
        scopes: list[str] | None = None,
        limit: int = SEARCH_DEFAULT_LIMIT,
    ) -> dict:
        """Search memory for a case-insensitive substring, line by
        line. ``scopes`` defaults to ["briefing", "notes",
        "recollection"] (imports/ is searchable only via
        search_imports). Deterministic order; at most 3 matches per
        file; ``limit`` caps total results (default 20, max 50)."""
        operation = "search_memory"
        _ensure(cfg)
        capped = _validate_search_args(operation, query, limit)
        requested = list(_SEARCH_SCOPES) if scopes is None else list(scopes)
        if not requested:
            raise _args_error(
                operation,
                "search_memory: scopes must not be empty.",
                "Omit scopes, or pick from: briefing, notes, recollection.",
            )
        for scope in requested:
            if scope == IMPORTS_DIR:
                raise _args_error(
                    operation,
                    "search_memory does not search imports/.",
                    "Use search_imports for imported content.",
                )
            if scope not in _SEARCH_SCOPES:
                raise _args_error(
                    operation,
                    f"search_memory: unknown scope {scope!r}.",
                    "Pick scopes from: briefing, notes, recollection.",
                )
        root = Path(cfg.memory_root)
        files = [
            (rel, scope, p)
            for scope in _SEARCH_SCOPES
            if scope in requested
            for rel, p in _scope_files(root, scope, "*.md")
        ]
        try:
            results, truncated = _scan_files(files, query, capped)
        except OSError as exc:
            raise _tool_error(
                code="memory_search_failed",
                message=f"search_memory failed: {exc}",
                operation=operation,
                path="",
                suggestion="Retry; if it persists, check the memory dir.",
                causes=[{
                    "layer": "memory_tools",
                    "code": "memory_search_failed",
                    "message": str(exc),
                }],
            ) from exc
        return {
            "ok": True, "query": query,
            "results": results, "truncated": truncated,
        }

    @mcp.tool()
    async def search_imports(
        query: str, limit: int = SEARCH_DEFAULT_LIMIT,
    ) -> dict:
        """Search imported (read-only) content under imports/ for a
        case-insensitive substring. Reads at most 128KB per file;
        same match/limit rules as search_memory."""
        operation = "search_imports"
        _ensure(cfg)
        capped = _validate_search_args(operation, query, limit)
        root = Path(cfg.memory_root)
        files = [
            (rel, IMPORTS_DIR, p)
            for rel, p in _scope_files(root, IMPORTS_DIR, "*")
        ]
        try:
            results, truncated = _scan_files(
                files, query, capped, byte_cap=IMPORTS_READ_LIMIT,
            )
        except OSError as exc:
            raise _tool_error(
                code="memory_search_failed",
                message=f"search_imports failed: {exc}",
                operation=operation,
                path="",
                suggestion="Retry; if it persists, check the memory dir.",
                causes=[{
                    "layer": "memory_tools",
                    "code": "memory_search_failed",
                    "message": str(exc),
                }],
            ) from exc
        return {
            "ok": True, "query": query,
            "results": results, "truncated": truncated,
        }

    # -- M4 status / recall / history (all read-only, agent-facing) ----
    #
    # Unlike the M3 read tools, these NEVER call _ensure: a status or
    # history read must report an uninitialised tree/repo honestly
    # rather than creating it. None of them expose any write, rollback,
    # or raw git flag/ref surface.

    @mcp.tool()
    async def get_memory_status() -> dict:
        """Read-only health of this agent's memory (no file bodies).

        Reports root existence, whether the local git audit repo is
        available, the compiled-briefing size with its budget and
        dirty/reload flags, and per-scope ``{files, total_size_bytes}``.
        Never creates the memory tree or the audit repo.
        """
        root = Path(cfg.memory_root)
        status = _store(cfg).get_memory_status()
        status["ok"] = True
        status["git_enabled"] = (
            memory_git.git_available() and (root / ".git").is_dir()
        )
        pending = _briefing_refresh_pending(cfg)
        status["briefing"]["dirty"] = pending
        status["briefing"]["provider_reload_required"] = pending
        return status

    @mcp.tool()
    async def get_memory_file_status(path: str) -> dict:
        """Existence / scope / size / limit / briefing-inclusion for one
        logical memory path — deliberately NO body.

        Extended (best-effort) with git last-change metadata
        (``git_tracked`` / ``last_changed_commit_id`` /
        ``last_changed_at``); when git is unavailable or the audit repo
        is not initialised those fields are null and file status still
        works. Read-only; never creates the tree or repo.
        """
        try:
            status = _store(cfg).get_memory_file_status(path)
        except MemoryStoreError as exc:
            raise _store_error("get_memory_file_status", exc) from exc
        git_tracked = None
        last_commit = None
        last_at = None
        try:
            hist = memory_git.history_status(
                Path(cfg.memory_root), status["path"],
            )
            git_tracked = hist["path_tracked"]
            last_commit = hist["last_changed_commit_id"]
            last_at = hist["last_changed_at"]
        except MemoryHistoryError as exc:
            # No git / no audit repo just leaves the git fields null;
            # any other failure is a real error worth surfacing.
            if exc.code not in (
                "memory_history_unavailable",
                "memory_history_not_initialized",
            ):
                raise _history_error("get_memory_file_status", exc) from exc
        status["git_tracked"] = git_tracked
        status["last_changed_commit_id"] = last_commit
        status["last_changed_at"] = last_at
        status["ok"] = True
        # M4 status is body-less by contract (recall bodies go through
        # read_memory_file / read_memory_files).
        assert "body" not in status
        return status

    @mcp.tool()
    async def list_memory_files(
        scope: str = "", limit: int = LIST_DEFAULT_LIMIT,
    ) -> dict:
        """List logical memory paths + lightweight metadata — never a
        body.

        Each entry carries ``path`` / ``scope`` / ``size`` /
        ``writable`` / ``briefing_included``. ``scope`` (optional)
        restricts to one area (briefing, notes, recollection, imports);
        ``limit`` bounds the count and ``truncated`` flags that more
        files exist. Read-only.
        """
        try:
            result = _store(cfg).list_memory_files(scope or None, limit)
        except MemoryStoreError as exc:
            raise _store_error("list_memory_files", exc) from exc
        return {"ok": True, **result}

    @mcp.tool()
    async def get_memory_history_status(path: str = "") -> dict:
        """Read-only health of the memory audit history.

        Reports git availability, whether the audit repo is
        initialised, the HEAD commit id, and the clean/dirty state of
        the work tree. When ``path`` (a logical memory path) is given it
        also reports whether that path is tracked and when it last
        changed. NOT a git passthrough — no raw git flags or refs are
        accepted, and this never creates the repo.
        """
        logical = (
            _validate_history_path(cfg, "get_memory_history_status", path)
            if path else None
        )
        try:
            result = memory_git.history_status(Path(cfg.memory_root), logical)
        except MemoryHistoryError as exc:
            raise _history_error("get_memory_history_status", exc) from exc
        return {"ok": True, **result}

    @mcp.tool()
    async def get_memory_history(
        path: str = "",
        scopes: list[str] | None = None,
        since: str = "",
        until: str = "",
        actor: str = "",
        operation: str = "",
        query: str = "",
        limit: int = memory_git.HISTORY_DEFAULT_LIMIT,
        include_diff: bool = False,
    ) -> dict:
        """Bounded read-only audit query over the local memory git
        history. NOT a ``git log`` passthrough.

        Only the whitelisted filters are honoured — ``path`` / ``scopes``
        (which areas), ``since`` / ``until`` (dates), ``actor`` (commit
        author), ``operation`` (matches the recorded write tool),
        ``query`` (case-insensitive substring over commit
        subject/reason/changed-paths — never the diff text), ``limit``,
        and ``include_diff``. No raw git flags, refs, revision ranges,
        or rollback are ever accepted: a filter value that looks like a
        flag (``--all``) or a ref range (``HEAD~5..HEAD``) is treated as
        a literal value. Each entry carries ``commit_id`` / ``time`` /
        ``actor`` / ``operation`` / ``reason`` / ``message`` /
        ``changed_paths`` / ``summary``; ``include_diff`` adds a
        byte-capped ``diff`` excerpt with a ``diff_truncated`` flag.
        """
        logical = (
            _validate_history_path(cfg, "get_memory_history", path)
            if path else None
        )
        try:
            result = memory_git.query_history(
                Path(cfg.memory_root),
                path=logical,
                scopes=scopes,
                since=since,
                until=until,
                actor=actor,
                operation=operation,
                query=query,
                limit=limit,
                include_diff=include_diff,
            )
        except MemoryHistoryError as exc:
            raise _history_error("get_memory_history", exc) from exc
        return {"ok": True, **result}
