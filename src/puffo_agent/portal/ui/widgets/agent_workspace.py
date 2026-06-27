"""Right-pane right half: Messages / Logs / Files tabs for an agent."""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QDir, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileSystemModel,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ...state import AgentConfig
from ..names import channel_id_to_name, slug_to_display_name, space_id_to_name
from .log_view import LogView
from .per_agent_log_source import PerAgentLogSource


class AgentWorkspace(QWidget):
    """Tabbed pane bound to a single agent id."""

    def __init__(
        self,
        snapshot_fn: Callable[[], list[str]],
        counter_fn: Callable[[], int],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._agent_id: Optional[str] = None
        self._cfg: Optional[AgentConfig] = None
        self._snapshot_fn = snapshot_fn
        self._counter_fn = counter_fn
        self._build()

    # Construction ──────────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._tabs = QTabWidget()
        self._logs_tab = self._build_logs_tab()
        self._files_tab = self._build_files_tab()
        self._channels_tab = self._build_channels_tab()
        self._tabs.addTab(self._channels_tab, "Messages")
        self._tabs.addTab(self._logs_tab, "Logs")
        self._tabs.addTab(self._files_tab, "Files")
        outer.addWidget(self._tabs)

    def _build_logs_tab(self) -> QWidget:
        # Start empty; bind() swaps in a PerAgentLogSource once an
        # agent id is known so the tab can merge daemon-side Python
        # log lines with the agent's audit.log NDJSON entries.
        self._agent_log = LogView(lambda: [], lambda: 0)
        return self._agent_log

    def _build_files_tab(self) -> QWidget:
        self._files_model = QFileSystemModel()
        self._files_model.setReadOnly(True)
        # Hidden flag lets .claude/.codex (operator-meaningful) appear.
        self._files_model.setFilter(
            QDir.AllEntries | QDir.NoDotAndDotDot | QDir.Hidden
        )

        self._files_tree = QTreeView()
        self._files_tree.setModel(self._files_model)
        self._files_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._files_tree.setSortingEnabled(True)
        for col in (2, 3):  # type / date columns — hide for compactness
            self._files_tree.hideColumn(col)
        self._files_tree.setColumnWidth(0, 280)
        return self._files_tree

    def _build_channels_tab(self) -> QWidget:
        wrap = QSplitter(Qt.Horizontal)
        self._channel_list = QListWidget()
        self._channel_list.itemSelectionChanged.connect(self._on_channel_selected)
        wrap.addWidget(self._channel_list)
        self._channel_messages = QTextEdit()
        self._channel_messages.setReadOnly(True)
        self._channel_messages.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 9pt;"
        )
        wrap.addWidget(self._channel_messages)
        wrap.setSizes([260, 600])
        return wrap

    # Public ────────────────────────────────────────────────────────

    def bind(self, agent_id: Optional[str]) -> None:
        self._agent_id = agent_id
        if agent_id is None:
            self._cfg = None
            self._agent_log.set_sources(lambda: [], lambda: 0)
            self._files_tree.setRootIndex(self._files_model.index(""))
            self._channel_list.clear()
            self._channel_messages.clear()
            return
        try:
            self._cfg = AgentConfig.load(agent_id)
        except Exception:
            self._cfg = None

        if self._cfg:
            from ...state import agent_dir
            root = str(agent_dir(self._agent_id))
            self._files_model.setRootPath(root)
            self._files_tree.setRootIndex(self._files_model.index(root))
            audit_path = self._cfg.resolve_workspace_dir() / ".puffo-agent" / "audit.log"
        else:
            audit_path = Path("/nonexistent")

        source = PerAgentLogSource(audit_path=audit_path)
        self._agent_log.set_sources(source.snapshot, source.counter)

        self._reload_channels()

    def poll(self) -> None:
        if self._tabs.currentWidget() is self._agent_log:
            self._agent_log.poll()

    # Channels tab ──────────────────────────────────────────────────

    def _db_path(self) -> Optional[Path]:
        if not self._agent_id:
            return None
        from ...state import agent_dir
        path = agent_dir(self._agent_id) / "messages.db"
        return path if path.exists() else None

    def _reload_channels(self) -> None:
        self._channel_list.clear()
        self._channel_messages.clear()
        db = self._db_path()
        if db is None:
            self._channel_list.addItem(QListWidgetItem("(no messages.db yet)"))
            return
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT envelope_kind, channel_id, space_id, sender_slug,"
                    "       recipient_slug, MAX(sent_at) AS last_at,"
                    "       COUNT(*) AS n"
                    " FROM messages"
                    " GROUP BY envelope_kind,"
                    "          COALESCE(channel_id, sender_slug || '|' || recipient_slug)"
                    " ORDER BY last_at DESC NULLS LAST"
                ).fetchall()
        except sqlite3.Error as exc:
            self._channel_list.addItem(QListWidgetItem(f"(db error: {exc})"))
            return
        if not rows:
            self._channel_list.addItem(QListWidgetItem("(no messages yet)"))
            return
        own_slug = self._cfg.puffo_core.slug if self._cfg else ""
        names = slug_to_display_name()
        spaces = space_id_to_name()
        channels = channel_id_to_name()
        for row in rows:
            label, key = _channel_label(row, own_slug, names, spaces, channels)
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            self._channel_list.addItem(item)

    def _on_channel_selected(self) -> None:
        items = self._channel_list.selectedItems()
        if not items:
            self._channel_messages.clear()
            return
        key = items[0].data(Qt.UserRole)
        if not isinstance(key, tuple):
            self._channel_messages.clear()
            return
        kind, identifier = key
        db = self._db_path()
        if db is None:
            return
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                if kind == "channel":
                    rows = conn.execute(
                        "SELECT sender_slug, content_type, content, sent_at"
                        " FROM messages"
                        " WHERE envelope_kind='channel' AND channel_id=?"
                        " ORDER BY sent_at DESC LIMIT 200",
                        (identifier,),
                    ).fetchall()
                else:
                    peer = identifier
                    rows = conn.execute(
                        "SELECT sender_slug, content_type, content, sent_at"
                        " FROM messages"
                        " WHERE envelope_kind='dm'"
                        "   AND (sender_slug=? OR recipient_slug=?)"
                        " ORDER BY sent_at DESC LIMIT 200",
                        (peer, peer),
                    ).fetchall()
        except sqlite3.Error as exc:
            self._channel_messages.setPlainText(f"(db error: {exc})")
            return
        names = slug_to_display_name()
        bubbles = "".join(_render_bubble(r, names) for r in reversed(rows))
        self._channel_messages.setHtml(_MESSAGE_STYLE + bubbles)
        sb = self._channel_messages.verticalScrollBar()
        sb.setValue(sb.maximum())


