"""PUF-409: per-agent auto-compact threshold + context telemetry.

The threshold maths mirrors claude-code v2.1.199's own implementation:

    threshold = min(floor(window * pct/100), window - 13000)   for 0 < pct <= 100
    threshold = window - 13000                                 otherwise

so these tests pin the two behaviours that are easy to get wrong: a pct of
100 collapses onto the default (it does NOT disable auto-compact), and
out-of-range values are ignored by the CLI rather than applied.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _bridge_support import isolated_home, write_test_agent  # noqa: E402

from puffo_agent.portal.control.context_telemetry import (  # noqa: E402
    COMPACT_HEADROOM_TOKENS,
    build_context_telemetry,
    compact_threshold_tokens,
    parse_threshold_pct,
    resolve_context_window,
)
from puffo_agent.portal.state import (  # noqa: E402
    AgentConfig,
    validate_env_overrides,
)


# ── threshold maths ──────────────────────────────────────────────


def test_default_threshold_is_window_minus_headroom():
    # Not a flat 95%: it's an absolute offset, so the percentage it works
    # out to depends on the window size.
    assert compact_threshold_tokens(200_000, None) == 187_000
    assert compact_threshold_tokens(1_000_000, None) == 987_000


@pytest.mark.parametrize("pct,expected", [(50, 100_000), (30, 60_000), (75, 150_000)])
def test_pct_override_scales_the_window(pct, expected):
    assert compact_threshold_tokens(200_000, pct) == expected


def test_pct_100_collapses_onto_the_default_and_does_not_disable():
    # The `min(..., window - headroom)` in claude-code means 100 can never
    # push the threshold above the default — "100" is not an off switch.
    window = 200_000
    assert compact_threshold_tokens(window, 100) == compact_threshold_tokens(window, None)
    assert compact_threshold_tokens(window, 100) == window - COMPACT_HEADROOM_TOKENS


@pytest.mark.parametrize("raw", ["0", "-5", "101", "abc", "", None])
def test_out_of_range_pct_reads_as_no_override(raw):
    # claude-code silently ignores these, so we must not report them as active.
    assert parse_threshold_pct(raw) is None


@pytest.mark.parametrize("raw,expected", [("50", 50.0), ("30", 30.0), ("100", 100.0)])
def test_in_range_pct_parses(raw, expected):
    assert parse_threshold_pct(raw) == expected


# ── window resolution ────────────────────────────────────────────


def test_window_defaults_to_200k():
    assert resolve_context_window("claude-sonnet-5", env={}) == 200_000


def test_window_env_override_wins():
    assert resolve_context_window("claude-sonnet-5", env={
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000",
    }) == 500_000


def test_extended_context_model_detected():
    assert resolve_context_window("claude-sonnet-4-5[1m]", env={}) == 1_000_000


# ── telemetry payload ────────────────────────────────────────────


def test_payload_reports_default_when_no_override():
    out = build_context_telemetry(
        model="claude-sonnet-5", current_context_tokens=138_000, env={},
    )
    assert out["max_context"] == 200_000
    assert out["current_context"] == 138_000
    assert out["auto_compact_threshold"] == 187_000
    assert out["auto_compact_threshold_pct"] is None
    assert out["threshold_is_default"] is True
    assert out["used_pct"] == 69.0


def test_payload_reports_active_override():
    out = build_context_telemetry(
        model="claude-sonnet-5",
        current_context_tokens=50_000,
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"},
        env={},
    )
    assert out["auto_compact_threshold"] == 100_000
    assert out["auto_compact_threshold_pct"] == 50.0
    assert out["threshold_is_default"] is False


def test_payload_marks_100_as_default_not_a_distinct_setting():
    # An operator selecting 100 must not be shown a threshold that differs
    # from the default, because claude-code won't apply one.
    out = build_context_telemetry(
        model="claude-sonnet-5",
        current_context_tokens=10,
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "100"},
        env={},
    )
    assert out["threshold_is_default"] is True
    assert out["auto_compact_threshold"] == 187_000


# ── env_overrides validation ─────────────────────────────────────


def test_validate_accepts_whitelisted_key():
    assert validate_env_overrides(
        {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}
    ) == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}


def test_validate_rejects_unknown_key():
    with pytest.raises(ValueError, match="not allowed"):
        validate_env_overrides({"PATH": "/tmp/evil"})


@pytest.mark.parametrize("bad", ["0", "-1", "101", "abc"])
def test_validate_rejects_values_claude_code_would_ignore(bad):
    with pytest.raises(ValueError):
        validate_env_overrides({"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": bad})


def test_validate_allows_empty_string_to_clear():
    assert validate_env_overrides(
        {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": ""}
    ) == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": ""}


def test_validate_normalises_numeric_input():
    # yaml/json may hand us an int; env values must be strings.
    assert validate_env_overrides(
        {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": 70}
    ) == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}


def test_validate_rejects_non_mapping():
    with pytest.raises(ValueError, match="must be an object"):
        validate_env_overrides(["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50"])


# ── config round-trip ────────────────────────────────────────────


def test_env_overrides_round_trip_through_agent_yml():
    home = isolated_home()
    write_test_agent(home, "ctx-bot")
    cfg = AgentConfig.load("ctx-bot")
    assert cfg.env_overrides == {}

    cfg.env_overrides = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}
    cfg.save()

    assert AgentConfig.load("ctx-bot").env_overrides == {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50",
    }


def test_yaml_int_value_loads_as_string():
    # An operator hand-editing agent.yml writes `70`, not `"70"`; it still
    # has to reach the subprocess env as a str.
    home = isolated_home()
    write_test_agent(home, "ctx-bot")
    path = AgentConfig.load("ctx-bot")
    path.env_overrides = {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}
    path.save()
    yml = os.path.join(home, "agents", "ctx-bot", "agent.yml")
    with open(yml, encoding="utf-8") as fh:
        body = fh.read().replace(
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: '70'",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: 70",
        )
    with open(yml, "w", encoding="utf-8") as fh:
        fh.write(body)

    loaded = AgentConfig.load("ctx-bot").env_overrides
    assert loaded == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70"}
    assert isinstance(loaded["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], str)


# ── spawn-env precedence ─────────────────────────────────────────


def test_overrides_layer_over_os_environ_but_under_adapter_owned_vars():
    from puffo_agent.agent.adapters.local_cli import LocalCLIAdapter

    home = isolated_home()
    write_test_agent(home, "ctx-bot")
    adapter = LocalCLIAdapter(
        agent_id="ctx-bot",
        model="claude-sonnet-5",
        workspace_dir=os.path.join(home, "ws"),
        claude_dir=os.path.join(home, "claude"),
        session_file=os.path.join(home, "s.json"),
        mcp_config_file=os.path.join(home, "mcp.json"),
        agent_home_dir=os.path.join(home, "agent-home"),
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"},
    )
    # Stringified on the way in so a yaml int can't reach subprocess env.
    assert adapter.env_overrides == {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"}

    env = {
        **os.environ,
        **adapter.env_overrides,
        "HOME": str(adapter.agent_home_dir),
    }
    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "50"
    # HOME is adapter-owned and must survive any override attempt.
    assert env["HOME"] == str(adapter.agent_home_dir)


# ── override-unhonored detector (PUF-409, Vase risk-accept condition) ──


def test_override_honored_while_context_is_below_threshold():
    out = build_context_telemetry(
        model="claude-sonnet-5",
        current_context_tokens=99_000,
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"},
        env={},
    )
    assert out["override_unhonored"] is False
    assert out["auto_compact_threshold"] == 100_000


def test_one_turn_at_the_threshold_is_not_treated_as_unhonored():
    # The turn that trips auto-compact necessarily reaches the threshold.
    out = build_context_telemetry(
        model="claude-sonnet-5",
        current_context_tokens=105_000,
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"},
        env={},
    )
    assert out["override_unhonored"] is False


def test_context_sailing_past_the_threshold_reports_unhonored():
    # 50% of 200K = 100K; a turn at 160K means no compact happened, so the
    # knob is not being applied by this claude-code build.
    out = build_context_telemetry(
        model="claude-sonnet-5",
        current_context_tokens=160_000,
        env_overrides={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"},
        env={},
    )
    assert out["override_unhonored"] is True
    # Must report observed behaviour, not the setting we asked for.
    assert out["auto_compact_threshold"] == 187_000
    assert out["auto_compact_threshold_pct"] is None
    assert out["threshold_is_default"] is True


def test_no_override_never_reports_unhonored():
    out = build_context_telemetry(
        model="claude-sonnet-5", current_context_tokens=199_000, env={},
    )
    assert out["override_unhonored"] is False
