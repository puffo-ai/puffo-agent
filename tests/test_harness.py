"""Tests for the harness abstraction layer.

Covers:
  1. ``build_harness`` resolves the runtime.harness string to a
     concrete ``Harness`` (default + explicit values).
  2. ``supports_claude_specific_tools()`` matches the MCP tool gating
     (claude-code -> True, hermes -> False).
  3. The MCP tool guard raises a clear error under non-claude harnesses
     so agents stop retrying tools that won't take effect.

The actual ``hermes chat -q`` subprocess path is not exercised here;
that needs a live hermes install and is covered in the smoke suite.
"""

from __future__ import annotations

import asyncio

import pytest

from puffo_agent.agent.harness import (
    ClaudeCodeHarness,
    Harness,
    HermesHarness,
    build_harness,
)


# ── build_harness ────────────────────────────────────────────────────────────


def test_build_harness_defaults_to_claude_code():
    """Backward-compat: agents with ``harness=""`` keep claude-code."""
    h = build_harness("")
    assert isinstance(h, ClaudeCodeHarness)
    assert h.name() == "claude-code"


def test_build_harness_explicit_claude_code():
    h = build_harness("claude-code")
    assert isinstance(h, ClaudeCodeHarness)


def test_build_harness_hermes():
    h = build_harness("hermes")
    assert isinstance(h, HermesHarness)
    assert h.name() == "hermes"


def test_build_harness_unknown_raises():
    with pytest.raises(ValueError, match="unknown harness"):
        build_harness("not-a-harness")


# ── supports_claude_specific_tools ────────────────────────────────────────────


def test_claude_code_supports_claude_tools():
    assert ClaudeCodeHarness().supports_claude_specific_tools() is True


def test_hermes_does_not_support_claude_tools():
    """The claude-specific MCP tools (install_skill, refresh, etc.)
    write to paths hermes doesn't read."""
    assert HermesHarness().supports_claude_specific_tools() is False


def test_base_harness_defaults_to_not_supporting():
    """New harness authors must opt IN to claude-specific tools, so a
    forgotten override can't silently enable write paths a harness
    doesn't understand."""
    class MinimalHarness(Harness):
        def name(self) -> str:
            return "minimal"
    assert MinimalHarness().supports_claude_specific_tools() is False


# ── supported_providers (runtime-matrix feed) ────────────────────────────────


def test_claude_code_providers_anthropic_only():
    assert ClaudeCodeHarness().supported_providers() == frozenset({"anthropic"})


def test_hermes_providers_anthropic_and_openai():
    assert HermesHarness().supported_providers() == frozenset({"anthropic", "openai"})


def test_gemini_cli_providers_google_only():
    from puffo_agent.agent.harness import GeminiCLIHarness
    assert GeminiCLIHarness().supported_providers() == frozenset({"google"})


def test_base_harness_declares_empty_provider_set():
    """Empty set forces concrete harnesses to opt in — the validation
    matrix rejects every provider, the safe fallback."""
    class MinimalHarness(Harness):
        def name(self) -> str:
            return "minimal"
    assert MinimalHarness().supported_providers() == frozenset()


def test_build_harness_accepts_gemini_cli():
    from puffo_agent.agent.harness import GeminiCLIHarness
    h = build_harness("gemini-cli")
    assert isinstance(h, GeminiCLIHarness)
    assert h.name() == "gemini-cli"


# ── _require_claude_code guard ───────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def _build_mcp_with_harness(harness: str, tmp_path=None):
    """Stand up a puffo_core MCP server locked to the given harness.
    KeyStore + MessageStore are real but empty — only the host-side
    tools (install_skill, refresh, etc.) wrapped by the guard are
    exercised here.
    """
    import os
    import tempfile

    from puffo_agent.agent.message_store import MessageStore
    from puffo_agent.crypto.http_client import PuffoCoreHttpClient
    from puffo_agent.crypto.keystore import KeyStore
    from puffo_agent.mcp.puffo_core_server import build_server

    workspace = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    keystore_dir = os.path.join(workspace, "keys")

    return build_server(
        slug="t",
        device_id="dev_t",
        server_url="http://localhost:3000",
        space_id="sp_t",
        keystore_dir=keystore_dir,
        workspace=workspace,
        agent_id="t",
        # Guard fires before any tool body runs, so the URL only needs
        # to parse.
        data_service_url="http://127.0.0.1:0",
        runtime_kind="cli-local",
        harness=harness,
    ), workspace


