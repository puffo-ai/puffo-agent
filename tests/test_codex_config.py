"""Phase 4 tests — codex config.toml writer.

Tomllib is stdlib in Python 3.11+ so we round-trip what we wrote to
make sure the hand-rolled emitter actually produces valid TOML and
preserves every field codex looks at (command / args / env).
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.mcp.config import write_codex_mcp_config


def _read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_round_trip_full_doc(tmp_path):
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        command="/usr/bin/python3",
        args=["-m", "puffo_agent.mcp.puffo_core_server"],
        env={
            "PUFFO_CORE_SLUG": "alice-noun-abcd",
            "PUFFO_WORKSPACE": "/tmp/work",
            "PYTHONUSERBASE": "/Users/op/.local",
        },
    )
    doc = _read_toml(dest)
    servers = doc.get("mcp_servers") or {}
    assert "puffo" in servers
    puffo = servers["puffo"]
    assert puffo["command"] == "/usr/bin/python3"
    assert puffo["args"] == ["-m", "puffo_agent.mcp.puffo_core_server"]
    assert puffo["env"]["PUFFO_CORE_SLUG"] == "alice-noun-abcd"
    assert puffo["env"]["PUFFO_WORKSPACE"] == "/tmp/work"
    assert puffo["env"]["PYTHONUSERBASE"] == "/Users/op/.local"


def test_env_omitted_when_empty(tmp_path):
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        command="/bin/echo",
        args=[],
        env={},
    )
    doc = _read_toml(dest)
    puffo = doc["mcp_servers"]["puffo"]
    # When env is empty we deliberately skip emitting the env table.
    assert "env" not in puffo


def test_paths_with_quotes_and_backslashes(tmp_path):
    """Windows paths + values containing quotes survive the round-trip
    without breaking TOML's basic-string escape rules."""
    dest = tmp_path / "config.toml"
    write_codex_mcp_config(
        dest,
        command=r"C:\Users\op\AppData\python.exe",
        args=["-m", "x"],
        env={
            "WEIRD": 'a "quoted" value',
            "WIN_PATH": r"C:\Users\op",
        },
    )
    doc = _read_toml(dest)
    puffo = doc["mcp_servers"]["puffo"]
    assert puffo["command"] == r"C:\Users\op\AppData\python.exe"
    assert puffo["env"]["WEIRD"] == 'a "quoted" value'
    assert puffo["env"]["WIN_PATH"] == r"C:\Users\op"
