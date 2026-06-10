"""Status page: the MCP server subprocesses the agents are running,
grouped by agent.

These are claude/codex grandchildren (one set per agent), so a busy
fleet shows many rows. Structure (which servers exist) is rebuilt only
when it changes; CPU/Mem/Status are updated in place each poll so the
group expansion state survives.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..mcp_probe import McpProbe

_COLUMNS = ("Agent / Server", "PID", "Status", "CPU %", "Mem (MB)")

_GREEN = QColor("#16a34a")
_AMBER = QColor("#d97706")
_RED = QColor("#dc2626")
_GRAY = QColor("#6b7280")

_ALIVE = {"running", "sleeping", "disk-sleep", "disk_sleep", "idle", "waking"}
_STOPPED = {"stopped", "stopped-trace", "stopped_trace"}
_DEAD = {"zombie", "dead"}


def _status_style(raw: str) -> tuple[str, QColor]:
    r = (raw or "").lower()
    if r in _ALIVE:
        return "running", _GREEN          # 'sleeping' = idle but healthy
    if r in _STOPPED:
        return raw, _AMBER
    if r in _DEAD:
        return raw, _RED
    return raw or "?", _GRAY


class McpStatusView(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._probe = McpProbe()
        self._structure_key: Optional[tuple] = None
        self._items: dict[int, QTreeWidgetItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        self._title = QLabel("MCP servers")
        self._title.setStyleSheet(
            "font-size: 14pt; font-weight: 600; color: #1f2937;"
        )
        layout.addWidget(self._title)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(len(_COLUMNS))
        self._tree.setHeaderLabels(list(_COLUMNS))
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, len(_COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        layout.addWidget(self._tree, stretch=1)

    def poll(self) -> None:
        rows = self._probe.sample()
        self._title.setText(f"MCP servers — {len(rows)} running")

        groups: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            groups.setdefault((row["agent"], row["agent_name"]), []).append(row)

        structure_key = tuple(
            (key, tuple(sorted(r["pid"] for r in items)))
            for key, items in sorted(groups.items())
        )
        if structure_key != self._structure_key:
            self._rebuild(groups)
            self._structure_key = structure_key
        else:
            for row in rows:
                item = self._items.get(row["pid"])
                if item is not None:
                    self._set_metrics(item, row)

    def _rebuild(self, groups: dict[tuple[str, str], list[dict]]) -> None:
        self._tree.clear()
        self._items = {}
        for (_agent_id, agent_name), items in sorted(
            groups.items(), key=lambda kv: kv[0][1].lower()
        ):
            top = QTreeWidgetItem([f"{agent_name}  ·  {len(items)}", "", "", "", ""])
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            self._tree.addTopLevelItem(top)
            for row in sorted(items, key=lambda r: r["server"]):
                child = QTreeWidgetItem()
                child.setText(0, row["server"])
                self._set_metrics(child, row)
                top.addChild(child)
                self._items[row["pid"]] = child
            top.setExpanded(True)

    @staticmethod
    def _set_metrics(item: QTreeWidgetItem, row: dict) -> None:
        item.setText(1, str(row["pid"]))
        label, color = _status_style(row["status"])
        item.setText(2, label)
        item.setForeground(2, color)
        item.setText(3, f"{row['cpu']:.0f}")
        item.setText(4, f"{row['mem_mb']:.0f}")
        for col in (1, 3, 4):
            item.setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