def _call_tool(server, tool_name, **kwargs):
    """Invoke a registered tool by name via the FastMCP tool manager,
    bypassing the stdio protocol."""
    tool = server._tool_manager._tools[tool_name]
    return _run(tool.fn(**kwargs))


def test_install_skill_blocked_under_hermes(tmp_path):
    server, _ = _build_mcp_with_harness("hermes")
    with pytest.raises(RuntimeError, match="only supported under the claude-code harness"):
        _call_tool(server, "install_skill", name="my-skill", content="body")


def test_install_skill_allowed_under_claude_code(tmp_path, monkeypatch):
    """Sanity: the guard doesn't false-positive on the normal case.
    Actual skill-write success is covered in test_agent_install.py.
    """
    monkeypatch.chdir(tmp_path)
    server, _ = _build_mcp_with_harness("claude-code", tmp_path=tmp_path)
    try:
        _call_tool(
            server, "install_skill",
            name="ok", content="# valid skill body",
        )
    except RuntimeError as exc:
        assert "claude-code harness" not in str(exc), (
            "claude-code harness should NOT be blocked by _require_claude_code"
        )


def test_refresh_blocked_under_hermes():
    server, _ = _build_mcp_with_harness("hermes")
    with pytest.raises(RuntimeError, match="only supported under the claude-code harness"):
        _call_tool(server, "refresh")


def test_install_mcp_server_blocked_under_hermes():
    server, _ = _build_mcp_with_harness("hermes")
    with pytest.raises(RuntimeError, match="only supported under the claude-code harness"):
        _call_tool(
            server, "install_mcp_server",
            name="test", command="npx", args=["-y", "@foo/bar"], env={},
        )


def test_uninstall_tools_blocked_under_hermes():
    server, _ = _build_mcp_with_harness("hermes")
    with pytest.raises(RuntimeError, match="only supported under the claude-code harness"):
        _call_tool(server, "uninstall_skill", name="x")
    with pytest.raises(RuntimeError, match="only supported under the claude-code harness"):
        _call_tool(server, "uninstall_mcp_server", name="x")


def test_list_tools_not_blocked_under_hermes():
    """list_skills / list_mcp_servers are read-only — no guard."""
    server, _ = _build_mcp_with_harness("hermes")
    result = _call_tool(server, "list_skills")
    assert isinstance(result, str)
    result = _call_tool(server, "list_mcp_servers")
    assert isinstance(result, str)


def test_harness_empty_means_backward_compat_not_blocked():
    """Old daemons without PUFFO_HARNESS must keep working — block
    would break agents mid-turn on upgrade."""
    server, _ = _build_mcp_with_harness("")
    # Refresh flag may fail elsewhere (no workspace), but the harness
    # guard must not fire.
    try:
        _call_tool(server, "refresh")
    except RuntimeError as exc:
        assert "only supported under" not in str(exc), (
            f"empty harness should not trigger the guard: {exc}"
        )


# ── Hermes subprocess helpers (parse / normalize / stitch) ───────────────────
#
# The docker adapter calls ``hermes chat --quiet -q ...`` per turn and
# parses stdout. Pure helpers tested here pin the shape assumptions.


def test_hermes_model_id_strips_claude_code_suffix():
    from puffo_agent.agent.adapters.docker_cli import _hermes_model_id
    # claude-code's [1m] context-window suffix is unknown to hermes.
    assert _hermes_model_id("claude-opus-4-6[1m]") == "anthropic/claude-opus-4-6"


