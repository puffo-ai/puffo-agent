"""Avatar rendering: deterministic initial circle + disk-cache lookup.

The worker writes fetched avatars to
``~/.puffo-agent/cache/avatars/<sha256(url)><ext>`` (signed GET via its
own keystore — see ``puffo_agent.agent.puffo_core_client
._fetch_and_cache_avatar``). This widget never makes its own HTTP
request because the blob endpoint rejects unsigned reads (401).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QFileSystemWatcher, QObject, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap

from ....agent.disk_cache import avatar_cache_path


_PALETTE = [
    "#ef4444", "#f97316", "#eab308", "#22c55e",
    "#06b6d4", "#3b82f6", "#8b5cf6", "#ec4899",
    "#14b8a6", "#a855f7", "#f59e0b", "#10b981",
]


def colour_for(key: str) -> QColor:
    if not key:
        return QColor("#9aa0a6")
    digest = hashlib.md5(key.encode("utf-8")).digest()
    return QColor(_PALETTE[digest[0] % len(_PALETTE)])


def initial_pixmap(name: str, key: str, size: int = 40) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(Qt.NoPen))
        painter.setBrush(QBrush(colour_for(key or name)))
        painter.drawEllipse(0, 0, size, size)
        letter = (name or "?").strip()[:1].upper() or "?"
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(size * 0.5))
        painter.setFont(font)
        painter.setPen(QPen(QColor("white")))
        painter.drawText(pm.rect(), Qt.AlignCenter, letter)
    finally:
        painter.end()
    return pm


def _clip_to_circle(src: QPixmap, size: int) -> QPixmap:
    scaled = src.scaled(
        size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
    )
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QBrush(scaled))
        painter.setPen(QPen(Qt.NoPen))
        painter.drawEllipse(0, 0, size, size)
    finally:
        painter.end()
    return out


class AvatarCache(QObject):
    """Disk-backed avatar lookup. Construct once in MainWindow; share
    across every widget that paints an avatar."""

    avatar_ready = Signal()  # any cached file changed

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._mem: Dict[tuple[str, int], QPixmap] = {}
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir_changed)
        try:
            cache_dir = avatar_cache_path("x").parent
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._watcher.addPath(str(cache_dir))
        except OSError:
            pass

    def pixmap(
        self, url: str, key: str, fallback_name: str, size: int = 40,
    ) -> QPixmap:
        if not url:
            return initial_pixmap(fallback_name, key, size)
        cached = avatar_cache_path(url)
        if not cached.exists():
            return initial_pixmap(fallback_name, key, size)
        entry = self._mem.get((url, size))
        if entry is None:
            src = QPixmap(str(cached))
            entry = (
                _clip_to_circle(src, size)
                if not src.isNull()
                else initial_pixmap(fallback_name, key, size)
            )
            self._mem[(url, size)] = entry
        return entry

    def _on_dir_changed(self, _path: str) -> None:
        # New file appeared — drop the entire memo and let next paint
        # rebuild. Cheap (a handful of pixmaps), and avoids tracking
        # per-url mtimes.
        self._mem.clear()
        self.avatar_ready.emit()
