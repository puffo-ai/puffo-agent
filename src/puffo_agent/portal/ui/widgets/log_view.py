"""Append-only log viewer used by both the runtime-wide and per-agent
panes.

The source is a ring buffer that drops its OLDEST line, so we diff on a
monotonic counter rather than buffer length — otherwise, once the ring
fills, length stays pinned and the view freezes on the lines it first
saw (the oldest). ``setMaximumBlockCount`` bounds the widget to the
latest ``max_lines`` so it always shows the newest.

Pin-to-bottom: if the user scrolled up to read something, new lines do
NOT yank them back; we only auto-tail when they're already at the end.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QWidget


class LogView(QPlainTextEdit):
    """Drop-in log pane.

    ``snapshot_fn`` returns the buffer's current lines (oldest→newest);
    ``counter_fn`` returns the monotonic total-emitted count. Optional
    ``filter_fn`` drops lines outside the current scope (e.g. an agent's
    id). Both fns are called on every ``poll()``.
    """

    def __init__(
        self,
        snapshot_fn: Callable[[], list[str]],
        counter_fn: Callable[[], int],
        filter_fn: Optional[Callable[[str], bool]] = None,
        parent: Optional[QWidget] = None,
        max_lines: int = 500,
    ) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(max_lines)
        self.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 9pt;"
        )
        self._snapshot_fn = snapshot_fn
        self._counter_fn = counter_fn
        self._filter_fn = filter_fn
        self._seen = 0

    def reset(self) -> None:
        """Re-render from scratch on filter / scope change."""
        self._seen = 0
        self.clear()

    def set_filter(self, filter_fn: Optional[Callable[[str], bool]]) -> None:
        self._filter_fn = filter_fn
        self.reset()

    def poll(self) -> None:
        total = self._counter_fn()
        if total < self._seen:
            # Counter went backwards (handler replaced) — re-render.
            self.reset()
        if total <= self._seen:
            return
        snapshot = self._snapshot_fn()
        delta = total - self._seen
        self._seen = total
        # New lines are the last ``delta`` of the snapshot; if we fell
        # behind by more than the ring holds, only the latest survive.
        new_lines = snapshot[-delta:] if delta < len(snapshot) else snapshot
        if self._filter_fn:
            new_lines = [line for line in new_lines if self._filter_fn(line)]
        if not new_lines:
            return
        scrollbar = self.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        prefix = "\n" if self.toPlainText() else ""
        cursor.insertText(prefix + "\n".join(new_lines))
        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())
