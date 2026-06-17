"""``rewrite_profile_name`` helper unit coverage.

Sync tests live in their own module so they don't pick up
``test_bridge_handlers.py``'s file-level ``pytest.mark.asyncio`` mark.
"""

from __future__ import annotations

from puffo_agent.agent.shared_content import rewrite_profile_name


def test_rewrite_returns_replacement_count(tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text("Bob is Bob, the helper. Bob loves Bob.\n", encoding="utf-8")
    n = rewrite_profile_name(profile, "Bob", "Robert")
    assert n == 4
    assert profile.read_text(encoding="utf-8") == (
        "Robert is Robert, the helper. Robert loves Robert.\n"
    )


def test_rewrite_no_op_when_old_equals_new(tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text("Bob is Bob.\n", encoding="utf-8")
    mtime_before = profile.stat().st_mtime_ns
    n = rewrite_profile_name(profile, "Bob", "Bob")
    assert n == 0
    assert profile.stat().st_mtime_ns == mtime_before


def test_rewrite_missing_file_returns_zero(tmp_path):
    n = rewrite_profile_name(tmp_path / "does-not-exist.md", "Bob", "Robert")
    assert n == 0


def test_rewrite_no_match_does_not_touch_file(tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text("Helpful agent. No name embedded.\n", encoding="utf-8")
    mtime_before = profile.stat().st_mtime_ns
    n = rewrite_profile_name(profile, "Bob", "Robert")
    assert n == 0
    assert profile.stat().st_mtime_ns == mtime_before


def test_rewrite_empty_names_are_noops(tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text("Bob is here.\n", encoding="utf-8")
    assert rewrite_profile_name(profile, "", "Robert") == 0
    assert rewrite_profile_name(profile, "Bob", "") == 0
    assert profile.read_text(encoding="utf-8") == "Bob is here.\n"


def test_rewrite_handles_cjk(tmp_path):
    # The ASCII-only boundary doesn't block CJK characters, so CJK
    # display names still match (``\b`` never would).
    profile = tmp_path / "profile.md"
    profile.write_text("你是田中。田中负责安排。\n", encoding="utf-8")
    n = rewrite_profile_name(profile, "田中", "山田")
    assert n == 2
    assert profile.read_text(encoding="utf-8") == "你是山田。山田负责安排。\n"


def test_rewrite_does_not_overreach_into_longer_ascii_words(tmp_path):
    # Standalone + possessive match; "Bob" inside "Bobcats" does not.
    profile = tmp_path / "profile.md"
    profile.write_text("Bob watches Bobcats; Bob's cabin.\n", encoding="utf-8")
    n = rewrite_profile_name(profile, "Bob", "Robert")
    assert n == 2
    assert profile.read_text(encoding="utf-8") == (
        "Robert watches Bobcats; Robert's cabin.\n"
    )
