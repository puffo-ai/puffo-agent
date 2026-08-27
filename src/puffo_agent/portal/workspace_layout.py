"""Filesystem layout shared by all daemon-owned Agent workspaces."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path


logger = logging.getLogger(__name__)
SHARED_WORKSPACE_NAME = "shared"
AVAILABLE_SHARED_WORKSPACE_STATES = frozenset({"created", "existing", "mounted"})


def _same_location(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _create_windows_junction(link: Path, target: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False
    return completed.returncode == 0


def ensure_workspace_shared_link(workspace: Path, shared_root: Path) -> str:
    """Expose one Puffo-home shared root as ``<workspace>/shared``.

    Existing real files and directories are never replaced. A stale symlink is
    safe to replace because the link itself owns no content. Returns one of
    ``created``, ``existing``, ``conflict``, or ``unavailable``.
    """
    workspace = workspace.expanduser()
    shared_root = shared_root.expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    shared_existed = shared_root.exists()
    shared_root.mkdir(parents=True, exist_ok=True)
    if not shared_existed and os.name != "nt":
        shared_root.chmod(0o700)

    link = workspace / SHARED_WORKSPACE_NAME
    if link.exists() and _same_location(link, shared_root):
        return "existing"
    if link.is_symlink():
        link.unlink()
    elif link.is_dir():
        # Docker creates an empty mountpoint when ``shared`` is bind-mounted
        # below the workspace mount. Replacing that empty directory is safe and
        # keeps a later Docker -> local runtime switch from becoming a false
        # migration conflict.
        try:
            link.rmdir()
        except OSError:
            logger.warning(
                "workspace shared path %s contains local data; preserving it "
                "instead of replacing it with the Puffo shared workspace",
                link,
            )
            return "conflict"
    elif link.exists():
        logger.warning(
            "workspace shared path %s already contains local data; preserving it "
            "instead of replacing it with the Puffo shared workspace",
            link,
        )
        return "conflict"

    relative_target = os.path.relpath(shared_root, start=workspace)
    try:
        os.symlink(relative_target, link, target_is_directory=True)
    except OSError as exc:
        if _create_windows_junction(link, shared_root):
            return "created"
        logger.warning(
            "could not expose Puffo shared workspace %s at %s: %s",
            shared_root,
            link,
            exc,
        )
        return "unavailable"
    return "created"


def prepare_workspace_shared_access(
    workspace: Path,
    shared_root: Path,
    *,
    mounted: bool = False,
) -> str:
    """Prepare one runtime's shared path and return its prompt-facing state.

    Docker exposes the shared root with a bind mount, so it removes a prior
    local-runtime symlink instead of mounting over it. Local runtimes use the
    symlink. Existing real content is never hidden by either mode.
    """
    if not mounted:
        return ensure_workspace_shared_link(workspace, shared_root)
    workspace = workspace.expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    shared_root = shared_root.expanduser()
    shared_existed = shared_root.exists()
    shared_root.mkdir(parents=True, exist_ok=True)
    if not shared_existed and os.name != "nt":
        shared_root.chmod(0o700)

    mountpoint = workspace / SHARED_WORKSPACE_NAME
    if mountpoint.is_symlink():
        mountpoint.unlink()
    elif mountpoint.is_dir():
        try:
            next(mountpoint.iterdir())
        except StopIteration:
            pass
        except OSError:
            return "unavailable"
        else:
            logger.warning(
                "workspace shared path %s contains local data; preserving it "
                "instead of hiding it with the Puffo shared-workspace mount",
                mountpoint,
            )
            return "conflict"
    elif mountpoint.exists():
        logger.warning(
            "workspace shared path %s contains local data; preserving it "
            "instead of hiding it with the Puffo shared-workspace mount",
            mountpoint,
        )
        return "conflict"
    return "mounted"
