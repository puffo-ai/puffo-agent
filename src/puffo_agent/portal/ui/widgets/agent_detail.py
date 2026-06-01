"""Right-pane left half: agent Info / Skills / MCP tabs."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ... import export
from ...api.handlers import (
    MAX_PROFILE_SUMMARY_BYTES,
    MAX_ROLE_LEN,
    MAX_ROLE_SHORT_LEN,
    _profile_summary,
    _update_profile_summary,
)
from ...runtime_matrix import (
    HARNESS_PROVIDERS,
    harness_applies,
    validate_triple,
)
from ...state import (
    AgentConfig,
    cli_session_json_path,
    restart_flag_path,
)
from ..names import resolve_display_name


def _provider_for_harness(harness: str) -> str:
    """Each cli-* harness pins to exactly one provider."""
    providers = HARNESS_PROVIDERS.get(harness)
    if providers and len(providers) == 1:
        return next(iter(providers))
    return ""


# (host-dirname, mcp filename or None) per harness. ``None`` means the
# harness has no skill / mcp convention this UI knows how to surface.
_HARNESS_SKILL_DIRNAME = {
    "claude-code": ".claude",
    "codex":       ".codex",
    "gemini-cli":  ".gemini",
}


def _scan_mcp_servers(
    agent_root: Path, home: Path, harness: str,
) -> list[tuple[str, str, dict]]:
    """``[(scope, name, config_dict), ...]`` for the agent's own harness."""
    import json as _json
    import tomllib as _tomllib

    out: list[tuple[str, str, dict]] = []
    if harness == "claude-code":
        json_sources = [
            ("agent",           agent_root / ".claude.json"),
            ("host",            home / ".claude.json"),
            ("agent workspace", agent_root / "workspace" / ".mcp.json"),
        ]
        for scope, path in json_sources:
            if not path.is_file():
                continue
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            servers = data.get("mcpServers") or {}
            for name in sorted(servers.keys()):
                out.append((scope, name, servers[name] or {}))
    elif harness == "codex":
        toml_sources = [
            ("agent", agent_root / ".codex" / "config.toml"),
            ("host",  home / ".codex" / "config.toml"),
        ]
        for scope, path in toml_sources:
            if not path.is_file():
                continue
            try:
                data = _tomllib.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            servers = data.get("mcp_servers") or {}
            for name in sorted(servers.keys()):
                out.append((scope, name, servers[name] or {}))
    return out


