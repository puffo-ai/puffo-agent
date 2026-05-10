"""Unit tests for permission_mode validation and cli-local wiring.

Most of the suite covers the proxy modes
(``default`` / ``acceptEdits`` / ``auto`` / ``dontAsk``) which are
temporarily unsupported; only ``bypassPermissions`` works today.
Module-level skip keeps the suite intact until the permission DM
flow lands.
"""

import pytest

pytest.skip(
    "permission proxy modes pending — only bypassPermissions is "
    "currently supported; see local_cli.py VALID_PERMISSION_MODES.",
    allow_module_level=True,
)

import json  # noqa: E402
import logging  # noqa: E402

from puffo_agent.agent.adapters.local_cli import (  # noqa: E402
    PERMISSION_HOOK_FULL_MATCHER,
    PERMISSION_HOOK_NON_EDIT_MATCHER,
    VALID_PERMISSION_MODES,
    _is_puffo_agent_hook_entry,
    _sanitise_permission_mode,
    LocalCLIAdapter,
)


# ── _sanitise_permission_mode ────────────────────────────────────────────────


class TestSanitisePermissionMode:
    @pytest.mark.parametrize("mode", [
        "default", "acceptEdits", "auto", "dontAsk", "bypassPermissions",
    ])
    def test_known_modes_pass_through(self, mode):
        assert _sanitise_permission_mode(mode, "a") == mode

    def test_empty_defaults_to_default(self):
        assert _sanitise_permission_mode("", "a") == "default"

    def test_unknown_mode_falls_back_to_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = _sanitise_permission_mode("paranoid", "han-local-cli")
        assert result == "default"
        assert any(
            "unknown permission_mode" in r.message and "paranoid" in r.message
            for r in caplog.records
        ), "expected a WARNING log on unknown mode"

    def test_plan_mode_rejected(self):
        # 'plan' is deliberately unsupported (research mode, not
        # useful for chat-reply); silent fallback to 'default'.
        assert _sanitise_permission_mode("plan", "a") == "default"

    def test_case_sensitive(self):
        # claude-code is case-sensitive; "Default" is not valid.
        assert _sanitise_permission_mode("Default", "a") == "default"

    def test_valid_set_excludes_plan(self):
        # Guard: re-adding 'plan' must be a conscious change.
        assert "plan" not in VALID_PERMISSION_MODES

    def test_valid_set_has_five_modes(self):
        assert len(VALID_PERMISSION_MODES) == 5


# ── _build_command ──────────────────────────────────────────────────────────


def _make_adapter(
    permission_mode: str = "default",
    model: str = "",
    claude_dir: str = "/tmp/ws/.claude",
) -> LocalCLIAdapter:
    # Paths don't need to exist; we never spawn. Tests that touch
    # settings.json pass a real tmp_path for claude_dir.
    return LocalCLIAdapter(
        agent_id="a",
        model=model,
        workspace_dir="/tmp/ws",
        claude_dir=claude_dir,
        session_file="/tmp/a/cli_session.json",
        mcp_config_file="/tmp/a/mcp-config.json",
        agent_home_dir="/tmp/a",
        permission_mode=permission_mode,
    )


