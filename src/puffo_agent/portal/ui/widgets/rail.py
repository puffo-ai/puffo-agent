"""Left-most vertical rail (Home / Agents)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QPushButton, QVBoxLayout, QWidget


class Rail(QWidget):
    """Two buttons stacked vertically: ``Home`` and ``Agents``."""

    section_changed = Signal(str)  # "home" / "operators" / "agents" / "logs" / "status"

    _BUTTON_QSS = (
        "QPushButton { color: #1f2937; background-color: transparent;"
        "              border: 0; padding: 12px 0; font-size: 9pt;"
        "              font-weight: 500; }"
        "QPushButton:hover { color: white; background-color: #6b7280; }"
        "QPushButton:checked { color: white; background-color: #111827;"
        "                       border-left: 3px solid #3b82f6; }"
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(72)
        # Rail blends with the app surface so unselected tabs read as
        # plain text — selection is the only "dark chip".
        self.setStyleSheet(
            "Rail { background-color: #f8fafc;"
            "       border-right: 1px solid #e5e7eb; }"
        )
        self.setAutoFillBackground(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._home_btn = self._make_button("🏠\nHome", "home")
        self._operators_btn = self._make_button("🔗\nOperators", "operators")
        self._agents_btn = self._make_button("👥\nAgents", "agents")
        self._logs_btn = self._make_button("📜\nLogs", "logs")
        self._status_btn = self._make_button("🔌\nStatus", "status")
        layout.addWidget(self._home_btn)
        layout.addWidget(self._operators_btn)
        layout.addWidget(self._agents_btn)
        layout.addWidget(self._logs_btn)
        layout.addWidget(self._status_btn)
        layout.addStretch(1)
        self._home_btn.setChecked(True)

    def _make_button(self, text: str, key: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(self._BUTTON_QSS)
        btn.toggled.connect(lambda checked, k=key: self._on_toggled(checked, k))
        self._group.addButton(btn)
        return btn

    def _on_toggled(self, checked: bool, key: str) -> None:
        if checked:
            self.section_changed.emit(key)
