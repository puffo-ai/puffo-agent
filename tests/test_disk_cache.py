"""Round-trip + edge cases for ``puffo_agent.agent.disk_cache``."""
from __future__ import annotations

import json
import os

import pytest

from puffo_agent.agent import disk_cache


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))


def test_persist_load_profile_round_trip():
    disk_cache.persist_profile(
        "alice-0001", "Alice", "https://chat.puffo.ai/relay/blobs/b_abc",
    )
    got = disk_cache.load_profile("alice-0001")
    assert got is not None
    assert got["slug"] == "alice-0001"
    assert got["display_name"] == "Alice"
    assert got["avatar_url"].endswith("/b_abc")
    assert isinstance(got["fetched_at"], int)


def test_persist_profile_no_op_on_empty_slug():
    disk_cache.persist_profile("", "X", "")
    assert disk_cache.load_profile("") is None


def test_persist_space_and_channel():
    disk_cache.persist_space("sp_1", "Demo Space")
    disk_cache.persist_channel("ch_1", "general", "sp_1")
    space = disk_cache.load_space("sp_1")
    channel = disk_cache.load_channel("ch_1")
    assert space and space["name"] == "Demo Space"
    assert channel and channel["name"] == "general"
    assert channel["space_id"] == "sp_1"


def test_load_all_returns_dict_keyed_by_id():
    disk_cache.persist_space("sp_a", "A")
    disk_cache.persist_space("sp_b", "B")
    all_spaces = disk_cache.load_all_spaces()
    assert set(all_spaces.keys()) == {"sp_a", "sp_b"}
    assert all_spaces["sp_a"]["name"] == "A"


def test_persist_space_skips_blank_name():
    disk_cache.persist_space("sp_x", "")
    assert disk_cache.load_space("sp_x") is None


def test_safe_filename_sanitises_separator_characters(tmp_path):
    disk_cache.persist_profile("alice/0001", "Alice", "")
    files = list((tmp_path / "cache" / "profiles").iterdir())
    assert len(files) == 1
    assert "/" not in files[0].name and "\\" not in files[0].name


def test_avatar_cache_path_round_trip_through_bytes():
    url = "https://chat.puffo.ai/relay/blobs/b_test.png"
    body = b"PNGFAKE\0\1\2"
    disk_cache.write_avatar_bytes(url, body)
    path = disk_cache.avatar_cache_path(url)
    assert path.exists()
    assert path.read_bytes() == body
    # Same URL → identical path; second call overwrites cleanly.
    disk_cache.write_avatar_bytes(url, b"second")
    assert path.read_bytes() == b"second"


def test_avatar_cache_path_unknown_extension_falls_back_to_img():
    url = "https://chat.puffo.ai/relay/blobs/b_xyz"
    path = disk_cache.avatar_cache_path(url)
    assert path.suffix == ".img"


def test_load_all_skips_malformed_files(tmp_path):
    disk_cache.persist_profile("ok-1", "OK", "")
    (tmp_path / "cache" / "profiles" / "broken.json").write_text("{not json")
    all_profiles = disk_cache.load_all_profiles()
    assert "ok-1" in all_profiles
    assert "broken" not in all_profiles