class TestBuildCommand:
    def test_command_starts_with_claude_and_permission_mode(self):
        adapter = _make_adapter(permission_mode="default")
        cmd = adapter._build_command(extra_args=[])
        assert cmd[0] == "claude"
        # --permission-mode must appear before user extra_args.
        assert "--permission-mode" in cmd
        i = cmd.index("--permission-mode")
        assert cmd[i + 1] == "default"

    def test_model_flag_included_when_set(self):
        adapter = _make_adapter(permission_mode="default", model="claude-opus-4-6")
        cmd = adapter._build_command(extra_args=[])
        assert "--model" in cmd
        assert "claude-opus-4-6" in cmd

    def test_model_flag_omitted_when_empty(self):
        adapter = _make_adapter(permission_mode="default", model="")
        cmd = adapter._build_command(extra_args=[])
        assert "--model" not in cmd

    def test_bypass_permissions_passes_through(self):
        adapter = _make_adapter(permission_mode="bypassPermissions")
        cmd = adapter._build_command(extra_args=[])
        i = cmd.index("--permission-mode")
        assert cmd[i + 1] == "bypassPermissions"

    def test_never_passes_dangerously_skip_permissions(self):
        # Regression guard: this flag is deprecated in favour of
        # --permission-mode. If it returns, permission decisions
        # silent-bypass and the MCP proxy never fires.
        for mode in VALID_PERMISSION_MODES:
            adapter = _make_adapter(permission_mode=mode)
            cmd = adapter._build_command(extra_args=["--foo", "bar"])
            assert "--dangerously-skip-permissions" not in cmd

    def test_unknown_mode_sanitised_at_construction(self, caplog):
        with caplog.at_level(logging.WARNING):
            adapter = _make_adapter(permission_mode="lolwut")
        assert adapter.permission_mode == "default"
        cmd = adapter._build_command(extra_args=[])
        i = cmd.index("--permission-mode")
        assert cmd[i + 1] == "default"

    def test_extra_args_preserved(self):
        adapter = _make_adapter(permission_mode="default")
        cmd = adapter._build_command(extra_args=["--mcp-config", "/x.json"])
        assert cmd[-2] == "--mcp-config"
        assert cmd[-1] == "/x.json"


# ── _hook_matcher_for_mode + _write_permission_hook_settings ─────────────────
#
# claude runs PreToolUse hooks regardless of --permission-mode, so
# the hook registration must itself be gated on the mode — else
# bypassPermissions still DMs the owner on every Bash call.


class TestHookMatcherForMode:
    def test_default_returns_full_matcher(self):
        adapter = _make_adapter(permission_mode="default")
        assert adapter._hook_matcher_for_mode() == PERMISSION_HOOK_FULL_MATCHER

    def test_accept_edits_returns_narrow_matcher(self):
        # Narrow matcher EXCLUDES edit tools — the point of
        # acceptEdits is auto-accepting them.
        adapter = _make_adapter(permission_mode="acceptEdits")
        matcher = adapter._hook_matcher_for_mode()
        assert matcher == PERMISSION_HOOK_NON_EDIT_MATCHER
        assert "Edit" not in matcher
        assert "Write" not in matcher
        assert "Bash" in matcher

    @pytest.mark.parametrize("mode", ["auto", "dontAsk", "bypassPermissions"])
    def test_auto_off_modes_return_none(self, mode):
        adapter = _make_adapter(permission_mode=mode)
        assert adapter._hook_matcher_for_mode() is None