# Helpers ───────────────────────────────────────────────────────────


def _channel_label(
    row: sqlite3.Row,
    own_slug: str,
    names: dict[str, str],
    spaces: dict[str, str],
    channels: dict[str, str],
) -> tuple[str, tuple[str, str]]:
    kind = row["envelope_kind"]
    last_at = row["last_at"]
    ts = _format_ts(last_at) if last_at else ""
    if kind == "channel":
        cid = row["channel_id"] or "(unnamed)"
        sid = row["space_id"] or ""
        channel_name = channels.get(cid, cid)
        space_label = spaces.get(sid, sid) if sid else ""
        suffix = f"  ({space_label})" if space_label else ""
        return (
            f"# {channel_name}{suffix}  · {row['n']}  {ts}",
            ("channel", cid),
        )
    peer_slug = row["sender_slug"] if row["sender_slug"] != own_slug else row["recipient_slug"]
    peer_slug = peer_slug or "(unknown)"
    peer_label = names.get(peer_slug, peer_slug)
    return (f"@ {peer_label}  · {row['n']}  {ts}", ("dm", peer_slug))


_MESSAGE_STYLE = """
<style>
  .msg { margin: 6px 0; padding: 6px 10px; border-left: 3px solid #e5e7eb; }
  .msg-meta { font-size: 8.5pt; color: #9ca3af; margin-bottom: 2px; }
  .msg-sender { font-weight: 700; color: #1f2937; }
  .msg-body { color: #1f2937; white-space: pre-wrap; }
</style>
"""


def _render_bubble(row: sqlite3.Row, names: dict[str, str]) -> str:
    ts = html.escape(_format_ts(row["sent_at"]))
    sender_slug = row["sender_slug"] or "?"
    sender = html.escape(names.get(sender_slug, sender_slug))
    content = row["content"]
    ctype = row["content_type"]
    if ctype and ctype.startswith("application/json"):
        try:
            parsed = json.loads(content)
            content = parsed.get("text") or parsed.get("body") or json.dumps(parsed, indent=2)
        except Exception:
            pass
    body = html.escape(str(content))
    return (
        f"<div class='msg'>"
        f"<div class='msg-meta'>{ts}</div>"
        f"<div><span class='msg-sender'>{sender}</span>: "
        f"<span class='msg-body'>{body}</span></div>"
        f"</div>"
    )


def _format_ts(ms_epoch: Optional[int]) -> str:
    if not ms_epoch:
        return ""
    try:
        return datetime.fromtimestamp(ms_epoch / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return str(ms_epoch)
