from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from _portal_support import write_test_agent
from puffo_agent.portal import cli
from puffo_agent.portal.state import AgentConfig, RuntimeState
from puffo_agent.portal.ui.widgets import agent_detail


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def agent_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(home))
    monkeypatch.setenv("PUFFO_HOME", str(home))
    write_test_agent(str(home), "threshold-ui")
    cfg = AgentConfig.load("threshold-ui")
    cfg.runtime.kind = "cli-local"
    cfg.runtime.harness = "claude-code"
    cfg.env_overrides = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}
    cfg.save()
    monkeypatch.setattr(agent_detail, "prefetch", lambda: None)
    return home


def test_threshold_control_uses_honest_default_label(qapp, agent_home):
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")

    assert view._autocompact.itemText(0) == "Default"
    assert view._autocompact.currentData() == "50"
    assert "Estimated 1000K window" in view._context_usage.text()
    assert "~500K (50%)" in view._context_usage.text()
    assert view._autocompact.isEnabled()


def test_preview_is_inert_before_an_agent_is_bound(qapp, agent_home):
    view = agent_detail.AgentDetail()
    view._update_autocompact_preview()
    assert view._context_usage.text() == "—"


def test_unknown_model_does_not_show_an_invented_window(qapp, agent_home):
    cfg = AgentConfig.load("threshold-ui")
    cfg.runtime.model = "claude-future-model"
    cfg.save()
    RuntimeState(max_context=0).save("threshold-ui")

    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")

    assert view._context_usage.text().startswith("Context limits unavailable.")


def test_threshold_control_uses_claude_reported_default(qapp, agent_home):
    cfg = AgentConfig.load("threshold-ui")
    cfg.env_overrides = {}
    cfg.save()
    RuntimeState(
        max_context=200_000,
        auto_compact_threshold_pct=83.5,
    ).save("threshold-ui")

    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")

    assert view._autocompact.itemText(0) == "Default (83.5%)"
    assert view._autocompact.currentData() == ""
    assert "200K window" in view._context_usage.text()
    assert "~167K (83.5%) (default)" in view._context_usage.text()
    assert "Estimated" not in view._context_usage.text()


def test_threshold_control_applies_to_cli_docker_claude(qapp, agent_home):
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")

    view._runtime_kind.setCurrentText("cli-docker")
    qapp.processEvents()

    assert view._autocompact.isEnabled()


def test_runtime_cli_reports_effective_local_codex_sandbox(agent_home, capsys):
    cfg = AgentConfig.load("threshold-ui")
    cfg.runtime.kind = "cli-local"
    cfg.runtime.provider = "openai"
    cfg.runtime.harness = "codex"
    cfg.runtime.sandbox = "workspace-write"
    cfg.save()

    assert cli.main(["agent", "runtime", "threshold-ui"]) == 0

    assert "sandbox:          workspace-write" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("runtime_kind", "harness"),
    [("ws-local", "claude-code"), ("ws-local", "codex")],
)
def test_threshold_control_disables_when_not_applicable(
    qapp, agent_home, runtime_kind, harness
):
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")

    view._runtime_kind.setCurrentText(runtime_kind)
    view._harness.setCurrentText(harness)
    qapp.processEvents()

    assert not view._autocompact.isEnabled()
    assert "require Claude Code or Codex" in view._autocompact.toolTip()


def test_threshold_control_applies_to_codex(qapp, agent_home):
    cfg = AgentConfig.load("threshold-ui")
    cfg.runtime.harness = "codex"
    cfg.runtime.provider = "openai"
    cfg.env_overrides = {"CODEX_AUTOCOMPACT_PCT_OVERRIDE": "75"}
    cfg.save()
    RuntimeState(max_context=258_400, auto_compact_threshold_pct=75).save(
        "threshold-ui"
    )
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")

    assert view._autocompact.isEnabled()
    assert view._autocompact.currentData() == "75"
    assert "258K window" in view._context_usage.text()
    assert "~193K (75%)" in view._context_usage.text()


def test_codex_save_uses_codex_override_key(qapp, agent_home):
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")
    view._harness.setCurrentText("codex")
    view._autocompact.setCurrentIndex(view._autocompact.findData("75"))

    view._on_save()

    assert AgentConfig.load("threshold-ui").env_overrides == {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50",
        "CODEX_AUTOCOMPACT_PCT_OVERRIDE": "75",
    }


