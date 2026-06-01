"""Append-only log viewer used by both the runtime-wide and per-agent
panes.

Pin-to-bottom semantics: if the user scrolled up to read something
the new lines do NOT yank them back. Only when their cursor is at
the latest line do we auto-tail.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTextEdit, QWidget


class LogView(QTextEdit):
    """Drop-in log pane.

    Pass a ``snapshot_fn`` returning the full log line list. Optional
    ``filter_fn`` drops lines that don't match the current scope (e.g.
    "this agent's id"). Both are called on every ``poll()``.
    """

    def __init__(
        self,
        snapshot_fn: Callable[[], list[str]],
        filter_fn: Optional[Callable[[str], bool]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 9pt;"
        )
        self._snapshot_fn = snapshot_fn
        self._filter_fn = filter_fn
        self._cursor = 0

    def reset(self) -> None:
        """Re-render from scratch on filter / scope change."""
        self._cursor = 0
        self.clear()

    def set_filter(self, filter_fn: Optional[Callable[[str], bool]]) -> None:
        self._filter_fn = filter_fn
        self.reset()

    def poll(self) -> None:
        snapshot = self._snapshot_fn()
        if self._cursor > len(snapshot):
            # Ring buffer rolled past our cursor — re-render.
            self.reset()
        new_lines = snapshot[self._cursor:]
        self._cursor = len(snapshot)
        if not new_lines:
            return
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
