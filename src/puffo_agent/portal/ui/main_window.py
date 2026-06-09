"""Main window: rail + (Home view | Agents section)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .daemon_thread import DaemonThread
from .log_buffer import LogRingHandler
from .widgets.agent_detail import AgentDetail
from .widgets.agent_list import AgentList
from .widgets.agent_workspace import AgentWorkspace
from .widgets.avatar import AvatarCache
from .widgets.home_view import HomeView
from .widgets.log_view import LogView
from .widgets.mcp_status import McpStatusView
from .widgets.rail import Rail


class _NullLogView:
    """No-op stand-in so the timer can call poll() unconditionally."""

    def poll(self) -> None:
        return None


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        daemon_thread: DaemonThread,
        log_buffer: LogRingHandler,
        detached: bool = False,
    ) -> None:
        super().__init__()
        self._daemon_thread = daemon_thread
        self._log_buffer = log_buffer
        # Detached = opened from the tray (start --background); closing the
        # window must NOT stop the daemon (only the tray's Quit does).
        # --ui (non-detached) keeps the close-stops-daemon behaviour.
        self._detached = detached
        self._stop_requested = False
        self._selected_id: Optional[str] = None
        self._section = "home"

        self._avatar_cache = AvatarCache(self)

        self.setWindowTitle("Puffo Agent")
        self.resize(1320, 820)
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()

        # Watchdog: the daemon thread exits either from our own
        # closeEvent (operator clicked X) or from an external
        # ``puffo-agent stop`` writing the sentinel. Either way the
        # Qt event loop has no other reason to quit — without this
        # the process lingers and ``puffo-agent stop`` times out
        # waiting for the PID to go away.
        self._daemon_watchdog = QTimer(self)
        self._daemon_watchdog.setInterval(500)
        self._daemon_watchdog.timeout.connect(self._check_daemon_alive)
        self._daemon_watchdog.start()

    # UI construction ───────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._rail = Rail()
        self._rail.section_changed.connect(self._on_section_changed)
        root_layout.addWidget(self._rail)

        self._sections = QStackedWidget()
        self._sections.addWidget(self._build_home_section())     # 0
        self._sections.addWidget(self._build_agents_section())   # 1
        self._sections.addWidget(self._build_logs_section())     # 2
        self._sections.addWidget(self._build_status_section())   # 3
        root_layout.addWidget(self._sections, stretch=1)

    def _build_home_section(self) -> QWidget:
        self._home = HomeView()
        return self._home

    def _build_logs_section(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        title = QLabel("System log")
        title.setStyleSheet("font-size: 14pt; font-weight: 600; color: #1f2937;")
        layout.addWidget(title)
        self._system_log = LogView(
            self._log_buffer.snapshot, self._log_buffer.counter,
        )
        layout.addWidget(self._system_log, stretch=1)
        return wrap

    def _build_status_section(self) -> QWidget:
        self._mcp_status = McpStatusView()
        return self._mcp_status

    def _build_agents_section(self) -> QWidget:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        self._agent_list = AgentList(avatar_cache=self._avatar_cache)
        self._agent_list.agent_selected.connect(self._on_agent_selected)
        splitter.addWidget(self._agent_list)

        self._agent_right = QStackedWidget()
        self._agent_right.addWidget(self._build_runtime_logs_pane())   # 0
        self._agent_right.addWidget(self._build_agent_detail_pane())   # 1
        splitter.addWidget(self._agent_right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 960])
        return splitter

    def _build_runtime_logs_pane(self) -> QWidget:
        # Runtime logs live on Home; right side stays empty until selection.
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)
        hint = QLabel("Select an agent from the list")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color: #9ca3af; font-size: 12pt;")
        layout.addWidget(hint)
        layout.addStretch(1)
        self._runtime_log = _NullLogView()
        return wrap

    def _build_agent_detail_pane(self) -> QWidget:
        wrap = QSplitter(Qt.Horizontal)
        wrap.setChildrenCollapsible(False)
        self._detail = AgentDetail()
        self._detail.saved.connect(self._on_detail_saved)
        self._workspace = AgentWorkspace(
            self._log_buffer.snapshot, self._log_buffer.counter,
        )
        wrap.addWidget(self._detail)
        wrap.addWidget(self._workspace)
        wrap.setSizes([460, 540])
        return wrap

    # Navigation ────────────────────────────────────────────────────

    def _on_section_changed(self, section: str) -> None:
        self._section = section
        self._sections.setCurrentIndex(
            {"home": 0, "agents": 1, "logs": 2, "status": 3}.get(section, 0)
        )

    def _on_agent_selected(self, agent_id: Optional[str]) -> None:
        self._selected_id = agent_id
        if agent_id is None:
            self._agent_right.setCurrentIndex(0)
            return
        self._agent_right.setCurrentIndex(1)
        self._detail.bind(agent_id)
        self._workspace.bind(agent_id)

    def _on_detail_saved(self, _agent_id: str) -> None:
        # Re-render the sidebar without waiting for the next 500 ms tick.
        self._agent_list.refresh()

    # Periodic refresh ──────────────────────────────────────────────

    def _tick(self) -> None:
        if self._section == "home":
            self._home.poll()
            return
        if self._section == "logs":
            self._system_log.poll()
            return
        if self._section == "status":
            self._mcp_status.poll()
            return
        self._agent_list.refresh()
        if self._selected_id is None:
            self._runtime_log.poll()
        else:
            self._workspace.poll()

    # Shutdown ──────────────────────────────────────────────────────

    def _check_daemon_alive(self) -> None:
        """If the daemon thread has exited (operator close, external
        ``puffo-agent stop``, or an unhandled crash inside the thread),
        tear down the Qt event loop so the OS process actually exits.
        """
        if self._daemon_thread.is_alive():
            return
        self._daemon_watchdog.stop()
        if self._detached:
            # Tray owns the app lifecycle; just close this viewer window.
            self.close()
            return
        QApplication.instance().quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._detached:
            # Tray-owned viewer: closing must NOT stop the daemon — only
            # the tray's Quit does. Just tear down this window's timers.
            self._timer.stop()
            self._daemon_watchdog.stop()
            event.accept()
            return
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
