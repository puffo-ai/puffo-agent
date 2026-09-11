"""Cohesive registration groups for semantic memory MCP tools."""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..agent import memory_git
from ..agent.memory import BRIEFING_DIR, IMPORTS_DIR, NOTES_DIR
from ..agent.memory_errors import MemoryHistoryError, MemoryStoreError
from ..agent.memory_store import IMPORTS_READ_LIMIT, LIST_DEFAULT_LIMIT, MemoryStore
from .memory_tools import (
    _SEARCH_SCOPES,
    SEARCH_DEFAULT_LIMIT,
    MemoryToolsConfig,
    _args_error,
    _briefing_refresh_pending,
    _ensure,
    _history_error,
    _normalize_name,
    _recollection_path,
    _run_write,
    _scan_files,
    _scope_files,
    _store,
    _store_error,
    _tool_error,
    _validate_history_path,
    _validate_patches,
    _validate_search_args,
)


def register_note_tools(mcp: FastMCP, cfg: MemoryToolsConfig) -> None:
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
            cfg,
            "create_note",
            lambda s: s.create_memory_file(path, body),
            reason,
        )

    @mcp.tool()
    async def patch_note(
        name: str,
        patches: list[dict],
        reason: str = "",
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
            cfg,
            "patch_note",
            lambda s: s.patch_memory_file(path, checked),
            reason,
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
            cfg,
            "append_note",
            lambda s: s.append_memory_file(path, text),
            reason,
        )


def register_briefing_and_recollection_tools(
    mcp: FastMCP, cfg: MemoryToolsConfig
) -> None:
    @mcp.tool()
    async def create_briefing_topic(
        name: str,
        body: str,
        reason: str = "",
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
            cfg,
            "create_briefing_topic",
            lambda s: s.create_memory_file(path, body),
            reason,
        )

    @mcp.tool()
    async def patch_briefing_topic(
        name: str,
        patches: list[dict],
        reason: str = "",
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
            cfg,
            "patch_briefing_topic",
            lambda s: s.patch_memory_file(path, checked),
            reason,
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
            lines.append("sources: " + ", ".join(str(i) for i in source_message_ids))
        if related_paths:
            lines.append("related: " + ", ".join(str(p) for p in related_paths))
        entry = "\n" + "\n".join(lines) + "\n"

        def op(store: MemoryStore) -> dict:
            if store.get_memory_file_status(path)["exists"]:
                return store.append_memory_file(path, entry)
            return store.create_memory_file(path, f"# {day}\n" + entry)

        return _run_write(cfg, "append_recollection", op, reason)

    # recollection/ is daemon-owned maintenance memory; the agent-facing
    # server registers it only when that explicit scope is present.
    if cfg.maintenance:
        mcp.tool()(append_recollection)


def register_memory_read_tools(mcp: FastMCP, cfg: MemoryToolsConfig) -> None:
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


def register_memory_search_tool(mcp: FastMCP, cfg: MemoryToolsConfig) -> None:
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
                causes=[
                    {
                        "layer": "memory_tools",
                        "code": "memory_search_failed",
                        "message": str(exc),
                    }
                ],
            ) from exc
        return {
            "ok": True,
            "query": query,
            "results": results,
            "truncated": truncated,
        }


def register_import_search_tool(mcp: FastMCP, cfg: MemoryToolsConfig) -> None:
    @mcp.tool()
    async def search_imports(
        query: str,
        limit: int = SEARCH_DEFAULT_LIMIT,
    ) -> dict:
        """Search imported (read-only) content under imports/ for a
        case-insensitive substring. Reads at most 128KB per file;
        same match/limit rules as search_memory."""
        operation = "search_imports"
        _ensure(cfg)
        capped = _validate_search_args(operation, query, limit)
        root = Path(cfg.memory_root)
        files = [
            (rel, IMPORTS_DIR, p) for rel, p in _scope_files(root, IMPORTS_DIR, "*")
        ]
        try:
            results, truncated = _scan_files(
                files,
                query,
                capped,
                byte_cap=IMPORTS_READ_LIMIT,
            )
        except OSError as exc:
            raise _tool_error(
                code="memory_search_failed",
                message=f"search_imports failed: {exc}",
                operation=operation,
                path="",
                suggestion="Retry; if it persists, check the memory dir.",
                causes=[
                    {
                        "layer": "memory_tools",
                        "code": "memory_search_failed",
                        "message": str(exc),
                    }
                ],
            ) from exc
        return {
            "ok": True,
            "query": query,
            "results": results,
            "truncated": truncated,
        }


def register_memory_search_tools(mcp: FastMCP, cfg: MemoryToolsConfig) -> None:
    """Register memory search before imported-memory search."""
    register_memory_search_tool(mcp, cfg)
    register_import_search_tool(mcp, cfg)


def register_memory_read_and_search_tools(
    mcp: FastMCP,
    cfg: MemoryToolsConfig,
) -> None:
    """Register bounded memory reads before memory search tools."""
    register_memory_read_tools(mcp, cfg)
    register_memory_search_tools(mcp, cfg)


def register_memory_status_tools(mcp: FastMCP, cfg: MemoryToolsConfig) -> None:
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
        status["git_enabled"] = memory_git.git_available() and (root / ".git").is_dir()
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
                Path(cfg.memory_root),
                status["path"],
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
        scope: str = "",
        limit: int = LIST_DEFAULT_LIMIT,
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


def register_memory_history_tools(mcp: FastMCP, cfg: MemoryToolsConfig) -> None:
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
            if path
            else None
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
            _validate_history_path(cfg, "get_memory_history", path) if path else None
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
