"""Right-pane left half: agent Info / Skills / MCP tabs."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import asyncio
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
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

from ....agent import disk_cache
from ....agent.model_catalog import ModelOption, prefetch, provider_models
from ... import export
from ...api.handlers import (
    MAX_AVATAR_BYTES,
    MAX_PROFILE_SUMMARY_BYTES,
    MAX_ROLE_LEN,
    MAX_ROLE_SHORT_LEN,
    _profile_summary,
    _update_profile_summary,
    _upload_avatar_via_agent_keystore,
)
from ...runtime_matrix import (
    HARNESS_PROVIDERS,
    harness_applies,
    validate_triple,
)
from ...state import AgentConfig
from ..names import resolve_display_name
from .avatar import initial_pixmap


def _provider_for_harness(harness: str) -> str:
    """Each cli-* harness pins to exactly one provider."""
    providers = HARNESS_PROVIDERS.get(harness)
    if providers and len(providers) == 1:
        return next(iter(providers))
    return ""


async def _verify_avatar_blob(cfg: AgentConfig, url: str) -> bytes:
    """Signed GET on the freshly-uploaded blob URL so the caller can
    byte-compare against the original payload."""
    from ....crypto.http_client import PuffoCoreHttpClient
    from ....crypto.keystore import KeyStore

    base = cfg.puffo_core.server_url.rstrip("/")
    if not url.startswith(base + "/"):
        raise RuntimeError(f"blob URL {url!r} is not under {base!r}")
    path = url[len(base):]
    ks = KeyStore.for_agent(cfg.id)
    http = PuffoCoreHttpClient(cfg.puffo_core.server_url, ks, cfg.puffo_core.slug)
    try:
        return await http.get_bytes(path)
    finally:
        await http.close()


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
        # Daemon-managed puffo MCP — registered via --mcp-config to
        # claude-cli, so it never appears in .claude.json.
        json_sources = [
            ("puffo",           agent_root / "mcp-config.json"),
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



class AgentDetail(QWidget):
    """Tabbed info pane bound to a single agent id."""

    saved = Signal(str)
    _avatar_uploaded = Signal(str, str)  # (new_url, error_message)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._agent_id: Optional[str] = None
        self._cfg: Optional[AgentConfig] = None
        self._initial_snapshot: Optional[tuple] = None
        # Warm the live claude-code model list off-thread so the picker
        # reads from cache, not a blocking /v1/models fetch.
        prefetch()
        self._build()
        self._avatar_uploaded.connect(self._on_avatar_uploaded)
        for w, sig in (
            (self._display_name, "textChanged"),
            (self._role,         "textChanged"),
            (self._role_short,   "textChanged"),
            (self._soul,         "textChanged"),
            (self._runtime_kind, "currentTextChanged"),
            (self._harness,      "currentTextChanged"),
            (self._model,        "currentTextChanged"),
            (self._effort,       "currentTextChanged"),
        ):
            getattr(w, sig).connect(self._check_dirty)
        self._check_dirty()

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

        avatar_row = QHBoxLayout()
        self._avatar_preview = QLabel()
        self._avatar_preview.setFixedSize(64, 64)
        avatar_row.addWidget(self._avatar_preview)
        self._avatar_btn = QPushButton("Change…")
        self._avatar_btn.clicked.connect(self._on_change_avatar)
        avatar_row.addWidget(self._avatar_btn)
        avatar_row.addStretch(1)
        layout.addRow("Avatar", self._wrap(avatar_row))

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

        # CLI runtimes + ws-local are surfaced. provider is derived from
        # harness for the CLI kinds; ws-local has no harness/model on the
        # daemon side so the dropdowns get locked in ``set_agent`` for
        # those agents.
        self._runtime_kind = QComboBox()
        self._runtime_kind.addItem("cli-local")
        self._runtime_kind.addItem("cli-docker")
        self._runtime_kind.addItem("ws-local")
        layout.addRow("Runtime", self._runtime_kind)

        self._harness = QComboBox()
        for h in ("claude-code", "codex"):
            self._harness.addItem(h)
        self._harness.currentTextChanged.connect(self._on_harness_changed)
        layout.addRow("Harness", self._harness)

        self._model = QComboBox()
        layout.addRow("Model", self._model)

        self._effort = QComboBox()
        layout.addRow("Effort", self._effort)

        # Read-only access policy: claude-code shows its permission mode;
        # codex shows the sandbox + approval policy.
        self._access = QLabel("")
        self._access.setStyleSheet("color: #6b7280;")
        self._access.setWordWrap(True)
        layout.addRow("Access", self._access)

        self._auto_accept_dm = QCheckBox(
            "Auto-accept DMs from anyone"
        )
        self._auto_accept_dm.setToolTip(
            "When off, DMs from anyone other than the operator are "
            "buffered and the operator is prompted (threaded y/n DM "
            "per sender) before the agent sees the message. y → "
            "allowlist + deliver; n → blocklist + drop."
        )
        layout.addRow("DM policy", self._auto_accept_dm)

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
            "Skills the running harness can load — user-scope "
            "(<code>&lt;agent&gt;/.claude/skills/</code>), project-scope "
            "(<code>workspace/.claude/skills/</code> for claude-code, "
            "<code>workspace/.agents/skills/</code> for codex), plus per-"
            "plugin <code>plugins/&lt;plugin&gt;/skills/&lt;name&gt;/SKILL.md</code>, "
            "and the operator's host-scope source."
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
            self._initial_snapshot = None
            self._check_dirty()
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
        self._paint_avatar(cfg)
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
        self._auto_accept_dm.setChecked(cfg.puffo_core.auto_accept_dm)
        self._set_combo(self._runtime_kind, cfg.runtime.kind)
        self._set_combo(self._harness, cfg.runtime.harness)
        self._populate_model_combo(cfg.runtime.harness, cfg.runtime.model)
        self._populate_effort_combo(cfg.runtime.harness, cfg.runtime.inference_level)
        self._access.setText(self._access_summary(cfg.runtime.harness, cfg))
        self._populate_skills(cfg)
        self._populate_mcp(cfg)
        # ws-local agents bring their own brain — runtime / harness / model
        # have no daemon-side meaning, so lock the dropdowns and grey them
        # out explicitly. PySide's platform style on Windows leaves a
        # disabled QComboBox visually indistinguishable from an enabled one
        # in some themes, so the stylesheet does the work.
        is_ws_local = (cfg.runtime.kind or "") == "ws-local"
        disabled_qss = (
            "QComboBox:disabled {"
            " color: #9ca3af; background-color: #f3f4f6;"
            " border: 1px solid #e5e7eb;"
            "}"
        )
        for w in (self._runtime_kind, self._harness, self._model):
            w.setEnabled(not is_ws_local)
            w.setStyleSheet(disabled_qss if is_ws_local else "")
            w.setToolTip(
                "ws-local agents bring their own AI tool — daemon-side "
                "runtime / harness / model don't apply."
                if is_ws_local else ""
            )
        # Skills + MCP are harness conventions (~/.claude, ~/.codex, …).
        # ws-local agents run no harness, so both tabs become inert.
        for tab_idx in (1, 2):
            self._tabs.setTabEnabled(tab_idx, not is_ws_local)
            self._tabs.setTabToolTip(
                tab_idx,
                "ws-local agents run no harness — Skills + MCP don't apply."
                if is_ws_local else "",
            )
        self._update_action_buttons()
        self._initial_snapshot = self._snapshot()
        self._check_dirty()

    def _snapshot(self) -> tuple:
        return (
            self._display_name.text(),
            self._role.text(),
            self._role_short.text(),
            self._soul.toPlainText(),
            self._runtime_kind.currentText(),
            self._harness.currentText(),
            self._model.currentData() or "",
            self._effort.currentData() or "",
        )

    def _check_dirty(self) -> None:
        dirty = (
            self._initial_snapshot is not None
            and self._snapshot() != self._initial_snapshot
        )
        self._save_btn.setEnabled(dirty)
        self._revert_btn.setEnabled(dirty)

    def _update_action_buttons(self) -> None:
        has = self._cfg is not None
        state = self._cfg.state if self._cfg else ""
        is_running = state == "running"
        is_ws_local = bool(self._cfg) and (self._cfg.runtime.kind or "") == "ws-local"
        self._pause_resume_btn.setEnabled(has and state in {"running", "paused"})
        self._pause_resume_btn.setText("Pause" if is_running else "Resume")
        # ws-local has no harness subprocess to drop a session for — there's
        # nothing to refresh. The attach client is the agent's "session".
        self._refresh_btn.setEnabled(has and not is_ws_local)
        self._refresh_btn.setToolTip(
            "ws-local agents have no harness session to refresh — the attach "
            "client is the agent's session."
            if is_ws_local
            else "Drop cli_session.json + restart the worker for a fresh LLM context."
        )
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
        if not self._agent_id or not self._cfg:
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
        # refresh(session=True): worker unlinks cli_session.json via
        # adapter.reload(with_session=True) on its next turn.
        import json
        import time
        workspace = self._cfg.resolve_workspace_dir()
        pa_dir = workspace / ".puffo-agent"
        try:
            pa_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"requested_at": int(time.time())})
            (pa_dir / "refresh_agent.flag").write_text(payload, encoding="utf-8")
            (pa_dir / "refresh_session.flag").write_text(payload, encoding="utf-8")
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

    def _paint_avatar(self, cfg: AgentConfig) -> None:
        size = self._avatar_preview.size().width()
        url = cfg.avatar_url
        pm = None
        if url:
            cached = disk_cache.avatar_cache_path(url)
            if cached.exists():
                pm = QPixmap(str(cached))
        if pm is None or pm.isNull():
            pm = initial_pixmap(cfg.display_name, cfg.puffo_core.slug or cfg.id, size=size)
        else:
            pm = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._avatar_preview.setPixmap(pm)

    def _on_change_avatar(self) -> None:
        if not self._cfg:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Pick an image",
            "",
            "Images (*.png *.jpg *.jpeg *.webp);;All files (*)",
        )
        if not path:
            return
        try:
            data = open(path, "rb").read()
        except OSError as exc:
            QMessageBox.warning(self, "Avatar", str(exc))
            return
        if len(data) > MAX_AVATAR_BYTES:
            QMessageBox.warning(
                self, "Avatar",
                f"Image is {len(data)} bytes; cap is {MAX_AVATAR_BYTES}.",
            )
            return
        cfg = self._cfg
        self._avatar_btn.setEnabled(False)
        self._avatar_btn.setText("Uploading…")

        def worker() -> None:
            try:
                url = asyncio.run(_upload_avatar_via_agent_keystore(cfg, data))
                if not url:
                    self._avatar_uploaded.emit("", "upload returned empty url")
                    return
                # Round-trip the blob back via a signed GET; mismatch
                # means the server stored something other than what we
                # sent (truncation, content-encoding, wrong blob_id).
                roundtrip = asyncio.run(_verify_avatar_blob(cfg, url))
                if roundtrip != data:
                    self._avatar_uploaded.emit(
                        "",
                        f"verify failed: round-tripped {len(roundtrip)} bytes, "
                        f"expected {len(data)}",
                    )
                    return
                disk_cache.write_avatar_bytes(url, data)
                self._avatar_uploaded.emit(url, "")
            except Exception as exc:
                self._avatar_uploaded.emit("", f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_avatar_uploaded(self, url: str, error: str) -> None:
        self._avatar_btn.setEnabled(True)
        self._avatar_btn.setText("Change…")
        if error or not url:
            QMessageBox.warning(self, "Avatar", error or "upload returned empty url")
            return
        if not self._cfg:
            return
        self._cfg.avatar_url = url
        try:
            self._cfg.save()
        except OSError as exc:
            QMessageBox.warning(self, "Avatar", f"could not save agent.yml: {exc}")
            return
        self._paint_avatar(self._cfg)
        self.saved.emit(self._agent_id or "")

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
        self._populate_effort_combo(harness, self._effort.currentData() or "")
        if self._cfg is not None:
            self._access.setText(self._access_summary(harness, self._cfg))

    def _populate_effort_combo(self, harness: str, current: str) -> None:
        from ....mcp.config import INFERENCE_LEVELS

        # codex: no xhigh tier
        levels = [
            lv for lv in INFERENCE_LEVELS
            if not (harness == "codex" and lv == "xhigh")
        ]
        self._effort.blockSignals(True)
        self._effort.clear()
        self._effort.addItem("(default)", "")
        for lv in levels:
            self._effort.addItem(lv, lv)
        idx = self._effort.findData(current)
        self._effort.setCurrentIndex(idx if idx >= 0 else 0)
        self._effort.blockSignals(False)

    def _access_summary(self, harness: str, cfg) -> str:
        """Read-only access line: permission mode for claude-code,
        sandbox + approval policy for codex."""
        if (cfg.runtime.kind or "") == "ws-local":
            return "n/a — ws-local brings its own AI"
        if harness == "codex":
            policy = (
                "never" if cfg.runtime.permission_mode == "bypassPermissions"
                else "untrusted"
            )
            return f"sandbox: {cfg.runtime.sandbox} · approve: {policy}"
        return f"permission: {cfg.runtime.permission_mode}"

    def _populate_model_combo(self, harness: str, current: str) -> None:
        self._model.blockSignals(True)
        self._model.clear()
        options = provider_models(harness)
        # Preserve a user-saved model that isn't in the catalog so
        # editing other fields doesn't silently flip it to default.
        if current and current not in [o.id for o in options]:
            options = options + [ModelOption(current, current)]
        for o in options:
            self._model.addItem(o.label, o.id)
        idx = self._model.findData(current)
        if idx >= 0:
            self._model.setCurrentIndex(idx)
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
        model = (self._model.currentData() or "").strip()

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
        cfg.puffo_core.auto_accept_dm = self._auto_accept_dm.isChecked()
        cfg.runtime.inference_level = self._effort.currentData() or ""
        try:
            cfg.save()
            _update_profile_summary(cfg, soul)
        except Exception as exc:
            QMessageBox.warning(self, "Save", f"failed to persist: {exc}")
            return
        self._reload_from_disk()
        self.saved.emit(self._agent_id)

    # Skills + MCP ──────────────────────────────────────────────────

    def _populate_skills(self, cfg: AgentConfig) -> None:
        self._skills_list.clear()
        self._skill_detail.setPlainText("(select a skill to see its SKILL.md)")
        from ...state import agent_dir
        agent_root = agent_dir(cfg.id)
        workspace = cfg.resolve_workspace_dir()
        home = Path.home()
        harness = cfg.runtime.harness

        # Every SKILL.md the running harness can actually load, grouped
        # by where it came from. Each scope is a (label, root) pair —
        # root holds top-level ``<name>/SKILL.md`` subdirs; plugin
        # scopes are enumerated separately below.
        scopes: list[tuple[str, Path]] = []
        plugin_roots: list[Path] = []
        if harness == "claude-code":
            scopes.append(("agent",     agent_root / ".claude" / "skills"))
            scopes.append(("workspace", workspace  / ".claude" / "skills"))
            scopes.append(("host",      home       / ".claude" / "skills"))
            plugin_roots.append(agent_root / ".claude" / "plugins")
        elif harness == "codex":
            scopes.append(("workspace", workspace  / ".agents" / "skills"))
            scopes.append(("host",      home       / ".agents" / "skills"))
        elif harness == "gemini-cli":
            scopes.append(("agent", agent_root / ".gemini" / "skills"))
            scopes.append(("host",  home       / ".gemini" / "skills"))
        else:
            self._skills_list.addItem(
                QListWidgetItem(f"(harness {harness!r} has no skill convention)")
            )
            return

        any_found = False

        for scope_label, root in scopes:
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                skill_md = entry / "SKILL.md"
                if not skill_md.exists():
                    continue
                item = QListWidgetItem(f"[{scope_label}] {entry.name}")
                item.setData(Qt.UserRole, str(skill_md))
                self._skills_list.addItem(item)
                any_found = True

        # Plugin scope: per-plugin ``skills/<name>/SKILL.md`` is the
        # convention Claude Code's plugin system loads. The marketplace
        # tree nests several levels deep so we rglob and label with
        # the immediate plugin dir.
        for plugins_root in plugin_roots:
            if not plugins_root.is_dir():
                continue
            for skill_md in sorted(plugins_root.rglob("skills/*/SKILL.md")):
                plugin_dir = skill_md.parent.parent.parent  # <plugin>/skills/<name>/SKILL.md
                plugin_name = plugin_dir.name
                skill_name = skill_md.parent.name
                item = QListWidgetItem(f"[plugin:{plugin_name}] {skill_name}")
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