def test_avatar_upload_uses_shared_profile_helper(
    qapp, agent_home, tmp_path, monkeypatch,
):
    avatar = tmp_path / "avatar.png"
    avatar.write_bytes(b"avatar-bytes")
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")
    monkeypatch.setattr(
        agent_detail.QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(avatar), ""),
    )
    calls = []

    async def fake_upload(cfg, data):
        calls.append((cfg.id, data))
        return "https://relay.example/blobs/blob_1"

    async def fake_verify(_cfg, _url):
        return b"avatar-bytes"

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(agent_detail, "upload_avatar", fake_upload)
    monkeypatch.setattr(agent_detail, "_verify_avatar_blob", fake_verify)
    monkeypatch.setattr(agent_detail.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(agent_detail.disk_cache, "write_avatar_bytes", lambda *_args: None)
    emitted = []
    view._avatar_uploaded.connect(lambda url, error: emitted.append((url, error)))

    view._on_change_avatar()

    assert calls == [("threshold-ui", b"avatar-bytes")]
    assert emitted == [("https://relay.example/blobs/blob_1", "")]


def test_threshold_selection_participates_in_dirty_state(qapp, agent_home):
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")
    assert not view._save_btn.isEnabled()

    view._autocompact.setCurrentIndex(0)
    qapp.processEvents()

    assert view._save_btn.isEnabled()


def test_threshold_selection_updates_the_preview_immediately(qapp, agent_home):
    cfg = AgentConfig.load("threshold-ui")
    cfg.runtime.model = "claude-haiku-4-5"
    cfg.save()
    RuntimeState(
        max_context=200_000,
        auto_compact_threshold_pct=50,
    ).save("threshold-ui")
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")

    view._autocompact.setCurrentIndex(view._autocompact.findData("30"))
    qapp.processEvents()

    assert "200K window" in view._context_usage.text()
    assert "~100K (50%)" in view._context_usage.text()


def test_switching_from_override_to_default_does_not_reuse_stale_pct(
    qapp, agent_home
):
    RuntimeState(
        max_context=200_000,
        auto_compact_threshold_pct=50,
    ).save("threshold-ui")
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")

    view._autocompact.setCurrentIndex(0)
    qapp.processEvents()

    assert "compact point unavailable" in view._context_usage.text()


def test_save_persists_selected_threshold(qapp, agent_home):
    RuntimeState(
        max_context=200_000,
        auto_compact_threshold_pct=50,
    ).save("threshold-ui")
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")
    view._autocompact.setCurrentIndex(view._autocompact.findData("75"))

    view._on_save()

    assert AgentConfig.load("threshold-ui").env_overrides == {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75"
    }
    state = RuntimeState.load("threshold-ui")
    assert state is not None
    assert state.max_context == 0
    assert state.auto_compact_threshold_pct is None


def test_non_context_save_keeps_runtime_limits(qapp, agent_home):
    RuntimeState(
        max_context=200_000,
        auto_compact_threshold_pct=50,
    ).save("threshold-ui")
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")
    view._display_name.setText("Renamed")

    view._on_save()

    state = RuntimeState.load("threshold-ui")
    assert state is not None
    assert state.max_context == 200_000
    assert state.auto_compact_threshold_pct == 50


def test_save_default_clears_existing_threshold(qapp, agent_home):
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")
    view._autocompact.setCurrentIndex(0)

    view._on_save()

    assert AgentConfig.load("threshold-ui").env_overrides == {}


def test_save_reports_validator_failure(qapp, agent_home, monkeypatch):
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")
    warnings = []
    monkeypatch.setattr(
        agent_detail,
        "merge_env_overrides",
        lambda _current, _updates: (_ for _ in ()).throw(
            ValueError("invalid threshold")
        ),
    )
    monkeypatch.setattr(
        agent_detail.QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )

    view._on_save()

    assert warnings
    assert "invalid threshold" in warnings[0][-1]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("_role_short", "界" * 20, "role_short"),
        (
            "_soul",
            "界" * (agent_detail.MAX_PROFILE_SUMMARY_BYTES + 1),
            "soul",
        ),
    ],
)
def test_save_rejects_oversized_utf8_fields(
    qapp, agent_home, monkeypatch, field, value, message
):
    view = agent_detail.AgentDetail()
    view.bind("threshold-ui")
    warnings = []
    monkeypatch.setattr(
        agent_detail.QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    widget = getattr(view, field)
    if field == "_soul":
        widget.setPlainText(value)
    else:
        widget.setText(value)

    view._on_save()

    assert warnings
    assert message in warnings[0][-1]
