"""Contacts-app style left sidebar listing all agents.

Each row: avatar circle, name + role_short on top, harness · model on
the second line; status dot floats on the right. Sort: running-first,
then starting / paused / error / stopped, then by name.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import asyncio
import threading

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...state import AgentConfig, RuntimeState, discover_agents
from .avatar import AvatarCache, initial_pixmap


_STATUS_ORDER = {"running": 0, "starting": 1, "paused": 2, "error": 3, "stopped": 4}
_STATUS_COLOUR = {
    "running": "#22c55e",
    "starting": "#d8b834",
    "paused": "#9aa0a6",
    "error": "#ef4444",
    "stopped": "#9aa0a6",
}


@dataclass
class AgentSummary:
    id: str
    display_name: str
    role_short: str
    status: str
    harness: str
    model: str
    slug: str
    avatar_url: str

    @classmethod
    def for_id(cls, agent_id: str) -> "AgentSummary":
        display_name = agent_id
        role_short = ""
        harness = ""
        model = ""
        slug = ""
        avatar_url = ""
        try:
            cfg = AgentConfig.load(agent_id)
            display_name = cfg.display_name or agent_id
            role_short = cfg.role_short
            harness = cfg.runtime.harness
            model = cfg.runtime.model
            slug = cfg.puffo_core.slug
            avatar_url = cfg.avatar_url
        except Exception:
            pass
        rt = RuntimeState.load(agent_id)
        status = rt.status if rt else "stopped"
        return cls(
            id=agent_id,
            display_name=display_name,
            role_short=role_short,
            status=status,
            harness=harness,
            model=model,
            slug=slug,
            avatar_url=avatar_url,
        )


class _Row(QWidget):
    def __init__(
        self,
        summary: AgentSummary,
        avatar_cache: AvatarCache,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._avatar_cache = avatar_cache
        self._summary = summary

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 10, 6)
        layout.setSpacing(10)

        self._avatar_label = QLabel()
        self._avatar_label.setFixedSize(40, 40)
        layout.addWidget(self._avatar_label)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        self._name_label = QLabel()
        self._name_label.setTextInteractionFlags(Qt.NoTextInteraction)
        font = QFont()
        font.setBold(True)
        self._name_label.setFont(font)
        text_col.addWidget(self._name_label)
        self._sub_label = QLabel()
        self._sub_label.setStyleSheet("color: #6b7280; font-size: 8.5pt;")
        text_col.addWidget(self._sub_label)
        layout.addLayout(text_col, stretch=1)

        self._dot = QLabel("●")
        self._dot.setStyleSheet("font-size: 14pt;")
        layout.addWidget(self._dot, alignment=Qt.AlignVCenter)

        self.apply(summary)

    def apply(self, summary: AgentSummary) -> None:
        self._summary = summary
        if self._avatar_cache:
            pm = self._avatar_cache.pixmap(
                summary.avatar_url,
                summary.slug or summary.id,
                summary.display_name,
                size=40,
            )
        else:
            pm = initial_pixmap(summary.display_name, summary.slug or summary.id, 40)
        self._avatar_label.setPixmap(pm)
        name = summary.display_name
        if summary.role_short:
            name = f"{name} ({summary.role_short})"
        self._name_label.setText(name)
        runtime_blurb_parts = [p for p in (summary.harness, summary.model) if p]
        self._sub_label.setText(" · ".join(runtime_blurb_parts) or "—")
        self._dot.setStyleSheet(
            f"color: {_STATUS_COLOUR.get(summary.status, '#9aa0a6')}; font-size: 14pt;"
        )
        self._dot.setToolTip(summary.status)


class AgentList(QWidget):
    """Sidebar list — emits ``agent_selected`` with id (or None)."""

    agent_selected = Signal(object)
    # Background import worker → main thread; bool = success, str = summary.
    _import_done = Signal(bool, str)
    _archive_check_done = Signal(bool, str)

    def __init__(
        self,
        avatar_cache: Optional[AvatarCache] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._avatar_cache = avatar_cache
        self._rows: dict[str, _Row] = {}
        self._selected_id: Optional[str] = None
        self._running_only = True
        self._build()
        self._import_done.connect(self._on_import_finished)
        self._archive_check_done.connect(self._on_archive_check_finished)
        if avatar_cache is not None:
            avatar_cache.avatar_ready.connect(self._on_avatar_ready)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QWidget()
        toolbar.setStyleSheet("background-color: #f3f4f6;")
        t_layout = QHBoxLayout(toolbar)
        t_layout.setContentsMargins(10, 6, 10, 6)
        self._import_btn = QPushButton("Import agent")
        self._import_btn.clicked.connect(self._on_import_clicked)
        t_layout.addWidget(self._import_btn)
        self._archive_check_btn = QPushButton("Archive check")
        self._archive_check_btn.setToolTip(
            "Verify every locally-archived agent is also revoked "
            "server-side; re-issue the revoke for any that aren't."
        )
        self._archive_check_btn.clicked.connect(self._on_archive_check_clicked)
        t_layout.addWidget(self._archive_check_btn)
        t_layout.addStretch(1)
        layout.addWidget(toolbar)

        header = QWidget()
        header.setStyleSheet("background-color: #f9fafb; border-top: 1px solid #e5e7eb;")
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(10, 6, 10, 6)
        # Default-on running filter; checkbox is the escape hatch
        # that surfaces paused / errored / stopped rows.
        self._show_all_toggle = QCheckBox("Show all")
        self._show_all_toggle.setChecked(False)
        self._show_all_toggle.toggled.connect(self._on_show_all_toggled)
        h_layout.addWidget(self._show_all_toggle)
        h_layout.addStretch(1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #6b7280; font-size: 9pt;")
        h_layout.addWidget(self._count_label)
        layout.addWidget(header)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SingleSelection)
        self._list.setUniformItemSizes(True)
        self._list.setSpacing(0)
        self._list.setStyleSheet(
            "QListWidget::item { border-bottom: 1px solid #eee; }"
            "QListWidget::item:selected { background-color: #e0f2fe; color: black; }"
        )
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list, stretch=1)

    def refresh(self, summaries_loader: Callable[[], list[AgentSummary]] | None = None) -> None:
        loader = summaries_loader or _default_loader
        all_rows = loader()
        all_rows.sort(key=lambda s: (_STATUS_ORDER.get(s.status, 9), s.display_name.lower(), s.id))
        rows = [r for r in all_rows if r.status == "running"] if self._running_only else all_rows

        running_count = sum(1 for r in all_rows if r.status == "running")
        self._count_label.setText(f"{running_count} running / {len(all_rows)} total")

        existing_ids = [self._list.item(i).data(Qt.UserRole) for i in range(self._list.count())]
        new_ids = [r.id for r in rows]
        if existing_ids != new_ids:
            preserved = self._selected_id
            self._list.blockSignals(True)
            self._list.clear()
            self._rows.clear()
            for row in rows:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, row.id)
                item.setSizeHint(QSize(0, 60))
                widget = _Row(row, self._avatar_cache)
                self._rows[row.id] = widget
                self._list.addItem(item)
                self._list.setItemWidget(item, widget)
                if row.id == preserved:
                    item.setSelected(True)
            self._list.blockSignals(False)
            return

        for row in rows:
            widget = self._rows.get(row.id)
            if widget is not None:
                widget.apply(row)

    @property
    def selected_id(self) -> Optional[str]:
        return self._selected_id

    def clear_selection(self) -> None:
        self._list.clearSelection()
        self._selected_id = None
        self.agent_selected.emit(None)

    def _on_selection_changed(self) -> None:
        items = self._list.selectedItems()
        new_id = items[0].data(Qt.UserRole) if items else None
        if new_id == self._selected_id:
            return
        self._selected_id = new_id
        self.agent_selected.emit(new_id)

    def _on_show_all_toggled(self, checked: bool) -> None:
        self._running_only = not checked
        self.refresh()

    def _on_import_clicked(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import agent archive",
            "",
            "Puffo Agent Export (*.puffoagent);;All files (*)",
        )
        if not path:
            return
        password, ok = QInputDialog.getText(
            self, "Import agent",
            "Decryption password:",
            QLineEdit.Password,
        )
        if not ok or not password:
            return
        try:
            blob = open(path, "rb").read()
        except OSError as exc:
            QMessageBox.warning(self, "Import", f"could not read archive: {exc}")
            return

        self._import_btn.setEnabled(False)
        self._import_btn.setText("Importing…")

        def worker() -> None:
            try:
                from ...import_agents import import_bundle, ImportError as _ImportError
                report = asyncio.run(import_bundle(blob, password))
                msg = "\n".join(
                    f"{r.agent_id}: {r.status}" + (f" — {r.detail}" if r.detail else "")
                    for r in report.results
                ) or "(no agents in bundle)"
                self._import_done.emit(True, msg)
            except Exception as exc:
                self._import_done.emit(False, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_import_finished(self, success: bool, summary: str) -> None:
        self._import_btn.setEnabled(True)
        self._import_btn.setText("Import agent")
        title = "Import agent" + ("" if success else " — failed")
        if success:
            QMessageBox.information(self, title, summary)
            self.refresh()
        else:
            QMessageBox.warning(self, title, summary)

    def _on_archive_check_clicked(self) -> None:
        self._archive_check_btn.setEnabled(False)
        self._archive_check_btn.setText("Checking…")

        def worker() -> None:
            try:
                from ...import_agents import (
                    ArchiveCheckOutcome,
                    sweep_archive_check,
                )
                results = asyncio.run(sweep_archive_check())
                if not results:
                    self._archive_check_done.emit(True, "no archived agents to check")
                    return
                mark = {
                    ArchiveCheckOutcome.CONSISTENT: "ok",
                    ArchiveCheckOutcome.RECONCILED: "revoked",
                    ArchiveCheckOutcome.DEVICE_NOT_FOUND: "device-not-found",
                    ArchiveCheckOutcome.NO_KEYS: "no-keys",
                    ArchiveCheckOutcome.UNREACHABLE: "unreachable",
                }
                lines = [
                    f"[{mark[r.outcome]}] {r.dir_name}"
                    + (f"  ({r.detail})" if r.detail else "")
                    for r in results
                ]
                reconciled = sum(
                    1 for r in results if r.outcome is ArchiveCheckOutcome.RECONCILED
                )
                problems = sum(
                    1 for r in results
                    if r.outcome in (
                        ArchiveCheckOutcome.UNREACHABLE,
                        ArchiveCheckOutcome.NO_KEYS,
                        ArchiveCheckOutcome.DEVICE_NOT_FOUND,
                    )
                )
                lines.append(
                    f"\n{len(results)} checked, {reconciled} revoked, "
                    f"{problems} needing attention"
                )
                self._archive_check_done.emit(problems == 0, "\n".join(lines))
            except Exception as exc:
                self._archive_check_done.emit(False, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_archive_check_finished(self, ok: bool, summary: str) -> None:
        self._archive_check_btn.setEnabled(True)
        self._archive_check_btn.setText("Archive check")
        title = "Archive check" + ("" if ok else " — attention required")
        if ok:
            QMessageBox.information(self, title, summary)
        else:
            QMessageBox.warning(self, title, summary)

    def _on_avatar_ready(self) -> None:
        # Re-paint visible rows so newly cached pixmaps land.
        for row_widget in self._rows.values():
            row_widget.apply(row_widget._summary)


def _default_loader() -> list[AgentSummary]:
    return [AgentSummary.for_id(aid) for aid in discover_agents()]
