"""Left-most vertical rail (Home / Agents)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QPushButton, QVBoxLayout, QWidget


class Rail(QWidget):
    """Two buttons stacked vertically: ``Home`` and ``Agents``."""

    section_changed = Signal(str)  # "home" or "agents"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(72)
        self.setStyleSheet(
            "QWidget { background-color: #111827; }"
            "QPushButton { color: #cbd5e1; background-color: transparent;"
            "              border: 0; padding: 12px 0; font-size: 9pt; }"
            "QPushButton:checked { color: white; background-color: #1f2937;"
            "                      border-left: 3px solid #3b82f6; }"
            "QPushButton:hover { color: white; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._home_btn = self._make_button("🏠\nHome", "home")
        self._agents_btn = self._make_button("👥\nAgents", "agents")
        layout.addWidget(self._home_btn)
        layout.addWidget(self._agents_btn)
        layout.addStretch(1)
        self._home_btn.setChecked(True)

    def _make_button(self, text: str, key: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setFlat(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.toggled.connect(lambda checked, k=key: self._on_toggled(checked, k))
        self._group.addButton(btn)
        return btn

    def _on_toggled(self, checked: bool, key: str) -> None:
        if checked:
            self.section_changed.emit(key)
