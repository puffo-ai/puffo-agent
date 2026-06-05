"""Home view: AI tool cards + agent count + pairing status + version footer."""
from __future__ import annotations

import importlib.metadata
from typing import Callable, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
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
from ..assets import logo_path
from ..names import resolve_display_name


class _LogoLabel(QLabel):
    """Click-through label that opens puffo.ai."""

    def __init__(self, size: int = 40, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        pm = QPixmap(str(logo_path()))
        if not pm.isNull():
            self.setPixmap(pm.scaled(
                size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation,
            ))
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Open puffo.ai")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            QDesktopServices.openUrl(QUrl("https://puffo.ai"))
        super().mousePressEvent(event)


_CARD_STYLE = (
    "QFrame#card { background-color: #ffffff; border: 1px solid #e5e7eb; "
    "              border-radius: 10px; }"
)


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    frame.setStyleSheet(_CARD_STYLE)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(8)
    return frame, layout


class _CliCard(QFrame):
    """One card per CLI tool; the path hides behind a small triangle."""

    def __init__(
        self,
        label: str,
        resolver: Callable[[], Optional[str]],
        cred_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        super().__init__()
        self.setObjectName("card")
        self._resolver = resolver
        self._cred_check = cred_check
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

    def mark_coming_soon(self) -> None:
        self._coming_soon = True

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
        if not path:
            self._dot.setStyleSheet("font-size: 14pt; color: #ef4444;")
            self._status_label.setText("not installed")
            self._status_label.setStyleSheet("color: #9ca3af; font-size: 9pt;")
            self._path_label.setText("(not on PATH and no env override)")
            return
        # Installed — discriminate logged-in vs needs-login when a
        # cred_check is supplied (cards without one are treated as ready).
        has_cred = True
        if self._cred_check is not None:
            try:
                has_cred = self._cred_check()
            except Exception:
                has_cred = False
        if has_cred:
            self._dot.setStyleSheet("font-size: 14pt; color: #22c55e;")
            self._status_label.setText("ready")
            self._status_label.setStyleSheet(
                "color: #16a34a; font-size: 9pt; font-weight: 500;"
            )
        else:
            self._dot.setStyleSheet("font-size: 14pt; color: #f59e0b;")
            self._status_label.setText("need log in")
            self._status_label.setStyleSheet(
                "color: #d97706; font-size: 9pt; font-weight: 500;"
            )
        self._path_label.setText(path)

    def _on_toggled(self, checked: bool) -> None:
        self._toggle.setText("▾" if checked else "▸")
        self._path_label.setVisible(checked)


class HomeView(QWidget):
    """Daemon overview pane."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 12)
        outer.setSpacing(14)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        title_row.addWidget(_LogoLabel(size=44))
        title = QLabel("Puffo Agent")
        title.setStyleSheet("font-size: 22pt; font-weight: 700; color: #111827;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        open_btn = QPushButton("Open Puffo")
        open_btn.setToolTip("Launch chat.puffo.ai/chat in your default browser.")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.setStyleSheet(
            "QPushButton { background-color: #3b82f6; color: white;"
            "              border: 1px solid #2563eb; border-radius: 6px;"
            "              padding: 6px 18px; font-weight: 500; }"
            "QPushButton:hover { background-color: #2563eb; }"
        )
        open_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://chat.puffo.ai/chat"))
        )
        title_row.addWidget(open_btn)
        outer.addLayout(title_row)

        # Bridge / pairing card
        bridge_card, bridge_layout = _card()
        bridge_title = QLabel("Local bridge")
        bridge_title.setStyleSheet(
            "color: #6b7280; font-size: 10pt; text-transform: uppercase;"
        )
        bridge_layout.addWidget(bridge_title)
        self._bridge_status = QLabel("…")
        self._bridge_status.setStyleSheet(
            "font-size: 13pt; font-weight: 500; color: #1f2937;"
        )
        self._bridge_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bridge_layout.addWidget(self._bridge_status)
        outer.addWidget(bridge_card)

        # AI tool cards
        from ....agent.cli_bin import (
            claude_has_credentials,
            codex_has_credentials,
            resolve_claude_bin,
            resolve_codex_bin,
        )
        cli_specs: list[tuple[str, Callable[[], Optional[str]], Optional[Callable[[], bool]]]] = [
            ("Claude Code", resolve_claude_bin, claude_has_credentials),
            ("Codex",       resolve_codex_bin,  codex_has_credentials),
        ]
        cli_grid = QHBoxLayout()
        cli_grid.setSpacing(12)
        self._cli_cards: list[_CliCard] = []
        for label, resolver, cred_check in cli_specs:
            card = _CliCard(label, resolver, cred_check)
            self._cli_cards.append(card)
            cli_grid.addWidget(card, stretch=1)
        hermes_card = _CliCard("Hermes", lambda: None)
        hermes_card.mark_coming_soon()
        self._cli_cards.append(hermes_card)
        cli_grid.addWidget(hermes_card, stretch=1)
        outer.addLayout(cli_grid)

        # Agent count card
        count_card, count_layout = _card()
        count_title = QLabel("Agents")
        count_title.setStyleSheet(
            "color: #6b7280; font-size: 10pt; text-transform: uppercase;"
        )
        count_layout.addWidget(count_title)
        self._count_label = QLabel("…")
        self._count_label.setStyleSheet(
            "font-size: 20pt; font-weight: 600; color: #1f2937;"
        )
        count_layout.addWidget(self._count_label)
        self._count_detail = QLabel("")
        self._count_detail.setStyleSheet("color: #6b7280;")
        count_layout.addWidget(self._count_detail)
        outer.addWidget(count_card)

        outer.addStretch(1)

        # Footer
        footer = QLabel(self._footer_text())
        footer.setAlignment(Qt.AlignCenter)
        footer.setStyleSheet("color: #9ca3af; font-size: 9pt; padding: 4px;")
        outer.addWidget(footer)

    def poll(self) -> None:
        for card in self._cli_cards:
            card.refresh()
        self._refresh_counts()
        self._refresh_bridge()

    @staticmethod
    def _footer_text() -> str:
        try:
            version = importlib.metadata.version("puffo-agent")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        return f"puffo-agent v{version}"

    def _refresh_bridge(self) -> None:
        from ...api.pairing import load_pairing
        pairing = load_pairing()
        if pairing is None:
            self._bridge_status.setText(
                "<span style='color:#9ca3af;'>● Not paired</span> &nbsp; "
                "<a href='https://chat.puffo.ai/chat/agents' "
                "style='color:#3b82f6; text-decoration:none;'>"
                "Pair at chat.puffo.ai/chat/agents →</a>"
            )
            self._bridge_status.setOpenExternalLinks(True)
            return
        name = resolve_display_name(pairing.slug) or pairing.slug
        device = pairing.device_id
        if len(device) > 28:
            device = device[:24] + "…"
        self._bridge_status.setText(
            f"<span style='color:#22c55e;'>● Paired</span> &nbsp; "
            f"<b>{name}</b> &nbsp; "
            f"<span style='color:#6b7280; font-size:9pt;'>device: {device}</span>"
        )

    def _refresh_counts(self) -> None:
        ids = discover_agents()
        running = 0
        for aid in ids:
            rt = RuntimeState.load(aid)
            if rt and rt.status == "running":
                running += 1
        self._count_label.setText(f"{running} / {len(ids)} running")
        self._count_detail.setText(f"{len(ids)} agents discovered on this device")
