"""Structured memory errors, hoisted out of ``memory.py``.

These live in their own leaf module so every layer of the memory stack
(``memory`` M1, ``memory_store`` M2, ``memory_tools`` M3, and the
``portal`` callers) can import the error types without pulling in — or
being pulled in by — the higher layers. ``memory`` re-exports both
names, so ``from .memory import MemoryStoreError`` keeps working.
"""

from __future__ import annotations


class MemoryStoreError(Exception):
    """Structured memory-store error: the M1 ``{path, size, limit,
    suggestion}`` shape extended with ``code`` (M3-aligned names such
    as ``memory_invalid_path`` / ``memory_scope_readonly``).
    ``size``/``limit`` only apply to size violations; errors without
    them omit the keys from ``to_dict()``."""

    def __init__(
        self,
        code: str,
        *,
        path: str,
        suggestion: str,
        size: int | None = None,
        limit: int | None = None,
    ):
        self.code = code
        self.path = path
        self.size = size
        self.limit = limit
        self.suggestion = suggestion
        if size is not None and limit is not None:
            message = (
                f"{code}: {path} is {size} bytes (limit {limit}). {suggestion}"
            )
        else:
            message = f"{code}: {path}. {suggestion}"
        super().__init__(message)

    def to_dict(self) -> dict:
        out: dict = {"code": self.code, "path": self.path}
        if self.size is not None:
            out["size"] = self.size
        if self.limit is not None:
            out["limit"] = self.limit
        out["suggestion"] = self.suggestion
        return out


class BriefingCompileError(MemoryStoreError):
    """A briefing violates the compile budget. Fail closed — callers
    get no truncated/partial output. ``code`` is one of
    ``memory_file_too_large`` / ``memory_briefing_too_large``
    (M3-aligned names)."""

    def __init__(
        self,
        code: str,
        *,
        path: str,
        size: int,
        limit: int,
        suggestion: str,
    ):
        super().__init__(
            code, path=path, size=size, limit=limit, suggestion=suggestion,
        )


class MemoryHistoryError(Exception):
    """Structured memory-history error: the M4 read-only audit query
    analogue of ``MemoryStoreError``. Carries ``code`` (one of the M4
    history codes such as ``memory_history_unavailable`` /
    ``memory_history_not_initialized`` / ``memory_invalid_history_query``
    / ``memory_history_query_too_large`` / ``memory_history_read_failed``),
    plus a human ``message`` and a ``suggestion``. Unlike
    ``MemoryStoreError`` it carries no ``path``/``size``/``limit`` — a
    history query is over the audit log, not a single file — so the
    tools layer maps it to the ``{code, message, suggestion}`` envelope
    with a ``memory_git`` cause layer."""

    def __init__(self, code: str, *, message: str, suggestion: str):
        self.code = code
        self.message = message
        self.suggestion = suggestion
        super().__init__(f"{code}: {message} {suggestion}".rstrip())

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion,
        }
