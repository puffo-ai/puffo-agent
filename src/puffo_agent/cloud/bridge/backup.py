"""Backup manifest for cloud agents.

The three-layer state model (architecture.md): config (control-plane
fact source), rebuildable (messages.db, transcript — droppable), and
irreplaceable (agent memory + produced artifacts). Only the
irreplaceable layer is backed up. The Agent Instance Manager *pulls*
these paths via the E2B FS API (never a sandbox push, anti-injection),
encrypts, and writes them to versioned S3.

This module is just the contract — the path lists AIM consumes. The
sandbox keeps writing memory to its usual dir (existing memory.py
behaviour); nothing here runs in-sandbox.
"""

from __future__ import annotations

# Irreplaceable layer, relative to the agent dir. Directories are
# pulled recursively, minus BACKUP_EXCLUDE_PATHS below.
BACKUP_INCLUDE_PATHS: tuple[str, ...] = (
    "memory",
    "workspace",
)

# Never back up: identity material (posture B keeps none in-sandbox,
# but exclude defensively), rebuildable state, harness session/transcript
# caches, and device-bound flags. Matched as path prefixes under the
# included dirs or the agent root.
BACKUP_EXCLUDE_PATHS: tuple[str, ...] = (
    "keys",
    "messages.db",
    "runtime.json",
    "cli_session.json",
    "codex_session.json",
    "workspace/.claude",
    "workspace/.codex/auth.json",
    "workspace/.puffo-agent",
    ".puffo-agent",
)


def is_backed_up(rel_path: str) -> bool:
    """True when ``rel_path`` (POSIX, relative to the agent dir) falls
    in the irreplaceable layer and isn't excluded."""
    norm = rel_path.replace("\\", "/").lstrip("/")
    if any(norm == ex or norm.startswith(ex + "/") for ex in BACKUP_EXCLUDE_PATHS):
        return False
    return any(
        norm == inc or norm.startswith(inc + "/") for inc in BACKUP_INCLUDE_PATHS
    )
