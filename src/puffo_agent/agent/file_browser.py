"""Read-only file browser served over the daemon's WebSocket RPC.

Executes ``list_files`` / ``read_file`` RPCs against a whitelist of
directories; never exposes secrets or anything outside the whitelist.
"""

from __future__ import annotations

import logging
import os
from typing import Tuple

logger = logging.getLogger(__name__)

# Whitelisted relative directories under BASE_DIR. config.yml is
# intentionally excluded — it contains API keys.
ALLOWED_ROOTS = ("memory", "skills", "agents")

MAX_FILE_BYTES = 256 * 1024  # 256 KB


def _resolve(base_dir: str, raw_path: str) -> str | None:
    """Resolve ``raw_path`` to an absolute path under one of
    ``ALLOWED_ROOTS``. Returns ``None`` on whitelist escape attempts
    (``../``, absolute paths, or paths outside ``ALLOWED_ROOTS``).
    """
    # Accept "/memory/foo" or "memory/foo".
    cleaned = (raw_path or "").lstrip("/\\")
    if not cleaned:
        return base_dir

    abs_path = os.path.abspath(os.path.join(base_dir, cleaned))
    # Block ../ escapes on all OSes.
    try:
        rel = os.path.relpath(abs_path, base_dir)
    except ValueError:
        return None
    if rel.startswith("..") or os.path.isabs(rel):
        return None

    first = rel.replace("\\", "/").split("/", 1)[0]
    if first not in ALLOWED_ROOTS:
        return None
    return abs_path


class FileBrowser:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir

    async def __call__(self, cmd: str, args: dict) -> Tuple[bool, dict, str | None]:
        if cmd == "list_files":
            return self.list_files(args.get("path", ""))
        if cmd == "read_file":
            return self.read_file(args.get("path", ""))
        return False, {}, f"unknown cmd: {cmd}"

    def list_files(self, path: str) -> Tuple[bool, dict, str | None]:
        if not path:
            # Empty path returns the allowed-roots set for the top of
            # the tree.
            return True, {
                "path": "",
                "entries": [{"name": r, "type": "dir"} for r in ALLOWED_ROOTS],
            }, None

        abs_path = _resolve(self.base_dir, path)
        if abs_path is None:
            return False, {}, "path not allowed"
        if not os.path.isdir(abs_path):
            return False, {}, "not a directory"

        entries = []
        try:
            for name in sorted(os.listdir(abs_path)):
                full = os.path.join(abs_path, name)
                entries.append({
                    "name": name,
                    "type": "dir" if os.path.isdir(full) else "file",
                })
        except OSError as e:
            return False, {}, str(e)
        return True, {"path": path, "entries": entries}, None

    def read_file(self, path: str) -> Tuple[bool, dict, str | None]:
        abs_path = _resolve(self.base_dir, path)
        if abs_path is None:
            return False, {}, "path not allowed"
        if not os.path.isfile(abs_path):
            return False, {}, "not a file"

        try:
            size = os.path.getsize(abs_path)
            if size > MAX_FILE_BYTES:
                return False, {}, f"file too large ({size} bytes, max {MAX_FILE_BYTES})"
            with open(abs_path, "rb") as f:
                raw = f.read()
        except OSError as e:
            return False, {}, str(e)

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False, {}, "binary file"

        return True, {"path": path, "content": content, "size": size}, None
