"""Low-level MemoryStore file API (M2).

The seven primitives from the memory design doc operate on **logical
memory paths** (``briefing/profile.md``, ``notes/topic.md``,
``recollection/2026/06/2026-06-04.md``, ``imports/index.md``) — never
physical filesystem paths. The store boundary enforces:

- path grammar (relative, known scope, no traversal/hidden/symlink
  escapes; ``briefing/`` stays flat because the compile globs
  ``briefing/*.md`` non-recursively);
- per-area scope rules (``imports/`` is importer-owned and read-only;
  ``recollection/`` writes need the explicit maintenance scope);
- per-area size limits, validated before any write commits;
- atomic writes (sibling temp file + ``os.replace``);
- briefing dirty/rebuild: successful ``briefing/`` mutations drop the
  M1 ``refresh_agent.flag`` via ``memory.request_prompt_refresh``.

Errors are ``memory.MemoryStoreError`` (briefing-scope size violations
raise the ``BriefingCompileError`` subclass so M1 callers keep
working). The primitives deliberately take no ``reason`` — semantic
reason/audit metadata belongs to the M3 tools layered on top.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from .memory import (
    BRIEFING_DIR,
    IMPORTS_DIR,
    NOTES_DIR,
    PER_FILE_LIMIT,
    PROFILE_BRIEFING_NAME,
    RECOLLECTION_DIR,
    TOTAL_LIMIT,
    _byte_size,
    _compose_briefing,
    _read_briefing_entries,
    compile_briefing,
    request_prompt_refresh,
)
from .memory_errors import BriefingCompileError, MemoryStoreError

# Per-file byte limits (top of the design ranges, like M1's briefing
# budget). ``imports/`` is never written through the store; its entry
# only bounds reads.
NOTES_FILE_LIMIT = 64 * 1024
RECOLLECTION_FILE_LIMIT = 128 * 1024
IMPORTS_READ_LIMIT = RECOLLECTION_FILE_LIMIT

READ_BATCH_LIMIT = 16

# list_memory_files bounds (M4 recall): default page size and the hard
# cap the requested limit is clamped to.
LIST_DEFAULT_LIMIT = 100
LIST_MAX_LIMIT = 500

_FILE_LIMITS = {
    BRIEFING_DIR: PER_FILE_LIMIT,
    NOTES_DIR: NOTES_FILE_LIMIT,
    RECOLLECTION_DIR: RECOLLECTION_FILE_LIMIT,
    IMPORTS_DIR: IMPORTS_READ_LIMIT,
}

_SCOPES = (BRIEFING_DIR, NOTES_DIR, RECOLLECTION_DIR, IMPORTS_DIR)

_PATH_SUGGESTION = (
    "Use a relative logical memory path like notes/topic.md under "
    "briefing/, notes/, recollection/, or imports/."
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class MemoryStore:
    """Validated file operations under one agent's memory root.

    ``workspace_dir`` (optional) enables the briefing dirty/rebuild
    hook; ``maintenance`` is the explicit scope flag that unlocks
    ``recollection/`` writes (daemon-owned maintenance, not ordinary
    chat turns).
    """

    def __init__(
        self,
        memory_root: str | Path,
        workspace_dir: str = "",
        maintenance: bool = False,
    ):
        self.memory_root = Path(memory_root)
        self.workspace_dir = workspace_dir
        self.maintenance = maintenance

    # ── the seven primitives ─────────────────────────────────────────

    def create_memory_file(self, path: str, body: str) -> dict:
        """Create a new file. Fails with ``memory_file_exists`` if the
        target already exists — overwriting goes through patch/append."""
        scope, logical = self._validate_write_path(path)
        physical = self._physical_path(logical)
        if physical.exists():
            raise MemoryStoreError(
                "memory_file_exists",
                path=str(logical),
                suggestion=(
                    "The file already exists; change it with "
                    "patch_memory_file or append_memory_file."
                ),
            )
        size = self._validate_size(scope, logical, body)
        self._write_atomic(physical, body)
        self._mark_briefing_dirty(scope, "create", logical)
        return {"path": str(logical), "changed": True, "size": size}

    def read_memory_file(self, path: str) -> dict:
        """Bounded read: ``body`` is capped at the area's file limit
        (``truncated`` flags a cut); ``size`` is the real file size.
        ``lossy`` is True when the file wasn't valid UTF-8 and
        replacement characters were substituted (the read never
        raises on stored bytes)."""
        scope, logical = self._validate_logical_path(path)
        physical = self._physical_path(logical)
        if not physical.is_file():
            raise MemoryStoreError(
                "memory_file_not_found",
                path=str(logical),
                suggestion=(
                    "Check the path with get_memory_file_status, or "
                    "create the file with create_memory_file."
                ),
            )
        limit = _FILE_LIMITS[scope]
        data = physical.read_bytes()
        size = len(data)
        truncated = size > limit
        if truncated:
            # A byte cap can split a multi-byte sequence; drop the
            # trailing partial character rather than emitting garbage.
            body = data[:limit].decode("utf-8", errors="ignore")
            lossy = False
        else:
            # Never raise on stored bytes: a non-UTF-8 file is surfaced
            # with U+FFFD replacements and flagged ``lossy`` instead of
            # crashing the read with UnicodeDecodeError.
            try:
                body = data.decode("utf-8")
                lossy = False
            except UnicodeDecodeError:
                body = data.decode("utf-8", errors="replace")
                lossy = True
        return {
            "path": str(logical),
            "scope": scope,
            "size": size,
            "truncated": truncated,
            "lossy": lossy,
            "body": body,
        }

    def read_memory_files(self, paths: list) -> list[dict]:
        """Pure read batch (≤ ``READ_BATCH_LIMIT`` paths). Each entry
        is a ``read_memory_file`` result or ``{path, error}`` — one bad
        path never fails the whole batch. Never writes."""
        if not isinstance(paths, (list, tuple)):
            raise MemoryStoreError(
                "memory_invalid_arguments",
                path="",
                suggestion="Pass a list of logical memory paths.",
            )
        if len(paths) > READ_BATCH_LIMIT:
            raise MemoryStoreError(
                "memory_invalid_arguments",
                path="",
                suggestion=(
                    f"read_memory_files accepts at most {READ_BATCH_LIMIT} "
                    "paths per call; split the batch."
                ),
            )
        results: list[dict] = []
        for p in paths:
            try:
                results.append(self.read_memory_file(p))
            except MemoryStoreError as exc:
                results.append({
                    "path": p if isinstance(p, str) else str(p),
                    "error": exc.to_dict(),
                })
        return results

    def patch_memory_file(self, path: str, patches: list) -> dict:
        """Exact text replacement. Each ``{old_text, new_text}`` must
        match exactly once; the patch list is all-or-nothing — nothing
        is written unless every patch applies."""
        scope, logical = self._validate_write_path(path)
        physical = self._physical_path(logical)
        if not physical.is_file():
            raise MemoryStoreError(
                "memory_file_not_found",
                path=str(logical),
                suggestion="Create the file with create_memory_file first.",
            )
        original = physical.read_text(encoding="utf-8")
        text = original
        for patch in patches:
            old_text = patch["old_text"]
            if not old_text:
                # An empty old_text has no unambiguous match point;
                # tell direct store callers the truth rather than
                # faking a "multiple matches" count.
                raise MemoryStoreError(
                    "memory_invalid_arguments",
                    path=str(logical),
                    suggestion=(
                        "old_text must be non-empty text that appears "
                        "exactly once in the file."
                    ),
                )
            matches = text.count(old_text)
            if matches == 0:
                raise MemoryStoreError(
                    "memory_patch_no_match",
                    path=str(logical),
                    suggestion=(
                        "old_text was not found; read the file again and "
                        "patch exact current text."
                    ),
                )
            if matches > 1:
                raise MemoryStoreError(
                    "memory_patch_multiple_matches",
                    path=str(logical),
                    suggestion=(
                        "old_text matched more than once; include more "
                        "surrounding context so it matches exactly once."
                    ),
                )
            text = text.replace(old_text, patch["new_text"])
        size = self._validate_size(scope, logical, text)
        changed = text != original
        if changed:
            self._write_atomic(physical, text)
            self._mark_briefing_dirty(scope, "patch", logical)
        return {"path": str(logical), "changed": changed, "size": size}

    def append_memory_file(self, path: str, text: str) -> dict:
        """Append to the end of an existing file (no separator magic,
        no prepend/insert mode)."""
        scope, logical = self._validate_write_path(path)
        physical = self._physical_path(logical)
        if not physical.is_file():
            raise MemoryStoreError(
                "memory_file_not_found",
                path=str(logical),
                suggestion="Create the file with create_memory_file first.",
            )
        original = physical.read_text(encoding="utf-8")
        combined = original + text
        size = self._validate_size(scope, logical, combined)
        changed = combined != original
        if changed:
            self._write_atomic(physical, combined)
            self._mark_briefing_dirty(scope, "append", logical)
        return {"path": str(logical), "changed": changed, "size": size}

    def delete_memory_file(self, path: str) -> dict:
        """Delete a safe memory file: validated writable path, regular
        file, and never the managed ``briefing/profile.md``."""
        scope, logical = self._validate_write_path(path)
        if scope == BRIEFING_DIR and logical.name == PROFILE_BRIEFING_NAME:
            raise MemoryStoreError(
                "memory_scope_readonly",
                path=str(logical),
                suggestion=(
                    "briefing/profile.md is the managed profile briefing "
                    "and cannot be deleted; patch it instead."
                ),
            )
        physical = self._physical_path(logical)
        if not physical.exists():
            raise MemoryStoreError(
                "memory_file_not_found",
                path=str(logical),
                suggestion="Nothing to delete at this path.",
            )
        if not physical.is_file():
            raise MemoryStoreError(
                "memory_invalid_path",
                path=str(logical),
                suggestion="Only regular memory files can be deleted.",
            )
        physical.unlink()
        self._mark_briefing_dirty(scope, "delete", logical)
        return {"path": str(logical), "changed": True}

    def get_memory_file_status(self, path: str) -> dict:
        """Existence/scope/size/limit/briefing-inclusion metadata —
        deliberately no ``body``. Works for missing files."""
        scope, logical = self._validate_logical_path(path)
        physical = self._physical_path(logical)
        exists = physical.is_file()
        writable = self._scope_writable(scope) and logical.suffix == ".md"
        return {
            "path": str(logical),
            "exists": exists,
            "scope": scope,
            "size": physical.stat().st_size if exists else None,
            "limit": _FILE_LIMITS[scope],
            "writable": writable,
            "briefing_included": (
                exists and scope == BRIEFING_DIR and logical.suffix == ".md"
            ),
        }

    # ── M4 status + recall (pure filesystem reads, no git) ───────────

    def _iter_scope_files(self, scope: str) -> list[tuple[str, Path]]:
        """``(logical posix path, physical path)`` pairs under one
        scope, sorted for deterministic order; hidden segments and
        symlinks are skipped (same rules as ``memory_tools._scope_files``).
        Uses ``rglob('*')`` filtered to real files — ``briefing/`` is
        flat, ``recollection/`` is nested, ``imports/`` may hold
        non-``.md`` files."""
        base = self.memory_root / scope
        if not base.is_dir():
            return []
        out: list[tuple[str, Path]] = []
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(self.memory_root).as_posix()
            if any(seg.startswith(".") for seg in rel.split("/")):
                continue
            if p.is_symlink() or not p.is_file():
                continue
            out.append((rel, p))
        return out

    def get_memory_status(self) -> dict:
        """Root health + compiled-briefing size + per-scope
        ``{files, total_size_bytes}`` — a pure filesystem read (no git,
        no bodies). The compiled size is computed defensively: an
        over-budget briefing reports the offending size with
        ``over_budget=True`` rather than raising, because a status read
        must never fail closed the way a write does."""
        try:
            compiled_size = _byte_size(compile_briefing(self.memory_root))
            over_budget = False
        except BriefingCompileError as exc:
            compiled_size = exc.size or 0
            over_budget = True
        scopes: dict = {}
        for scope in _SCOPES:
            files = self._iter_scope_files(scope)
            scopes[scope] = {
                "files": len(files),
                "total_size_bytes": sum(p.stat().st_size for _, p in files),
            }
        return {
            "memory_root": str(self.memory_root),
            "root_exists": self.memory_root.is_dir(),
            "briefing": {
                "compiled_size_bytes": compiled_size,
                "limit_bytes": TOTAL_LIMIT,
                "over_budget": over_budget,
            },
            "scopes": scopes,
        }

    def list_memory_files(
        self, scope: str | None = None, limit: int = LIST_DEFAULT_LIMIT,
    ) -> dict:
        """Logical paths + lightweight metadata for the requested scope
        (or all scopes in fixed order), **never** a body. Each entry is
        ``{path, scope, size, writable, briefing_included}``; the list
        is capped at ``min(limit, LIST_MAX_LIMIT)`` and ``truncated``
        flags that more files exist beyond the cap."""
        if scope is not None and scope not in _SCOPES:
            raise MemoryStoreError(
                "memory_path_out_of_scope",
                path=str(scope),
                suggestion=(
                    "scope must be one of briefing, notes, recollection, "
                    "imports — or omitted to list every scope."
                ),
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise MemoryStoreError(
                "memory_invalid_arguments",
                path="",
                suggestion=(
                    f"limit must be a positive integer (default "
                    f"{LIST_DEFAULT_LIMIT}, max {LIST_MAX_LIMIT})."
                ),
            )
        cap = min(limit, LIST_MAX_LIMIT)
        files: list[dict] = []
        truncated = False
        for sc in _SCOPES:
            if scope is not None and sc != scope:
                continue
            for rel, physical in self._iter_scope_files(sc):
                if len(files) >= cap:
                    truncated = True
                    break
                logical = PurePosixPath(rel)
                files.append({
                    "path": rel,
                    "scope": sc,
                    "size": physical.stat().st_size,
                    "writable": (
                        self._scope_writable(sc) and logical.suffix == ".md"
                    ),
                    "briefing_included": (
                        sc == BRIEFING_DIR and logical.suffix == ".md"
                    ),
                })
            if truncated:
                break
        return {"files": files, "truncated": truncated}

    # ── daemon-owned writer (not one of the public seven) ────────────

    def put_memory_file(self, path: str, body: str) -> dict:
        """Overwrite-allowed create, for the daemon-owned M1 writers
        (``MemoryManager.save`` / ``sync_profile_briefing`` /
        ``migrate_flat_memory``) — same validation, sizing, atomicity,
        and briefing dirty hook. Not part of the public seven:
        agent-facing callers must use create/patch/append so overwrites
        stay deliberate."""
        scope, logical = self._validate_write_path(path)
        physical = self._physical_path(logical)
        size = self._validate_size(scope, logical, body)
        changed = not (
            physical.is_file()
            and physical.read_text(encoding="utf-8") == body
        )
        if changed:
            self._write_atomic(physical, body)
            self._mark_briefing_dirty(scope, "put", logical)
        return {"path": str(logical), "changed": changed, "size": size}

    # ── validation ───────────────────────────────────────────────────

    def _validate_logical_path(
        self, path: str,
    ) -> tuple[str, PurePosixPath]:
        """Enforce the logical path grammar; returns ``(scope, path)``."""
        if not isinstance(path, str) or not path:
            raise MemoryStoreError(
                "memory_invalid_path",
                path=str(path or ""),
                suggestion=_PATH_SUGGESTION,
            )
        if "\x00" in path or "\\" in path:
            raise MemoryStoreError(
                "memory_invalid_path", path=path, suggestion=_PATH_SUGGESTION,
            )
        if path.startswith("/") or _DRIVE_RE.match(path):
            raise MemoryStoreError(
                "memory_invalid_path",
                path=path,
                suggestion="Memory paths are relative, not absolute. "
                + _PATH_SUGGESTION,
            )
        segments = path.split("/")
        for segment in segments:
            if not segment:
                raise MemoryStoreError(
                    "memory_invalid_path",
                    path=path,
                    suggestion=_PATH_SUGGESTION,
                )
            if segment in (".", ".."):
                raise MemoryStoreError(
                    "memory_invalid_path",
                    path=path,
                    suggestion="Path traversal is not allowed. "
                    + _PATH_SUGGESTION,
                )
            if segment.startswith("."):
                raise MemoryStoreError(
                    "memory_invalid_path",
                    path=path,
                    suggestion="Hidden path segments are not allowed. "
                    + _PATH_SUGGESTION,
                )
        if len(segments) < 2:
            raise MemoryStoreError(
                "memory_invalid_path",
                path=path,
                suggestion="Memory files live inside a memory area. "
                + _PATH_SUGGESTION,
            )
        scope = segments[0]
        if scope not in _SCOPES:
            raise MemoryStoreError(
                "memory_path_out_of_scope",
                path=path,
                suggestion=(
                    "Memory paths must start with briefing/, notes/, "
                    "recollection/, or imports/."
                ),
            )
        if scope == BRIEFING_DIR and len(segments) != 2:
            # The briefing compile globs briefing/*.md non-recursively;
            # nested briefing files would be silent dead weight.
            raise MemoryStoreError(
                "memory_invalid_path",
                path=path,
                suggestion="briefing/ is flat: use briefing/<topic>.md.",
            )
        return scope, PurePosixPath(path)

    def _validate_write_path(
        self, path: str,
    ) -> tuple[str, PurePosixPath]:
        scope, logical = self._validate_logical_path(path)
        if scope == IMPORTS_DIR:
            raise MemoryStoreError(
                "memory_scope_readonly",
                path=str(logical),
                suggestion=(
                    "imports/ is importer-owned and read-only; put your "
                    f"own content in {NOTES_DIR}/."
                ),
            )
        if scope == RECOLLECTION_DIR and not self.maintenance:
            raise MemoryStoreError(
                "memory_scope_readonly",
                path=str(logical),
                suggestion=(
                    "recollection/ writes need the explicit maintenance "
                    f"scope; ordinary turns should write {NOTES_DIR}/."
                ),
            )
        if logical.suffix != ".md":
            raise MemoryStoreError(
                "memory_invalid_path",
                path=str(logical),
                suggestion="Memory write targets must end in .md.",
            )
        return scope, logical

    def _scope_writable(self, scope: str) -> bool:
        if scope == IMPORTS_DIR:
            return False
        if scope == RECOLLECTION_DIR:
            return self.maintenance
        return True

    def _physical_path(self, logical: PurePosixPath) -> Path:
        """Map a validated logical path onto the filesystem, rejecting
        symlink targets and any resolution outside the memory root."""
        root = self.memory_root.resolve()
        physical = root.joinpath(*logical.parts)
        if physical.is_symlink():
            raise MemoryStoreError(
                "memory_invalid_path",
                path=str(logical),
                suggestion="Symlinks are not valid memory files.",
            )
        resolved = physical.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise MemoryStoreError(
                "memory_invalid_path",
                path=str(logical),
                suggestion="The path resolves outside the memory root.",
            )
        return physical

    def _validate_size(
        self, scope: str, logical: PurePosixPath, body: str,
    ) -> int:
        """Per-file limit, plus the would-be compiled total for
        ``briefing/`` (the M1 ``save()`` simulation). Briefing-scope
        violations raise ``BriefingCompileError`` so M1 callers see
        the error type they already handle."""
        size = _byte_size(body)
        limit = _FILE_LIMITS[scope]
        if size > limit:
            if scope == BRIEFING_DIR:
                raise BriefingCompileError(
                    "memory_file_too_large",
                    path=str(logical),
                    size=size,
                    limit=limit,
                    suggestion=(
                        "Trim this briefing topic; move detail to "
                        f"memory/{NOTES_DIR}/ (searchable, not injected)."
                    ),
                )
            raise MemoryStoreError(
                "memory_file_too_large",
                path=str(logical),
                size=size,
                limit=limit,
                suggestion=(
                    "Split this file, or move detail into another "
                    "memory area."
                ),
            )
        if scope == BRIEFING_DIR:
            self._validate_briefing_total(logical, body)
        return size

    def _validate_briefing_total(
        self, logical: PurePosixPath, body: str,
    ) -> None:
        profile_body, entries = _read_briefing_entries(
            self.memory_root, enforce_per_file=False,
        )
        entry_map = dict(entries)
        stripped = body.strip()
        if logical.name == PROFILE_BRIEFING_NAME:
            profile_body = stripped
        elif stripped:
            entry_map[logical.stem] = stripped
        else:
            # The compile drops empty bodies; mirror that exactly.
            entry_map.pop(logical.stem, None)
        total = _byte_size(
            _compose_briefing(profile_body, sorted(entry_map.items()))
        )
        if total > TOTAL_LIMIT:
            raise BriefingCompileError(
                "memory_briefing_too_large",
                path=str(logical),
                size=total,
                limit=TOTAL_LIMIT,
                suggestion=(
                    "This write would push the compiled briefing over "
                    f"budget; move topics to memory/{NOTES_DIR}/ first."
                ),
            )

    # ── write plumbing ───────────────────────────────────────────────

    def _write_atomic(self, physical: Path, body: str) -> None:
        """Sibling temp file + ``os.replace``: the target is either
        untouched or fully replaced, never half-written. The temp name
        is non-``.md`` so a crashed write can't leak into the compile."""
        physical.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(physical.parent), prefix=".puffo-mem-", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(tmp_name, physical)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _mark_briefing_dirty(
        self, scope: str, op: str, logical: PurePosixPath,
    ) -> None:
        if scope != BRIEFING_DIR:
            return
        request_prompt_refresh(
            self.workspace_dir, f"memory_store.{op}:{logical}",
        )
