"""Status page: the MCP server subprocesses the agents are running.

These are claude/codex grandchildren (one set per agent), so a busy
fleet shows many rows — the table makes 'what's running and how heavy'
legible at a glance.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..mcp_probe import McpProbe

_COLUMNS = ("Agent", "Server", "PID", "Status", "CPU %", "Mem (MB)")


class McpStatusView(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._probe = McpProbe()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        self._title = QLabel("MCP servers")
        self._title.setStyleSheet(
            "font-size: 14pt; font-weight: 600; color: #1f2937;"
        )
        layout.addWidget(self._title)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in range(2, len(_COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        layout.addWidget(self._table, stretch=1)

    def poll(self) -> None:
        rows = self._probe.sample()
        self._title.setText(f"MCP servers — {len(rows)} running")
        self._table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            cells = (
                row["agent"],
                row["server"],
                str(row["pid"]),
                row["status"],
                f"{row['cpu']:.0f}",
                f"{row['mem_mb']:.0f}",
            )
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._table.setItem(r, c, item)
