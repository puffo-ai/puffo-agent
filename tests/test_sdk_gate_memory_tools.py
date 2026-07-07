"""The sdk-local adapter runs every tool call through ``_gate``, which
allows a call only if some configured pattern matches. Puffo MCP tools
are auto-allowed by seeding the gate's patterns with
``PUFFO_CORE_TOOL_FQNS``. This pins that the ten M3 memory tools are in
that set — otherwise they register on the server but get denied at call
time on sdk-local (the M3 finding this guards against).

``claude_agent_sdk`` is an optional dep the dev extra does NOT install;
``SDKAdapter.__init__`` defers that import, so the module (and
``_pattern_matches``) import fine without it, and the gate logic can be
pinned directly.
"""

from puffo_agent.agent.adapters.sdk import _pattern_matches
from puffo_agent.mcp.config import (
    MCP_SERVER_NAME,
    PUFFO_CORE_TOOL_FQNS,
    PUFFO_CORE_TOOL_NAMES,
)

M3_TOOL_NAMES = (
    "create_note",
    "patch_note",
    "append_note",
    "create_briefing_topic",
    "patch_briefing_topic",
    "append_recollection",
    "read_memory_file",
    "read_memory_files",
    "search_memory",
    "search_imports",
)


def test_m3_memory_tools_are_in_core_allowlist():
    assert set(M3_TOOL_NAMES) <= set(PUFFO_CORE_TOOL_NAMES)


def test_m3_memory_tool_fqns_pass_the_sdk_gate():
    # The adapter seeds its gate patterns with PUFFO_CORE_TOOL_FQNS
    # (see SDKAdapter.__init__ / _gate). Reproduce the exact allow
    # check the gate performs for each M3 tool's FQN.
    for name in M3_TOOL_NAMES:
        fqn = f"mcp__{MCP_SERVER_NAME}__{name}"
        allowed = any(
            _pattern_matches(fqn, {}, pat) for pat in PUFFO_CORE_TOOL_FQNS
        )
        assert allowed, f"{fqn} would be denied by the sdk gate"


def test_unknown_puffo_tool_is_not_auto_allowed():
    # Guard the guard: an FQN NOT in the allowlist must not match, so
    # the assertion above is meaningful rather than vacuously true.
    fqn = "mcp__puffo__definitely_not_a_real_tool"
    assert fqn not in PUFFO_CORE_TOOL_FQNS
    assert not any(
        _pattern_matches(fqn, {}, pat) for pat in PUFFO_CORE_TOOL_FQNS
    )