def test_hermes_model_id_prepends_anthropic_prefix_when_missing():
    from puffo_agent.agent.adapters.docker_cli import _hermes_model_id
    assert _hermes_model_id("claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"


def test_hermes_model_id_keeps_explicit_provider_prefix():
    from puffo_agent.agent.adapters.docker_cli import _hermes_model_id
    assert _hermes_model_id("openrouter/anthropic/claude-opus-4-6") == \
        "openrouter/anthropic/claude-opus-4-6"


def test_hermes_model_id_empty_returns_default():
    from puffo_agent.agent.adapters.docker_cli import _hermes_model_id
    # Empty / missing -> sensible default so hermes always gets a
    # concrete --model.
    assert _hermes_model_id("").startswith("anthropic/")
    assert _hermes_model_id(None).startswith("anthropic/")  # type: ignore[arg-type]


def test_parse_hermes_reply_first_turn():
    from puffo_agent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "⚠️  Normalized model 'anthropic/claude-opus-4-6' to 'claude-opus-4-6' for \n"
        "anthropic.\n"
        "\n"
        "session_id: 20260422_214146_02b4d1\n"
        "🚀✨🎯"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert reply == "🚀✨🎯"
    assert session_id == "20260422_214146_02b4d1"


def test_parse_hermes_reply_resumed_turn():
    """--continue prepends a ``↻ Resumed session`` line; parser must
    still pick up the reply after session_id."""
    from puffo_agent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "⚠️  Normalized model 'anthropic/claude-opus-4-6' to 'claude-opus-4-6' for \n"
        "anthropic.\n"
        "↻ Resumed session 20260422_213753_5d42f9 (1 user message, 2 total messages)\n"
        "\n"
        "session_id: 20260422_213753_5d42f9\n"
        "Hello there, how are you?"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert reply == "Hello there, how are you?"
    assert session_id == "20260422_213753_5d42f9"


def test_parse_hermes_reply_multiline_body():
    """Multi-line replies preserve internal newlines."""
    from puffo_agent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "session_id: abc\n"
        "line one\n"
        "line two\n"
        "line three"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert reply == "line one\nline two\nline three"
    assert session_id == "abc"


def test_parse_hermes_reply_no_session_id_but_reply_present():
    """Some hermes invocations under ``--quiet`` emit no ``session_id:``
    line on fresh sessions; parser must still extract the reply.
    """
    from puffo_agent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "⚠️  Normalized model 'anthropic/claude-opus-4-6' to 'claude-opus-4-6' for \n"
        "anthropic.\n"
        "[SILENT]"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert reply == "[SILENT]"
    assert session_id == ""


def test_parse_hermes_reply_resumed_session_id_captured_without_session_id_line():
    """The ``↻ Resumed session <id>`` line alone is enough to capture
    session_id when no standalone ``session_id:`` line follows."""
    from puffo_agent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "⚠️  Normalized model 'anthropic/claude-opus-4-6' to 'claude-opus-4-6' for \n"
        "anthropic.\n"
        "↻ Resumed session 20260422_222809_425056 (1 user message, 2 total messages)\n"
        "你好 @han.dev！有什么我可以帮你的吗？😊"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert reply == "你好 @han.dev！有什么我可以帮你的吗？😊"
    assert session_id == "20260422_222809_425056"


def test_parse_hermes_reply_filters_banner_lines_narrowly():
    """Banner-tail filter (``^[a-z0-9-]+\\.$``) only matches a line
    that is one word + period (e.g. ``anthropic.``). Regular prose
    ending in a period is not eaten.
    """
    from puffo_agent.agent.adapters.docker_cli import _parse_hermes_reply
    stdout = (
        "⚠️  Normalized model 'x/y' to 'y' for \n"
        "anthropic.\n"
        "session_id: sid-123\n"
        "The answer is 42.\n"
        "Further context: hermes.\n"
        "- bullet point\n"
        "- another"
    )
    reply, session_id = _parse_hermes_reply(stdout)
    assert session_id == "sid-123"
    assert "The answer is 42." in reply
    assert "Further context: hermes." in reply
    assert "- bullet point" in reply
    assert "- another" in reply
    assert "anthropic." not in reply


def test_stitch_hermes_prompt_first_turn():
    """Hermes has no --system flag for ``chat -q``; system prompt is
    inlined above the user message with a visible separator."""
    from puffo_agent.agent.adapters.docker_cli import _stitch_hermes_prompt
    stitched = _stitch_hermes_prompt("You are Puffo.", "hello")
    assert stitched == "You are Puffo.\n\n---\n\nhello"


def test_stitch_hermes_prompt_no_system_passes_through():
    """Empty system prompt -> user_message through unchanged, no stray
    separator at the top."""
    from puffo_agent.agent.adapters.docker_cli import _stitch_hermes_prompt
    assert _stitch_hermes_prompt("", "hello") == "hello"
    assert _stitch_hermes_prompt(None, "hello") == "hello"  # type: ignore[arg-type]


# ── cli-local rejects harness=hermes ─────────────────────────────────────────


def test_local_cli_rejects_hermes_harness():
    """cli-local doesn't support hermes yet; reject at construction so
    the daemon log shows a clear error rather than silently misbehaving.
    """
    from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter
    with pytest.raises(RuntimeError, match="not.+supported.+cli-local"):
        LocalCLIAdapter(
            agent_id="t",
            model="",
            workspace_dir="/tmp/ws",
            claude_dir="/tmp/ws/.claude",
            session_file="/tmp/sess.json",
            mcp_config_file="/tmp/mcp.json",
            agent_home_dir="/tmp/agent",
            harness=HermesHarness(),
        )


def test_local_cli_accepts_claude_code_harness():
    """Sanity: constructor's harness check doesn't false-positive on
    the default case."""
    from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter
    LocalCLIAdapter(
        agent_id="t",
        model="",
        workspace_dir="/tmp/ws",
        claude_dir="/tmp/ws/.claude",
        session_file="/tmp/sess.json",
        mcp_config_file="/tmp/mcp.json",
        agent_home_dir="/tmp/agent",
        harness=ClaudeCodeHarness(),
    )


def test_local_cli_rejects_gemini_cli_harness():
    """gemini-cli has the same cli-local limitation as hermes (operator's
    ``~/.gemini/`` may hold personal sessions). Same error shape."""
    from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter
    from puffo_agent.agent.harness import GeminiCLIHarness
    with pytest.raises(RuntimeError, match="not.+supported.+cli-local"):
        LocalCLIAdapter(
            agent_id="t",
            model="",
            workspace_dir="/tmp/ws",
            claude_dir="/tmp/ws/.claude",
            session_file="/tmp/sess.json",
            mcp_config_file="/tmp/mcp.json",
            agent_home_dir="/tmp/agent",
            harness=GeminiCLIHarness(),
        )


# ── Gemini CLI helpers (model-id + stdout parser) ────────────────────────────
#
# Pure helpers; subprocess path is covered in the smoke suite.


def test_gemini_model_id_default_when_empty():
    from puffo_agent.agent.adapters.docker_cli import _gemini_model_id
    assert _gemini_model_id("").startswith("gemini-")
    assert _gemini_model_id(None).startswith("gemini-")  # type: ignore[arg-type]


def test_gemini_model_id_passes_through_explicit_value():
    from puffo_agent.agent.adapters.docker_cli import _gemini_model_id
    assert _gemini_model_id("gemini-2.5-flash") == "gemini-2.5-flash"


def test_gemini_model_id_strips_claude_style_context_suffix():
    """Tolerate operators copy-pasting claude-style ids — strip the
    ``[1m]`` 1M-context suffix instead of letting gemini reject it."""
    from puffo_agent.agent.adapters.docker_cli import _gemini_model_id
    assert _gemini_model_id("gemini-2.5-pro[1m]") == "gemini-2.5-pro"


def test_parse_gemini_reply_happy_path():
    from puffo_agent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = '{"response": "hello from gemini", "stats": {"input_tokens": 5}}'
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == "hello from gemini"
    assert session_id == ""
    assert err == ""


def test_parse_gemini_reply_captures_session_id_at_top_level():
    """Gemini 0.38.2 puts ``session_id`` at the JSON top level, not
    inside ``stats``."""
    from puffo_agent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = (
        '{"session_id": "d21ddcdd-b12b-4579-9905-9dd0c26beb95", '
        '"response": "OK", "stats": {"models": {}}}'
    )
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == "OK"
    assert session_id == "d21ddcdd-b12b-4579-9905-9dd0c26beb95"


def test_parse_gemini_reply_extracts_message_from_error_object():
    """Structured failures: parser surfaces the inner ``error.message``
    string, not the stringified dict."""
    from puffo_agent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = (
        '{"session_id": "abc", "error": {"type": "Error", '
        '"message": "You have exhausted your daily quota on this model.", '
        '"code": 1}}'
    )
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == ""
    assert session_id == "abc"
    assert err == "You have exhausted your daily quota on this model."


def test_parse_gemini_reply_flags_usage_banner_as_malformed_argv():
    """If gemini prints its ``Usage:`` help banner instead of JSON,
    argv was malformed. Surface as an error string, never as a reply.
    """
    from puffo_agent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = (
        "Usage: gemini [options] [command]\n\n"
        "Gemini CLI - Defaults to interactive mode...\n"
        "Commands:\n  gemini mcp    Manage MCP servers\n"
    )
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == ""
    assert session_id == ""
    assert "argv" in err.lower() and "malformed" in err.lower()


def test_parse_gemini_reply_falls_back_to_raw_on_json_error():
    """Plain-text upstream failures (despite ``--output-format json``):
    return raw stdout so callers still have something to log."""
    from puffo_agent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = "ERROR: invalid API key"
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == "ERROR: invalid API key"
    assert session_id == ""
    assert err == ""


def test_parse_gemini_reply_empty_stdout_returns_empty():
    from puffo_agent.agent.adapters.docker_cli import _parse_gemini_reply
    assert _parse_gemini_reply("") == ("", "", "")
    assert _parse_gemini_reply("   \n  ") == ("", "", "")


def test_parse_gemini_reply_tolerates_missing_response_field():
    """Well-formed JSON without ``response`` returns empty rather than
    crashing."""
    from puffo_agent.agent.adapters.docker_cli import _parse_gemini_reply
    stdout = '{"stats": {"tokens": 10}}'
    reply, session_id, err = _parse_gemini_reply(stdout)
    assert reply == ""
    assert err == ""


# ── _build_gemini_argv — argv invariants ─────────────────────────────────────
#
# Preamble lines (built by PuffoAgent._append_user) start with ``- ``
# markdown list syntax. Passed as a separate argv after ``-p`` yargs
# treats it as another flag and gemini prints its --help banner.


def test_build_gemini_argv_uses_prompt_equals_form_not_dash_p():
    """Load-bearing: prompt goes in as one ``--prompt=<msg>`` token,
    not two ``-p <msg>`` tokens. ``=``-joined form tells yargs
    everything after ``=`` is the value, so a leading ``-`` in the
    value doesn't get eaten as another flag."""
    from puffo_agent.agent.adapters.docker_cli import _build_gemini_argv
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-test",
        model="gemini-2.5-flash",
        has_prior_session=False,
        user_message="- message: hello",
    )
    # Prompt must be ONE argv token, `=`-joined.
    assert "--prompt=- message: hello" in argv
    # Bare ``-p`` + separate value form must NOT appear.
    assert "-p" not in argv
    prompt_tokens = [a for a in argv if a.startswith("--prompt=")]
    assert len(prompt_tokens) == 1
    assert prompt_tokens[0] == "--prompt=- message: hello"