def _read_settings(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _puffo_agent_pretools(settings: dict) -> list[dict]:
    hooks_cfg = settings.get("hooks") or {}
    pretool = hooks_cfg.get("PreToolUse") or []
    return [e for e in pretool if _is_puffo_agent_hook_entry(e)]


class TestWritePermissionHookSettings:
    """Reconciliation: settings.json must reflect the current
    permission_mode, not whatever was written on a previous run.
    """

    def test_default_mode_registers_full_matcher(self, tmp_path):
        adapter = _make_adapter(
            permission_mode="default", claude_dir=str(tmp_path),
        )
        adapter._write_permission_hook_settings()

        settings = _read_settings(tmp_path / "settings.json")
        ours = _puffo_agent_pretools(settings)
        assert len(ours) == 1
        assert ours[0]["matcher"] == PERMISSION_HOOK_FULL_MATCHER

    def test_accept_edits_registers_narrow_matcher(self, tmp_path):
        adapter = _make_adapter(
            permission_mode="acceptEdits", claude_dir=str(tmp_path),
        )
        adapter._write_permission_hook_settings()

        settings = _read_settings(tmp_path / "settings.json")
        ours = _puffo_agent_pretools(settings)
        assert len(ours) == 1
        assert ours[0]["matcher"] == PERMISSION_HOOK_NON_EDIT_MATCHER

    @pytest.mark.parametrize("mode", ["auto", "dontAsk", "bypassPermissions"])
    def test_auto_off_modes_do_not_register_hook(self, mode, tmp_path):
        # Core invariant: bypassPermissions must NOT leave the hook
        # in settings.json — the hook would override the mode and
        # DM the owner anyway.
        adapter = _make_adapter(
            permission_mode=mode, claude_dir=str(tmp_path),
        )
        adapter._write_permission_hook_settings()

        settings = _read_settings(tmp_path / "settings.json")
        assert _puffo_agent_pretools(settings) == []

    def test_mode_switch_removes_stale_entry(self, tmp_path):
        # default -> bypassPermissions reconciles the stale entry out
        # of settings.json so the mode change takes effect.
        first = _make_adapter(
            permission_mode="default", claude_dir=str(tmp_path),
        )
        first._write_permission_hook_settings()
        assert len(_puffo_agent_pretools(_read_settings(tmp_path / "settings.json"))) == 1

        second = _make_adapter(
            permission_mode="bypassPermissions", claude_dir=str(tmp_path),
        )
        second._write_permission_hook_settings()
        settings = _read_settings(tmp_path / "settings.json")
        assert _puffo_agent_pretools(settings) == []

    def test_mode_switch_updates_matcher(self, tmp_path):
        # default -> acceptEdits swaps full matcher for narrow, no
        # stacking.
        first = _make_adapter(
            permission_mode="default", claude_dir=str(tmp_path),
        )
        first._write_permission_hook_settings()
        second = _make_adapter(
            permission_mode="acceptEdits", claude_dir=str(tmp_path),
        )
        second._write_permission_hook_settings()

        settings = _read_settings(tmp_path / "settings.json")
        ours = _puffo_agent_pretools(settings)
        assert len(ours) == 1
        assert ours[0]["matcher"] == PERMISSION_HOOK_NON_EDIT_MATCHER

    def test_preserves_non_puffoagent_hooks(self, tmp_path):
        # Operator-written settings (custom lint hooks, etc.) must
        # survive reconciliation; we only rewrite our own entry.
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{
                        "type": "command",
                        "command": "/usr/local/bin/my-custom-hook.sh",
                    }]},
                ],
                "PostToolUse": [
                    {"matcher": "Edit", "hooks": [{
                        "type": "command", "command": "echo post",
                    }]},
                ],
            },
            "env": {"MY_VAR": "keepme"},
        }))

        adapter = _make_adapter(
            permission_mode="default", claude_dir=str(tmp_path),
        )
        adapter._write_permission_hook_settings()

        settings = _read_settings(settings_path)
        assert settings["env"]["MY_VAR"] == "keepme"
        assert "PostToolUse" in settings["hooks"]
        pretool = settings["hooks"]["PreToolUse"]
        # Custom hook survives, plus ours.
        assert any(
            h.get("command", "").endswith("my-custom-hook.sh")
            for e in pretool for h in (e.get("hooks") or [])
        )
        assert len(_puffo_agent_pretools(settings)) == 1

    def test_repeat_writes_do_not_stack(self, tmp_path):
        # Worker start calls this every spawn; must be idempotent.
        adapter = _make_adapter(
            permission_mode="default", claude_dir=str(tmp_path),
        )
        for _ in range(5):
            adapter._write_permission_hook_settings()
        settings = _read_settings(tmp_path / "settings.json")
        assert len(_puffo_agent_pretools(settings)) == 1


class TestIsPuffoagentHookEntry:
    def test_detects_by_command_marker(self):
        entry = {
            "matcher": "Bash",
            "hooks": [{
                "type": "command",
                "command": '"/usr/bin/python" -m puffo_agent.hooks.permission',
            }],
        }
        assert _is_puffo_agent_hook_entry(entry) is True

    def test_ignores_user_hooks(self):
        entry = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "echo hi"}],
        }
        assert _is_puffo_agent_hook_entry(entry) is False

    def test_handles_malformed_entries(self):
        assert _is_puffo_agent_hook_entry("not a dict") is False
        assert _is_puffo_agent_hook_entry({}) is False
        assert _is_puffo_agent_hook_entry({"hooks": "not a list"}) is False
