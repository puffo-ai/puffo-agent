"""Qt main window: agents table + filters + detail + log pane.

State comes from disk (``agent.yml`` + ``runtime.json``) via a 500 ms
``QTimer``. The in-memory ``Worker.runtime`` isn't accessed across
threads — disk heartbeats are stale by at most one reconcile tick.

Actions reuse the same file sentinels the HTTP handlers write
(``restart.flag`` / ``archive.flag``) or flip ``agent.yml``'s state
for pause/resume. The reconcile loop applies them on its next tick
(~2s).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..state import (
    AgentConfig,
    RuntimeState,
    archive_flag_path,
    discover_agents,
    restart_flag_path,
)
from .daemon_thread import DaemonThread
from .log_buffer import LogRingHandler


_STATUS_COLOR = {
    "running": QColor("#22c55e"),
    "paused": QColor("#9aa0a6"),
    "error": QColor("#ef4444"),
    "stopped": QColor("#9aa0a6"),
    "starting": QColor("#d8b834"),
}

_STATUS_LABEL = {
    "running": "Online",
    "paused": "Paused",
    "error": "Error",
    "stopped": "Stopped",
    "starting": "Starting",
}

_COLUMNS = [
    "Name",
    "Role",
    "Slug",
    "Owner",
    "Status",
    "Runtime",
    "Harness",
    "Model",
    "Server",
]

_DEFAULT_COL_WIDTHS = {
    0: 160,   # Name
    1: 220,   # Role
    2: 180,   # Slug
    3: 150,   # Owner
    4: 100,   # Status
    5: 110,   # Runtime
    6: 110,   # Harness
    7: 140,   # Model
    8: 260,   # Server
}


class _AgentRow:
    """Cached per-agent snapshot used by the table + filter logic."""

    __slots__ = (
        "id",
        "display_name",
        "role",
        "slug",
        "owner",
        "status",
        "runtime_kind",
        "harness",
        "model",
        "server",
        "state",
    )

    def __init__(self, agent_id: str) -> None:
        self.id = agent_id
        self.display_name = agent_id
        self.role = ""
        self.slug = ""
        self.owner = ""
        self.status = "stopped"
        self.runtime_kind = ""
        self.harness = ""
        self.model = ""
        self.server = ""
        self.state = "running"

    def load(self) -> "_AgentRow":
        try:
            cfg = AgentConfig.load(self.id)
            self.display_name = cfg.display_name or self.id
            self.role = cfg.role
            self.slug = cfg.puffo_core.slug
            self.owner = cfg.puffo_core.operator_slug
            self.runtime_kind = cfg.runtime.kind
            self.harness = cfg.runtime.harness
            self.model = cfg.runtime.model
            self.server = cfg.puffo_core.server_url
            self.state = cfg.state
        except Exception:
            pass
        rt = RuntimeState.load(self.id)
        self.status = rt.status if rt else "stopped"
        return self


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        daemon_thread: DaemonThread,
        log_buffer: LogRingHandler,
    ) -> None:
        super().__init__()
        self._daemon_thread = daemon_thread
        self._log_buffer = log_buffer
        self._selected_id: Optional[str] = None
        self._stop_requested = False
        self._log_consumed = 0
        self._row_index: dict[str, int] = {}
        self._known_owners: set[str] = set()
        self._filter_status = "all"
        self._filter_owner = "all"

        self.setWindowTitle("Puffo Agent")
        self.resize(1200, 720)
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()

    # UI construction ───────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(6)

        root_layout.addLayout(self._build_filter_bar())

        main_splitter = QSplitter(Qt.Vertical)
        main_splitter.addWidget(self._build_table())
        main_splitter.addWidget(self._build_detail())
        main_splitter.addWidget(self._build_log())
        main_splitter.setStretchFactor(0, 4)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setStretchFactor(2, 3)
        main_splitter.setSizes([400, 140, 280])
        root_layout.addWidget(main_splitter, stretch=1)

    def _build_filter_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(QLabel("Status:"))
        self._status_combo = QComboBox()
        self._status_combo.addItem("All", "all")
        for value in ("running", "paused", "error", "stopped", "starting"):
            self._status_combo.addItem(_STATUS_LABEL.get(value, value), value)
        self._status_combo.currentIndexChanged.connect(self._on_status_filter)
        bar.addWidget(self._status_combo)

        bar.addSpacing(16)
        bar.addWidget(QLabel("Owner:"))
        self._owner_combo = QComboBox()
        self._owner_combo.addItem("All", "all")
        self._owner_combo.currentIndexChanged.connect(self._on_owner_filter)
        bar.addWidget(self._owner_combo)

        bar.addStretch(1)
        self._count_label = QLabel("")
        bar.addWidget(self._count_label)
        return bar

    def _build_table(self) -> QWidget:
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.itemSelectionChanged.connect(self._on_select)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        # Every column is user-resizable; defaults below give a
        # reasonable first paint.
        for col in range(len(_COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            self._table.setColumnWidth(col, _DEFAULT_COL_WIDTHS.get(col, 120))
        return self._table

    def _build_detail(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(4, 4, 4, 4)
        self._detail = QLabel("(no agent selected)")
        self._detail.setTextFormat(Qt.RichText)
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._detail, stretch=1)

        actions = QHBoxLayout()
        self._restart_btn = QPushButton("Restart")
        self._pause_btn = QPushButton("Pause")
        self._resume_btn = QPushButton("Resume")
        self._archive_btn = QPushButton("Archive")
        for btn, slot in (
            (self._restart_btn, self._on_restart),
            (self._pause_btn, self._on_pause),
            (self._resume_btn, self._on_resume),
            (self._archive_btn, self._on_archive),
        ):
            btn.clicked.connect(slot)
            actions.addWidget(btn)
        actions.addStretch(1)
        layout.addLayout(actions)
        return wrap

    def _build_log(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(QLabel("Logs:"))
        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 9pt;"
        )
        layout.addWidget(self._log_view, stretch=1)
        return wrap

    # Periodic refresh ──────────────────────────────────────────────

    def _tick(self) -> None:
        rows = [_AgentRow(aid).load() for aid in discover_agents()]
        self._refresh_owner_combo(rows)
        self._refresh_table(rows)
        self._refresh_detail()
        self._refresh_buttons()
        self._refresh_logs()

    def _refresh_owner_combo(self, rows: list[_AgentRow]) -> None:
        owners = {r.owner for r in rows if r.owner}
        if owners == self._known_owners:
            return
        self._known_owners = owners
        current = self._owner_combo.currentData()
        self._owner_combo.blockSignals(True)
        self._owner_combo.clear()
        self._owner_combo.addItem("All", "all")
        for owner in sorted(owners):
            self._owner_combo.addItem(owner, owner)
        idx = self._owner_combo.findData(current)
        if idx >= 0:
            self._owner_combo.setCurrentIndex(idx)
        self._owner_combo.blockSignals(False)

    def _refresh_table(self, rows: list[_AgentRow]) -> None:
        rows.sort(key=lambda r: (r.display_name.lower(), r.id))
        self._table.setUpdatesEnabled(False)
        self._table.setRowCount(len(rows))
        self._row_index = {r.id: i for i, r in enumerate(rows)}
        visible = 0
        for i, row in enumerate(rows):
            self._fill_row(i, row)
            hide = not self._row_passes_filter(row)
            self._table.setRowHidden(i, hide)
            if not hide:
                visible += 1
        if self._selected_id and self._selected_id in self._row_index:
            self._table.selectRow(self._row_index[self._selected_id])
        self._table.setUpdatesEnabled(True)
        self._count_label.setText(f"{visible} / {len(rows)} agents")

    def _fill_row(self, row_idx: int, row: _AgentRow) -> None:
        status_label = _STATUS_LABEL.get(row.status, row.status)
        color = _STATUS_COLOR.get(row.status, QColor("#9aa0a6"))
        cells = [
            row.display_name,
            row.role,
            row.slug or "—",
            row.owner or "—",
            f"● {status_label}",
            row.runtime_kind or "—",
            row.harness or "—",
            row.model or "—",
            row.server or "—",
        ]
        tooltips = {1: row.role, 8: row.server}
        for col, text in enumerate(cells):
            item = self._table.item(row_idx, col)
            if item is None:
                item = QTableWidgetItem()
                self._table.setItem(row_idx, col, item)
            item.setText(text)
            item.setData(Qt.UserRole, row.id)
            if col == 4:
                item.setForeground(color)
            tip = tooltips.get(col)
            if tip:
                item.setToolTip(tip)

    def _row_passes_filter(self, row: _AgentRow) -> bool:
        if self._filter_status != "all" and row.status != self._filter_status:
            return False
        if self._filter_owner != "all" and row.owner != self._filter_owner:
            return False
        return True

    def _refresh_detail(self) -> None:
        if not self._selected_id:
            self._detail.setText("(no agent selected)")
            return
        try:
            cfg = AgentConfig.load(self._selected_id)
        except Exception as exc:
            self._detail.setText(f"<i>failed to load agent.yml: {exc}</i>")
            return
        rt = RuntimeState.load(self._selected_id) or RuntimeState()
        color = _STATUS_COLOR.get(rt.status, QColor("#9aa0a6")).name()
        status_label = _STATUS_LABEL.get(rt.status, rt.status)
        role = cfg.role or "<i>(no role)</i>"
        self._detail.setText(
            f"<b style='font-size:13pt;'>{cfg.display_name or cfg.id or self._selected_id}</b>"
            f" &nbsp; <span style='color:{color};'>● {status_label}</span>"
            f" &nbsp; <span style='color:#888;'>health: {rt.health}</span><br>"
            f"<b>id:</b> {self._selected_id} &nbsp; "
            f"<b>slug:</b> {cfg.puffo_core.slug or '—'} &nbsp; "
            f"<b>owner:</b> {cfg.puffo_core.operator_slug or '—'}<br>"
            f"<b>runtime:</b> {cfg.runtime.kind} · "
            f"{cfg.runtime.harness} · "
            f"{cfg.runtime.model or '(default)'}<br>"
            f"<b>role:</b> {role}"
        )

    def _refresh_logs(self) -> None:
        snapshot = self._log_buffer.snapshot()
        # Snapshot is a sliding window; if it shrank past our cursor
        # we lost lines — reset to the oldest available.
        if self._log_consumed > len(snapshot):
            self._log_consumed = 0
            self._log_view.clear()
        new_lines = snapshot[self._log_consumed:]
        if not new_lines:
            return
        scrollbar = self._log_view.verticalScrollBar()
        # Pin-to-bottom semantics: if the user scrolled up to read
        # something, leave their view alone — only auto-tail when they
        # were already glued to the latest line.
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(("\n" if self._log_view.toPlainText() else "") + "\n".join(new_lines))
        self._log_consumed = len(snapshot)
        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _refresh_buttons(self) -> None:
        has_selection = self._selected_id is not None
        cfg_state = None
        if has_selection:
            try:
                cfg_state = AgentConfig.load(self._selected_id).state
            except Exception:
                pass
        self._restart_btn.setEnabled(has_selection)
        self._archive_btn.setEnabled(has_selection)
        self._pause_btn.setEnabled(has_selection and cfg_state == "running")
        self._resume_btn.setEnabled(has_selection and cfg_state == "paused")

    # Selection + filters ───────────────────────────────────────────

    def _on_select(self) -> None:
        items = self._table.selectedItems()
        if not items:
            self._selected_id = None
        else:
            self._selected_id = items[0].data(Qt.UserRole)
        self._refresh_detail()
        self._refresh_buttons()

    def _on_status_filter(self) -> None:
        self._filter_status = self._status_combo.currentData() or "all"
        self._tick()

    def _on_owner_filter(self) -> None:
        self._filter_owner = self._owner_combo.currentData() or "all"
        self._tick()

    # Actions ───────────────────────────────────────────────────────

    def _on_restart(self) -> None:
        if not self._selected_id:
            return
        self._drop_flag(restart_flag_path(self._selected_id), "restart")

    def _on_archive(self) -> None:
        if not self._selected_id:
            return
        confirm = QMessageBox.question(
            self,
            "Archive agent",
            f"Archive '{self._selected_id}'? The agent dir will be moved to "
            "~/.puffo-agent/archived/ and the worker stopped.",
        )
        if confirm == QMessageBox.Yes:
            self._drop_flag(archive_flag_path(self._selected_id), "archive")

    def _on_pause(self) -> None:
        self._flip_state("paused")

    def _on_resume(self) -> None:
        self._flip_state("running")

    def _drop_flag(self, path, label: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("requested", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, label, f"could not write flag: {exc}")

    def _flip_state(self, target: str) -> None:
        if not self._selected_id:
            return
        try:
            cfg = AgentConfig.load(self._selected_id)
        except Exception as exc:
            QMessageBox.warning(self, target, f"could not load agent.yml: {exc}")
            return
        if cfg.state == target:
            return
        cfg.state = target
        try:
            cfg.save()
        except OSError as exc:
            QMessageBox.warning(self, target, f"could not save agent.yml: {exc}")

    # Shutdown ──────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:
        # Daemon shutdown writes the stop sentinel; the reconcile loop
        # picks it up within ~2s, tears down workers (up to ~30s for
        # docker adapters), then ``os._exit(0)``s the whole process.
        # If we accepted the close immediately the window would
        # disappear while the process kept running — confusing on
        # Windows where the user might try to relaunch and hit the
        # single-daemon lock. Instead we hide the UI behind a
        # shutting-down banner and let the bg thread terminate us.
        if not self._stop_requested:
            self._stop_requested = True
            self._timer.stop()
            wrote_sentinel = self._daemon_thread.request_stop()
            if not wrote_sentinel:
                event.accept()
                return
            self.setWindowTitle("Puffo Agent — shutting down…")
            self._show_shutdown_overlay()
        event.ignore()

    def _show_shutdown_overlay(self) -> None:
        overlay = QLabel(
            "Shutting down agents…\n\nThis can take up to ~30 seconds while\n"
            "workers close their LLM sessions cleanly.",
            self,
        )
        overlay.setAlignment(Qt.AlignCenter)
        overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 200); color: white; "
            "font-size: 13pt; padding: 24px;"
        )
        self.setCentralWidget(overlay)