def test_build_gemini_argv_preserves_multi_line_cjk_prompt():
    """Multi-line CJK + markdown list preamble must survive untouched
    as a single argv element."""
    from puffo_agent.agent.adapters.docker_cli import _build_gemini_argv
    msg = (
        "- channel: @han.dev\n"
        "- thread_root_id: k87yuaun7p8o8yis8jxuddojse\n"
        "- message: 测试"
    )
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-test",
        model="",
        has_prior_session=False,
        user_message=msg,
    )
    assert f"--prompt={msg}" in argv


def test_build_gemini_argv_includes_resume_flag_when_session_exists():
    from puffo_agent.agent.adapters.docker_cli import _build_gemini_argv
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-test",
        model="gemini-2.5-flash",
        has_prior_session=True,
        user_message="hi",
    )
    assert "-r" in argv
    # ``latest`` must come right after ``-r``.
    i = argv.index("-r")
    assert argv[i + 1] == "latest"


def test_build_gemini_argv_omits_resume_for_fresh_session():
    from puffo_agent.agent.adapters.docker_cli import _build_gemini_argv
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-test",
        model="gemini-2.5-flash",
        has_prior_session=False,
        user_message="hi",
    )
    assert "-r" not in argv
    assert "latest" not in argv


def test_build_gemini_argv_skips_model_flag_when_empty():
    """Empty model -> no ``--model`` flag, so gemini uses the
    container default."""
    from puffo_agent.agent.adapters.docker_cli import _build_gemini_argv
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-test",
        model="",
        has_prior_session=False,
        user_message="hi",
    )
    assert "--model" not in argv


def test_build_gemini_argv_passes_api_key_via_docker_exec_e():
    """GEMINI_API_KEY flows through ``docker exec -e`` (container env),
    never through the host's environment — scopes the key to one
    invocation."""
    from puffo_agent.agent.adapters.docker_cli import _build_gemini_argv
    argv = _build_gemini_argv(
        container_name="puffo-abc",
        api_key="sk-ant-xyz",
        model="",
        has_prior_session=False,
        user_message="hi",
    )
    assert "docker" in argv and "exec" in argv
    e_idx = argv.index("-e")
    assert argv[e_idx + 1] == "GEMINI_API_KEY=sk-ant-xyz"
