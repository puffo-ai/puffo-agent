"""The worker regenerates the managed briefing for ITS identity at start.

`sync_profile_briefing` existed, was tested, and was called by nothing. Memory
is seeded byte-for-byte from another agent, so a seeded briefing named the
source — the first live demo agent concluded it was the agent it came from.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from puffo_agent.agent.memory import PROFILE_MANAGED_BEGIN, PROFILE_MANAGED_END
from puffo_agent.portal.worker_run import refresh_managed_briefing


def _cfg(agent_id="optionexpe-6553-44cdb158", slug="optionexpe-6553-44cdb158"):
    return SimpleNamespace(
        id=agent_id, display_name="OptionExpert-cloud", role="Options specialist",
        role_short="OptionExpert", puffo_core=SimpleNamespace(slug=slug),
    )


def _seeded_briefing(root: Path, source_id: str, addendum: str) -> Path:
    b = root / "briefing"; b.mkdir(parents=True)
    p = b / "profile.md"
    p.write_text(
        f"{PROFILE_MANAGED_BEGIN}\n# Old\nYou are Old (agent {source_id}).\n{PROFILE_MANAGED_END}\n\n{addendum}\n",
        encoding="utf-8",
    )
    return p


def test_a_seeded_briefing_is_rewritten_for_the_real_agent(tmp_path):
    mem = tmp_path / "memory"; prof = tmp_path / "profile.md"
    prof.write_text("# Agent\n\n# Soul\n\nrigorous options math\n")
    p = _seeded_briefing(mem, "optionexpe-9840-999e4666", "My own standing note.")
    assert refresh_managed_briefing(_cfg(), str(mem), str(prof)) == "ok"
    text = p.read_text()
    assert "optionexpe-9840-999e4666" not in text, "the source id must not survive a start"
    assert "optionexpe-6553-44cdb158" in text
    assert "rigorous options math" in text, "soul comes from the store-owned profile"


def test_user_text_outside_the_markers_survives(tmp_path):
    mem = tmp_path / "memory"; prof = tmp_path / "profile.md"; prof.write_text("# Soul\nx\n")
    p = _seeded_briefing(mem, "someone-else-1234-abcdef12", "My own standing note.")
    refresh_managed_briefing(_cfg(), str(mem), str(prof))
    assert "My own standing note." in p.read_text()


def test_an_agent_with_no_briefing_gets_one(tmp_path):
    """The cloud agent booted with 0 memory files — nothing wrote a briefing."""
    mem = tmp_path / "memory"; prof = tmp_path / "profile.md"; prof.write_text("# Soul\nx\n")
    assert refresh_managed_briefing(_cfg(), str(mem), str(prof)) == "ok"
    assert "optionexpe-6553-44cdb158" in (mem / "briefing" / "profile.md").read_text()


def test_a_missing_profile_is_not_fatal(tmp_path):
    mem = tmp_path / "memory"
    assert refresh_managed_briefing(_cfg(), str(mem), str(tmp_path / "absent.md")) == "ok"