_DEFAULT_MODELS = {
    "claude-code": ["", "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "hermes":      ["", "claude-opus-4-7", "claude-sonnet-4-6", "gpt-5.5", "gpt-5.4"],
    "gemini-cli":  ["", "gemini-2.5-pro", "gemini-2.5-flash"],
    "codex":       ["", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"],
}


class AgentDetail(QWidget):
    """Tabbed info pane bound to a single agent id."""

    saved = Signal(str)  # agent_id of the row that just persisted

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._agent_id: Optional[str] = None
        self._cfg: Optional[AgentConfig] = None
        self._build()

    # Construction ──────────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_action_bar())
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_info_tab(), "Info")
        self._tabs.addTab(self._build_skills_tab(), "Skills")
        self._tabs.addTab(self._build_mcp_tab(), "MCP")
        outer.addWidget(self._tabs)

    def _build_action_bar(self) -> QWidget:
        frame = QFrame()
        frame.setStyleSheet("QFrame { background-color: #f3f4f6; border-bottom: 1px solid #e5e7eb; }")
        bar = QHBoxLayout(frame)
        bar.setContentsMargins(8, 6, 8, 6)
        bar.setSpacing(6)
        self._pause_resume_btn = QPushButton("Pause")
        self._pause_resume_btn.clicked.connect(self._on_pause_resume)
        bar.addWidget(self._pause_resume_btn)
        self._refresh_btn = QPushButton("Refresh session")
        self._refresh_btn.setToolTip(
            "Drop cli_session.json + restart the worker for a fresh LLM context."
        )
        self._refresh_btn.clicked.connect(self._on_refresh_session)
        bar.addWidget(self._refresh_btn)
        self._export_btn = QPushButton("Export")
        self._export_btn.setToolTip("Encrypted export bundle. Agent must be paused first.")
        self._export_btn.clicked.connect(self._on_export)
        bar.addWidget(self._export_btn)
        self._archive_btn = QPushButton("Archive")
        self._archive_btn.setToolTip(
            "Move the agent dir to ~/.puffo-agent/archived/ and stop the worker."
        )
        self._archive_btn.clicked.connect(self._on_archive)
        bar.addWidget(self._archive_btn)
        bar.addStretch(1)
        return frame

    def _build_info_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QFormLayout(body)
        layout.setLabelAlignment(Qt.AlignRight)

        self._slug = QLineEdit()
        self._slug.setReadOnly(True)
        self._slug.setStyleSheet("color: #6b7280; background: #f7f7f7;")
        self._slug.setToolTip("Server-managed; change by re-pairing the agent.")
        layout.addRow("Slug", self._slug)

        self._owner_slug = QLineEdit()
        self._owner_slug.setReadOnly(True)
        self._owner_slug.setStyleSheet("color: #6b7280; background: #f7f7f7;")
        self._owner_slug.setToolTip("Server-managed; carried by the identity cert.")
        layout.addRow("Owner", self._owner_slug)

        self._display_name = QLineEdit()
        layout.addRow("Display name", self._display_name)

        self._role_short = QLineEdit()
        self._role_short.setMaxLength(MAX_ROLE_SHORT_LEN)
        self._role_short.setPlaceholderText(f"≤{MAX_ROLE_SHORT_LEN} chars chip label")
        layout.addRow("Role (short)", self._role_short)

        self._role = QLineEdit()
        self._role.setMaxLength(MAX_ROLE_LEN)
        self._role.setPlaceholderText(f"≤{MAX_ROLE_LEN} chars; \"prefix: description\"")
        layout.addRow("Role description", self._role)

        self._soul = QPlainTextEdit()
        self._soul.setPlaceholderText(
            f"# Soul section of profile.md (≤{MAX_PROFILE_SUMMARY_BYTES} bytes UTF-8)"
        )
        self._soul.setMinimumHeight(160)
        layout.addRow("Soul", self._soul)

        # Only the CLI runtimes are surfaced; provider is derived from harness.
        self._runtime_kind = QComboBox()
        self._runtime_kind.addItem("cli-local")
        self._runtime_kind.addItem("cli-docker")
        layout.addRow("Runtime", self._runtime_kind)

        self._harness = QComboBox()
        for h in ("claude-code", "codex", "gemini-cli"):
            self._harness.addItem(h)
        self._harness.currentTextChanged.connect(self._on_harness_changed)
        layout.addRow("Harness", self._harness)

        self._model = QComboBox()
        self._model.setEditable(True)
        self._model.setPlaceholderText("(daemon default)")
        layout.addRow("Model", self._model)

        actions = QHBoxLayout()
        self._save_btn = QPushButton("Save")
        self._save_btn.clicked.connect(self._on_save)
        self._revert_btn = QPushButton("Revert")
        self._revert_btn.clicked.connect(self._on_revert)
        actions.addStretch(1)
        actions.addWidget(self._revert_btn)
        actions.addWidget(self._save_btn)
        layout.addRow("", self._wrap(actions))

        scroll.setWidget(body)
        return scroll

    def _build_skills_tab(self) -> QWidget:
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        hint = QLabel(
            "Skills installed under <code>workspace/.claude/skills/</code> "
            "(agent-scope) and <code>~/.claude/skills/</code> (system-scope)."
        )
        hint.setStyleSheet("color: #6b7280;")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self._skills_list = QListWidget()
        self._skills_list.itemSelectionChanged.connect(self._on_skill_selected)
        split.addWidget(self._skills_list)
        self._skill_detail = QTextEdit()
        self._skill_detail.setReadOnly(True)
        self._skill_detail.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        self._skill_detail.setPlainText("(select a skill to see its SKILL.md)")
        split.addWidget(self._skill_detail)
        split.setSizes([220, 380])
        outer.addWidget(split, stretch=1)
        return wrap

    def _build_mcp_tab(self) -> QWidget:
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        hint = QLabel(
            "MCP servers configured for the agent's harness "
            "(command, args, env from the matching config file)."
        )
        hint.setStyleSheet("color: #6b7280;")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self._mcp_list = QListWidget()
        self._mcp_list.itemSelectionChanged.connect(self._on_mcp_selected)
        split.addWidget(self._mcp_list)
        self._mcp_detail = QTextEdit()
        self._mcp_detail.setReadOnly(True)
        self._mcp_detail.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        self._mcp_detail.setPlainText("(select an MCP server to see its command + env)")
        split.addWidget(self._mcp_detail)
        split.setSizes([220, 380])
        outer.addWidget(split, stretch=1)
        return wrap

    @staticmethod
    def _wrap(box) -> QWidget:
        wrap = QWidget()
        wrap.setLayout(box)
        return wrap

    # Public ────────────────────────────────────────────────────────

    def bind(self, agent_id: Optional[str]) -> None:
        self._agent_id = agent_id
        if agent_id is None:
            self._cfg = None
            return
        self._reload_from_disk()

    def _reload_from_disk(self) -> None:
        if not self._agent_id:
            return
        try:
            self._cfg = AgentConfig.load(self._agent_id)
        except Exception as exc:
            QMessageBox.warning(self, "load", f"could not load agent.yml: {exc}")
            return
        cfg = self._cfg
        self._slug.setText(cfg.puffo_core.slug)
        owner_slug = cfg.puffo_core.operator_slug
        owner_name = resolve_display_name(owner_slug)
        self._owner_slug.setText(
            f"{owner_name} ({owner_slug})" if owner_name and owner_name != owner_slug else owner_slug
        )
        self._display_name.setText(cfg.display_name)
        self._role.setText(cfg.role)
        self._role_short.setText(cfg.role_short)
        self._soul.setPlainText(_profile_summary(cfg))
        self._set_combo(self._runtime_kind, cfg.runtime.kind)
        self._set_combo(self._harness, cfg.runtime.harness)
        self._populate_model_combo(cfg.runtime.harness, cfg.runtime.model)
        self._populate_skills(cfg)
        self._populate_mcp(cfg)
        self._update_action_buttons()

    def _update_action_buttons(self) -> None:
        has = self._cfg is not None
        state = self._cfg.state if self._cfg else ""
        is_running = state == "running"
        self._pause_resume_btn.setEnabled(has and state in {"running", "paused"})
        self._pause_resume_btn.setText("Pause" if is_running else "Resume")
        self._refresh_btn.setEnabled(has)
        self._archive_btn.setEnabled(has)
        self._export_btn.setEnabled(has and state == "paused")
        self._export_btn.setToolTip(
            "Encrypted archive of this agent."
            if has and state == "paused"
            else "Pause the agent first; export refuses running workers (PUF-263)."
        )

    # Actions ──────────────────────────────────────────────────────

    def _on_pause_resume(self) -> None:
        if not self._cfg:
            return
        target = "paused" if self._cfg.state == "running" else "running"
        self._flip_state(target)

    def _on_refresh_session(self) -> None:
        if not self._agent_id:
            return
        confirm = QMessageBox.question(
            self,
            "Refresh session",
            "Drop the agent's CLI session and restart it with a fresh "
            "LLM context? The transcript inside the CLI tool will not "
            "be recovered.",
        )
        if confirm != QMessageBox.Yes:
            return
        session_path = cli_session_json_path(self._agent_id)
        try:
            session_path.unlink(missing_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Refresh session", f"could not clear session: {exc}")
            return
        try:
            flag = restart_flag_path(self._agent_id)
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("refresh-session", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Refresh session", f"could not write flag: {exc}")

    def _on_export(self) -> None:
        if not self._agent_id or not self._cfg:
            return
        if self._cfg.state != "paused":
            QMessageBox.information(
                self, "Export",
                "Pause the agent first — exports of running agents are rejected.",
            )
            return
        password, ok = QInputDialog.getText(
            self, "Export agent",
            "Password to encrypt the export archive:",
            QLineEdit.Password,
        )
        if not ok or not password:
            return
        target, _filter = QFileDialog.getSaveFileName(
            self, "Save encrypted export",
            f"{self._agent_id}.puffoagent",
            "Puffo Agent Export (*.puffoagent)",
        )
        if not target:
            return
        try:
            blob = export.pack(
                [self._agent_id],
                password,
                exported_by_slug=self._cfg.puffo_core.operator_slug or "",
            )
        except export.ExportError as exc:
            QMessageBox.warning(self, "Export", str(exc))
            return
        except Exception as exc:
            QMessageBox.warning(self, "Export", f"export failed: {exc}")
            return
        try:
            with open(target, "wb") as f:
                f.write(blob)
        except OSError as exc:
            QMessageBox.warning(self, "Export", f"could not write file: {exc}")
            return
        QMessageBox.information(self, "Export", f"Wrote {len(blob)} bytes to {target}")

    def _on_archive(self) -> None:
        if not self._agent_id:
            return
        confirm = QMessageBox.question(
            self,
            "Archive agent",
            f"Archive '{self._agent_id}'? The agent dir will be moved to "
            "~/.puffo-agent/archived/ on the next reconcile tick (~2s) "
            "and the worker stopped.",
        )
        if confirm != QMessageBox.Yes:
            return
        from ...state import archive_flag_path
        try:
            flag = archive_flag_path(self._agent_id)
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("requested", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Archive", f"could not write flag: {exc}")

    def _flip_state(self, target_state: str) -> None:
        if not self._cfg:
            return
        if self._cfg.state == target_state:
            return
        self._cfg.state = target_state
        try:
            self._cfg.save()
        except OSError as exc:
            QMessageBox.warning(self, target_state, f"could not save agent.yml: {exc}")
            return
        self._update_action_buttons()
        self.saved.emit(self._agent_id or "")

    # Editing ──────────────────────────────────────────────────────

    def _on_harness_changed(self, harness: str) -> None:
        current = self._model.currentText()
        self._populate_model_combo(harness, current)

    def _populate_model_combo(self, harness: str, current: str) -> None:
        self._model.blockSignals(True)
        self._model.clear()
        for m in _DEFAULT_MODELS.get(harness, [""]):
            self._model.addItem(m or "(daemon default)", m)
        idx = self._model.findData(current)
        if idx >= 0:
            self._model.setCurrentIndex(idx)
        else:
            self._model.setEditText(current)
        self._model.blockSignals(False)

    def _on_revert(self) -> None:
        self._reload_from_disk()

    def _on_save(self) -> None:
        if not self._cfg or not self._agent_id:
            return
        cfg = self._cfg

        runtime_kind = self._runtime_kind.currentText()
        harness = self._harness.currentText() if harness_applies(runtime_kind) else cfg.runtime.harness
        provider = _provider_for_harness(harness) or cfg.runtime.provider
        model = (self._model.currentData()
                 if self._model.currentData() is not None
                 else self._model.currentText()).strip()

        result = validate_triple(runtime_kind, provider, harness)
        if not result.ok:
            QMessageBox.warning(self, "Save", result.error or "invalid runtime triple")
            return

        role = self._role.text().strip()
        role_short = self._role_short.text().strip()
        if len(role.encode("utf-8")) > MAX_ROLE_LEN:
            QMessageBox.warning(self, "Save", f"role > {MAX_ROLE_LEN} chars")
            return
        if len(role_short.encode("utf-8")) > MAX_ROLE_SHORT_LEN:
            QMessageBox.warning(self, "Save", f"role_short > {MAX_ROLE_SHORT_LEN} chars")
            return

        soul = self._soul.toPlainText().strip()
        if len(soul.encode("utf-8")) > MAX_PROFILE_SUMMARY_BYTES:
            QMessageBox.warning(
                self, "Save",
                f"soul > {MAX_PROFILE_SUMMARY_BYTES} bytes (UTF-8)",
            )
            return

        cfg.display_name = self._display_name.text().strip() or cfg.display_name
        cfg.role = role
        cfg.role_short = role_short
        cfg.runtime.kind = runtime_kind
        cfg.runtime.provider = provider
        cfg.runtime.harness = harness
        cfg.runtime.model = model
        try:
            cfg.save()
            _update_profile_summary(cfg, soul)
        except Exception as exc:
            QMessageBox.warning(self, "Save", f"failed to persist: {exc}")
            return
        self.saved.emit(self._agent_id)

    # Skills + MCP ──────────────────────────────────────────────────

    def _populate_skills(self, cfg: AgentConfig) -> None:
        self._skills_list.clear()
        self._skill_detail.setPlainText("(select a skill to see its SKILL.md)")
        from ...state import agent_dir
        agent_root = agent_dir(cfg.id)
        home = Path.home()
        # Only show the dirs the agent's own harness can actually load.
        # Mixing harnesses would confuse the operator (a codex agent
        # never reads .claude/skills/).
        skill_dirname = _HARNESS_SKILL_DIRNAME.get(cfg.runtime.harness)
        if skill_dirname is None:
            self._skills_list.addItem(
                QListWidgetItem(f"(harness {cfg.runtime.harness!r} has no skill convention)")
            )
            return
        search = [
            ("agent", agent_root / skill_dirname / "skills"),
            ("host",  home / skill_dirname / "skills"),
        ]
        any_found = False
        for scope, root in search:
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                skill_md = entry / "SKILL.md"
                if not skill_md.exists():
                    continue
                item = QListWidgetItem(f"[{scope}] {entry.name}")
                item.setData(Qt.UserRole, str(skill_md))
                self._skills_list.addItem(item)
                any_found = True
        if not any_found:
            self._skills_list.addItem(QListWidgetItem("(no skills installed)"))

    def _on_skill_selected(self) -> None:
        items = self._skills_list.selectedItems()
        if not items:
            self._skill_detail.setPlainText("(select a skill to see its SKILL.md)")
            return
        skill_md = items[0].data(Qt.UserRole)
        if not isinstance(skill_md, str):
            return
        try:
            body = Path(skill_md).read_text(encoding="utf-8")
        except OSError as exc:
            self._skill_detail.setPlainText(f"(could not read {skill_md}: {exc})")
            return
        self._skill_detail.setPlainText(body)

    def _populate_mcp(self, cfg: AgentConfig) -> None:
        self._mcp_list.clear()
        self._mcp_detail.setPlainText("(select an MCP server to see its command + env)")
        from ...state import agent_dir
        agent_root = agent_dir(cfg.id)
        home = Path.home()
        any_found = False
        for scope, name, config in _scan_mcp_servers(
            agent_root, home, cfg.runtime.harness,
        ):
            item = QListWidgetItem(f"[{scope}] {name}")
            import json as _json
            item.setData(Qt.UserRole, _json.dumps(config, indent=2))
            self._mcp_list.addItem(item)
            any_found = True
        if not any_found:
            self._mcp_list.addItem(QListWidgetItem("(no MCP servers configured)"))

    def _on_mcp_selected(self) -> None:
        items = self._mcp_list.selectedItems()
        if not items:
            self._mcp_detail.setPlainText("(select an MCP server to see its command + env)")
            return
        payload = items[0].data(Qt.UserRole)
        if isinstance(payload, str):
            self._mcp_detail.setPlainText(payload)

    @staticmethod
    def _set_combo(combo: QComboBox, value: str, *, by_data: bool = False) -> None:
        if by_data:
            idx = combo.findData(value)
            if idx >= 0:
                combo.setCurrentIndex(idx)
                return
        idx = combo.findText(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        elif combo.isEditable():
            combo.setEditText(value)
