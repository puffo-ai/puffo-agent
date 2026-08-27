import pytest

from puffo_agent.agent.adapters import desired_install
from puffo_agent.mcp import host_tools
from puffo_agent.skill_ids import SKILL_ID_RE


def test_both_call_sites_share_one_object():
    assert host_tools.SKILL_ID_RE is SKILL_ID_RE
    assert desired_install.SKILL_ID_RE is SKILL_ID_RE


@pytest.mark.parametrize(
    "ok",
    [
        "interview-me",
        "planning-and-task-breakdown",
        "continuous-learning-v2",
        "benchmark",
        "a",
        "0",
        "a" * 64,
    ],
)
def test_accepts_catalog_ids(ok):
    assert SKILL_ID_RE.match(ok)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "-leading-hyphen",
        "WithCaps",
        "under_score",
        "with.dot",
        "with space",
        "../etc/passwd",
        "a" * 65,
        "ok-id\n",
        "ok\nevil",
    ],
)
def test_rejects_everything_else(bad):
    assert not SKILL_ID_RE.match(bad)


def test_the_end_anchor_is_newline_tight():
    """``$`` would accept this; the SQL CHECK in migration 038 does not."""
    assert not SKILL_ID_RE.match("interview-me\n")


def test_pattern_matches_the_sql_check_verbatim():
    assert SKILL_ID_RE.pattern == r"^[a-z0-9][a-z0-9-]{0,63}\Z", (
        "charset drifted from the CHECK in puffo-server migration 038"
    )
