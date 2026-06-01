"""On-disk caches for profile / space / channel names + avatars.

The worker populates these from its existing ``/spaces`` /
``/spaces/<id>/channels`` / ``/identities/profiles`` calls. Readers
(CLI / desktop UI) load them without needing a worker alive or a
signed HPKE request. Per-entry JSON files keep concurrent writers
race-free."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from ..portal.state import home_dir


logger = logging.getLogger(__name__)


def _cache_root() -> Path:
    return home_dir() / "cache"


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, data: dict) -> None:
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    _atomic_write(path, payload)


def avatar_cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        suffix = ".img"
    return _cache_root() / "avatars" / f"{digest}{suffix}"


def persist_profile(slug: str, display_name: str, avatar_url: str) -> None:
    if not slug:
        return
    try:
        _atomic_write_json(
            _cache_root() / "profiles" / f"{_safe(slug)}.json",
            {
                "slug": slug,
                "display_name": display_name,
                "avatar_url": avatar_url,
                "fetched_at": int(time.time()),
            },
        )
    except OSError as exc:
        logger.debug("persist_profile(%s) failed: %s", slug, exc)


def persist_space(space_id: str, name: str) -> None:
    if not space_id or not name:
        return
    try:
        _atomic_write_json(
            _cache_root() / "spaces" / f"{_safe(space_id)}.json",
            {
                "space_id": space_id,
                "name": name,
                "fetched_at": int(time.time()),
            },
        )
    except OSError as exc:
        logger.debug("persist_space(%s) failed: %s", space_id, exc)


def persist_channel(channel_id: str, name: str, space_id: str = "") -> None:
    if not channel_id or not name:
        return
    try:
        _atomic_write_json(
            _cache_root() / "channels" / f"{_safe(channel_id)}.json",
            {
                "channel_id": channel_id,
                "name": name,
                "space_id": space_id,
                "fetched_at": int(time.time()),
            },
        )
    except OSError as exc:
        logger.debug("persist_channel(%s) failed: %s", channel_id, exc)


def write_avatar_bytes(url: str, data: bytes) -> None:
    if not url or not data:
        return
    try:
        _atomic_write(avatar_cache_path(url), data)
    except OSError as exc:
        logger.debug("write_avatar_bytes(%s) failed: %s", url, exc)


def load_profile(slug: str) -> Optional[dict]:
    return _load(_cache_root() / "profiles" / f"{_safe(slug)}.json")


def load_space(space_id: str) -> Optional[dict]:
    return _load(_cache_root() / "spaces" / f"{_safe(space_id)}.json")


def load_channel(channel_id: str) -> Optional[dict]:
    return _load(_cache_root() / "channels" / f"{_safe(channel_id)}.json")


def load_all_profiles() -> dict[str, dict]:
    return _load_dir(_cache_root() / "profiles", "slug")


def load_all_spaces() -> dict[str, dict]:
    return _load_dir(_cache_root() / "spaces", "space_id")


def load_all_channels() -> dict[str, dict]:
    return _load_dir(_cache_root() / "channels", "channel_id")


def _load(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_dir(root: Path, key_field: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    for entry in root.iterdir():
        if not entry.is_file() or entry.suffix != ".json":
            continue
        data = _load(entry)
        if not data:
            continue
        key = data.get(key_field)
        if isinstance(key, str) and key:
            out[key] = data
    return out


def _safe(key: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in key)[:128] or "_"
