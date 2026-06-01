"""Home view: daemon-wide summary.

Sections: a card per detected CLI tool (collapsible path), agent
count card, system log card.
"""
from __future__ import annotations

import shutil
from typing import Callable, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...state import RuntimeState, discover_agents
from .log_view import LogView


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(8)
    return frame, layout


class _CliCard(QFrame):
    """One card per CLI tool. The path is hidden behind a triangle
    toggle; status dot + tool name stay always visible."""

    def __init__(self, label: str, resolver: Callable[[], Optional[str]]) -> None:
        super().__init__()
        self.setObjectName("card")
        self._resolver = resolver
        self._path: Optional[str] = None
        self._coming_soon = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(10)

        self._dot = QLabel("●")
        self._dot.setStyleSheet("font-size: 14pt; color: #9ca3af;")
        header.addWidget(self._dot)

        self._title = QLabel(label)
        self._title.setStyleSheet(
            "font-size: 13pt; font-weight: 600; color: #1f2937;"
        )
        header.addWidget(self._title)
        header.addStretch(1)

        self._status_label = QLabel("not found")
        self._status_label.setStyleSheet("color: #9ca3af; font-size: 9pt;")
        header.addWidget(self._status_label)

        self._toggle = QToolButton()
        self._toggle.setText("▸")
        self._toggle.setStyleSheet(
            "QToolButton { border: none; color: #9ca3af; padding: 0; "
            "              font-size: 9pt; min-width: 12px; }"
            "QToolButton:hover { color: #1f2937; }"
        )
        self._toggle.setCheckable(True)
        self._toggle.setCursor(Qt.PointingHandCursor)
        self._toggle.toggled.connect(self._on_toggled)
        header.addWidget(self._toggle)

        layout.addLayout(header)

        self._path_label = QLabel("")
        self._path_label.setStyleSheet(
            "color: #6b7280; font-family: Consolas, 'Courier New', monospace; "
            "font-size: 9pt; padding-top: 4px; border-top: 1px solid #f3f4f6;"
        )
        self._path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._path_label.setWordWrap(True)
        self._path_label.setVisible(False)
        layout.addWidget(self._path_label)

    def refresh(self) -> None:
        if self._coming_soon:
            self._dot.setStyleSheet("font-size: 14pt; color: #d8b834;")
            self._status_label.setText("coming soon")
            self._status_label.setStyleSheet(
                "color: #9ca3af; font-size: 9pt; font-style: italic;"
            )
            self._path_label.setText("Hermes integration ships in a future release.")
            self._toggle.setEnabled(False)
            self._toggle.setVisible(False)
            return
        try:
            path = self._resolver()
        except Exception:
            path = None
        self._path = path
        if path:
            self._dot.setStyleSheet("font-size: 14pt; color: #22c55e;")
            self._status_label.setText("installed")
            self._status_label.setStyleSheet(
                "color: #16a34a; font-size: 9pt; font-weight: 500;"
            )
            self._path_label.setText(path)
            self._toggle.setEnabled(True)
        else:
            self._dot.setStyleSheet("font-size: 14pt; color: #ef4444;")
            self._status_label.setText("not found")
            self._status_label.setStyleSheet(
                "color: #9ca3af; font-size: 9pt;"
            )
            self._path_label.setText("(not on PATH and no env override)")
            self._toggle.setEnabled(True)

    def mark_coming_soon(self) -> None:
        self._coming_soon = True

    def _on_toggled(self, checked: bool) -> None:
        self._toggle.setText("▾" if checked else "▸")
        self._path_label.setVisible(checked)


class HomeView(QWidget):
    """Daemon overview pane."""

    def __init__(
        self,
        snapshot_fn: Callable[[], list[str]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._build(snapshot_fn)

    def _build(self, snapshot_fn: Callable[[], list[str]]) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        title_row = QHBoxLayout()
        title = QLabel("Puffo Agent")
        title.setStyleSheet("font-size: 22pt; font-weight: 700; color: #111827;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        open_btn = QPushButton("Open Puffo")
        open_btn.setToolTip("Launch chat.puffo.ai in your default browser.")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.setStyleSheet(
            "QPushButton { background-color: #3b82f6; color: white;"
            "              border: 1px solid #2563eb; border-radius: 6px;"
            "              padding: 6px 18px; font-weight: 500; }"
            "QPushButton:hover { background-color: #2563eb; }"
        )
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://chat.puffo.ai")))
        title_row.addWidget(open_btn)
        outer.addLayout(title_row)

        # CLI cards
        from ....agent.cli_bin import resolve_claude_bin, resolve_codex_bin
        cli_specs: list[tuple[str, Callable[[], Optional[str]]]] = [
            ("Claude Code", resolve_claude_bin),
            ("Codex",       resolve_codex_bin),
        ]
        cli_grid = QHBoxLayout()
        cli_grid.setSpacing(12)
        self._cli_cards: list[_CliCard] = []
        for label, resolver in cli_specs:
            card = _CliCard(label, resolver)
            self._cli_cards.append(card)
            cli_grid.addWidget(card, stretch=1)
        # Hermes integration ships later; the placeholder card avoids
        # the false "not installed" red dot.
        hermes_card = _CliCard("Hermes", lambda: None)
        hermes_card.mark_coming_soon()
        self._cli_cards.append(hermes_card)
        cli_grid.addWidget(hermes_card, stretch=1)
        outer.addLayout(cli_grid)

        # Agent count card
        count_card, count_layout = _card()
        count_title = QLabel("Agents")
        count_title.setStyleSheet("color: #6b7280; font-size: 10pt; text-transform: uppercase;")
        count_layout.addWidget(count_title)
        self._count_label = QLabel("…")
        self._count_label.setStyleSheet("font-size: 20pt; font-weight: 600; color: #1f2937;")
        count_layout.addWidget(self._count_label)
        self._count_detail = QLabel("")
        self._count_detail.setStyleSheet("color: #6b7280;")
        count_layout.addWidget(self._count_detail)
        outer.addWidget(count_card)

        # System log card
        log_card, log_layout = _card()
        log_title = QLabel("System log")
        log_title.setStyleSheet("color: #6b7280; font-size: 10pt; text-transform: uppercase;")
        log_layout.addWidget(log_title)
        self._log = LogView(snapshot_fn)
        log_layout.addWidget(self._log)
        outer.addWidget(log_card, stretch=1)

    def poll(self) -> None:
        for card in self._cli_cards:
            card.refresh()
        self._refresh_counts()
        self._log.poll()

    def _refresh_counts(self) -> None:
        ids = discover_agents()
        running = 0
        for aid in ids:
            rt = RuntimeState.load(aid)
            if rt and rt.status == "running":
                running += 1
        self._count_label.setText(f"{running} / {len(ids)} running")
        self._count_detail.setText(f"{len(ids)} agents discovered on this device")
