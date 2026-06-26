"""Operators page: linked operators + a "Create new link" action.

Lists the operators this machine is paired with (control/pairings.json) and
lets the user mint a new link code against a server URL. Approval still happens
operator-side in the web app; this page shows the code and polls for it.
"""
from __future__ import annotations

import asyncio
import threading
import webbrowser
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...control.link import (
    DEFAULT_SERVER_URL,
    await_link_approval,
    friendly_device_name,
    mint_link_code,
)
from ...control.store import load_pairings


def _machine_name() -> str:
    return friendly_device_name()


class _LinkDialog(QDialog):
    """Mint a link code against a server URL, show it, and poll for approval."""

    _minted = Signal(str, str)        # (code, base)
    _mint_failed = Signal(str)        # error
    _approval = Signal(str, object)   # (status, operator_slug|None)
    _approval_failed = Signal(str)    # error

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Link a new operator")
        self.setMinimumWidth(440)
        self._hostname = _machine_name()
        self._link_url = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        intro = QLabel(
            "Mint a link code, then approve it in the puffo web app "
            "(My Agents → Link machine)."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #6b7280; font-size: 10pt;")
        layout.addWidget(intro)

        url_label = QLabel("Server URL")
        url_label.setStyleSheet("color: #374151; font-weight: 600;")
        layout.addWidget(url_label)
        self._url = QLineEdit(DEFAULT_SERVER_URL)
        self._url.setPlaceholderText(DEFAULT_SERVER_URL)
        layout.addWidget(self._url)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._status.setStyleSheet("color: #374151; font-size: 10pt;")
        layout.addWidget(self._status)

        row = QHBoxLayout()
        row.addStretch(1)
        self._close = QPushButton("Close")
        self._close.clicked.connect(self.reject)
        self._open_browser = QPushButton("Open in browser")
        self._open_browser.setCursor(Qt.PointingHandCursor)
        self._open_browser.clicked.connect(self._on_open_browser)
        self._open_browser.hide()
        self._create = QPushButton("Create link")
        self._create.setDefault(True)
        self._create.clicked.connect(self._on_create)
        row.addWidget(self._close)
        row.addWidget(self._open_browser)
        row.addWidget(self._create)
        layout.addLayout(row)

        self._minted.connect(self._on_minted)
        self._mint_failed.connect(self._on_mint_failed)
        self._approval.connect(self._on_approval)
        self._approval_failed.connect(self._on_approval_failed)

    def _on_create(self) -> None:
        url = self._url.text().strip()
        if not url:
            self._status.setText("Enter a server URL.")
            return
        self._create.setEnabled(False)
        self._url.setEnabled(False)
        self._status.setText("Registering machine + minting code…")

        def worker() -> None:
            try:
                code, base = asyncio.run(mint_link_code(url, self._hostname))
                self._minted.emit(code, base)
            except Exception as exc:  # noqa: BLE001 — surfaced in the dialog
                self._mint_failed.emit(str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_minted(self, code: str, base: str) -> None:
        web = base[: -len("/relay")] if base.endswith("/relay") else base
        self._link_url = f"{web}/link-machine?code={code}"
        self._status.setText(
            f"Link code:  {code}\n\n"
            f"Approve at  {self._link_url}\n"
            "or in My Agents → Link machine.\n\nWaiting for approval…"
        )
        self._open_browser.show()

        def worker() -> None:
            try:
                status, slug = asyncio.run(
                    await_link_approval(base, code, self._hostname)
                )
                self._approval.emit(status, slug)
            except Exception as exc:  # noqa: BLE001
                self._approval_failed.emit(str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_open_browser(self) -> None:
        if self._link_url:
            webbrowser.open(self._link_url)

    def _on_mint_failed(self, error: str) -> None:
        self._status.setText(f"Could not create link: {error}")
        self._create.setEnabled(True)
        self._url.setEnabled(True)

    def _on_approval(self, status: str, slug: object) -> None:
        if status == "approved":
            self._status.setText(f"Linked to operator {slug}.")
            self.accept()
            return
        msg = "Code expired." if status == "expired" else "Timed out waiting for approval."
        self._status.setText(f"{msg} Try again.")
        self._open_browser.hide()  # the code is stale
        self._create.setEnabled(True)
        self._url.setEnabled(True)

    def _on_approval_failed(self, error: str) -> None:
        self._status.setText(f"Approval failed: {error}")
        self._open_browser.hide()
        self._create.setEnabled(True)
        self._url.setEnabled(True)


class OperatorsView(QWidget):
    """Lists linked operators + a button to mint a new link code."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._render_key: Optional[tuple] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Operators")
        title.setStyleSheet("font-size: 14pt; font-weight: 600; color: #1f2937;")
        header.addWidget(title)
        header.addStretch(1)
        new_btn = QPushButton("+ Create new link")
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.clicked.connect(self._open_link_dialog)
        header.addWidget(new_btn)
        outer.addLayout(header)

        subtitle = QLabel(
            "Operators paired with this machine. Each can manage its own agents "
            "remotely over the control connection."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #6b7280; font-size: 10pt;")
        outer.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self._list_host = QWidget()
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 4, 0, 0)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch(1)
        scroll.setWidget(self._list_host)
        outer.addWidget(scroll, stretch=1)

        self.poll()

    def poll(self) -> None:
        try:
            pairings = load_pairings()
        except Exception:  # noqa: BLE001 — a corrupt file shouldn't crash the UI
            pairings = {}
        key = tuple((p.operator_slug, p.server_url, p.name) for p in pairings.values())
        if key == self._render_key:
            return
        self._render_key = key
        self._rebuild(list(pairings.values()))

    def _rebuild(self, pairings: list) -> None:
        # Drop existing rows, keep the trailing stretch (last item).
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if not pairings:
            empty = QLabel('No operators linked yet. Click "Create new link".')
            empty.setStyleSheet("color: #9ca3af; font-size: 11pt; padding: 12px 2px;")
            self._list_layout.insertWidget(0, empty)
            return
        for i, p in enumerate(sorted(pairings, key=lambda x: x.name.lower())):
            self._list_layout.insertWidget(i, self._make_card(p))

    def _make_card(self, p) -> QWidget:
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background-color: #ffffff; border: 1px solid #e5e7eb;"
            "         border-radius: 10px; }"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        name = QLabel(p.name or p.operator_slug)
        name.setStyleSheet("font-size: 11pt; font-weight: 600; color: #111827; border: 0;")
        lay.addWidget(name)
        slug = QLabel(p.operator_slug)
        slug.setTextInteractionFlags(Qt.TextSelectableByMouse)
        slug.setStyleSheet("font-family: monospace; color: #6b7280; font-size: 9pt; border: 0;")
        lay.addWidget(slug)
        url = QLabel(p.server_url)
        url.setStyleSheet("color: #9ca3af; font-size: 9pt; border: 0;")
        lay.addWidget(url)
        return card

    def _open_link_dialog(self) -> None:
        dlg = _LinkDialog(self)
        dlg.exec()
        self.poll()
