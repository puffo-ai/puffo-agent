"""Unit tests for ``puffo_agent.agent.cli_bin``."""
from __future__ import annotations

from pathlib import Path

import pytest

from puffo_agent.agent import cli_bin


def _make_exe(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


def test_env_override_wins(tmp_path, monkeypatch):
    """``$PUFFO_CODEX_BIN`` beats PATH + bundle paths."""
    fake = _make_exe(tmp_path, "fake_codex")
    monkeypatch.setenv("PUFFO_CODEX_BIN", str(fake))
    monkeypatch.setattr("shutil.which", lambda _name: "/some/other/codex")
    assert cli_bin.resolve_codex_bin() == str(fake)


def test_env_override_ignored_when_file_missing(tmp_path, monkeypatch):
    """A pointer to a non-existent file is treated as "not set" and
    falls through to PATH — protects against stale env vars."""
    bogus = tmp_path / "nope" / "codex"
    monkeypatch.setenv("PUFFO_CODEX_BIN", str(bogus))
    monkeypatch.setattr("shutil.which", lambda _name: "/from/path/codex")
    assert cli_bin.resolve_codex_bin() == "/from/path/codex"


def test_path_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("PUFFO_CODEX_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/codex")
    assert cli_bin.resolve_codex_bin() == "/usr/local/bin/codex"


def test_bundle_path_hit_when_env_and_path_miss(tmp_path, monkeypatch):
    """The Mac LaunchAgent case: PATH misses Codex.app, but the
    bundle path resolves the binary."""
    bundled = _make_exe(tmp_path, "codex_bundled")
    monkeypatch.delenv("PUFFO_CODEX_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(cli_bin, "_codex_bundle_paths", lambda: [bundled])
    assert cli_bin.resolve_codex_bin() == str(bundled)


def test_returns_none_on_full_miss(monkeypatch):
    monkeypatch.delenv("PUFFO_CODEX_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(cli_bin, "_codex_bundle_paths", lambda: [])
    assert cli_bin.resolve_codex_bin() is None


def test_claude_resolver_uses_its_own_env(tmp_path, monkeypatch):
    """``resolve_claude_bin`` reads ``PUFFO_CLAUDE_BIN``, not
    ``PUFFO_CODEX_BIN`` — make sure the namespacing is correct."""
    fake_claude = _make_exe(tmp_path, "fake_claude")
    monkeypatch.setenv("PUFFO_CLAUDE_BIN", str(fake_claude))
    monkeypatch.setenv("PUFFO_CODEX_BIN", "/should/not/leak")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(cli_bin, "_claude_bundle_paths", lambda: [])
    assert cli_bin.resolve_claude_bin() == str(fake_claude)


@pytest.mark.parametrize("platform_value,want_first", [
    ("darwin", "/Applications/Codex.app/Contents/Resources/codex"),
    ("win32", "codex.exe"),  # contains-check
])
def test_bundle_paths_per_platform(platform_value, want_first, monkeypatch):
    """Each platform contributes the right first-priority bundle
    path — protects against accidental cross-platform regressions."""
    monkeypatch.setattr(cli_bin.sys, "platform", platform_value)
    paths = cli_bin._codex_bundle_paths()
    assert paths, f"no candidates for platform {platform_value!r}"
    if platform_value == "darwin":
        # Use as_posix() so the comparison works regardless of the
        # host OS running the test (pathlib normalises separators
        # to the host's, even on a literal POSIX-shaped input).
        assert paths[0].as_posix() == want_first
    else:
        assert want_first in str(paths[0]).lower()


def test_hermes_env_override_wins(tmp_path, monkeypatch):
    """``$PUFFO_HERMES_BIN`` beats PATH + bundle paths."""
    fake = _make_exe(tmp_path, "fake_hermes")
    monkeypatch.setenv("PUFFO_HERMES_BIN", str(fake))
    monkeypatch.setattr("shutil.which", lambda _name: "/some/other/hermes")
    assert cli_bin.resolve_hermes_bin() == str(fake)


def test_hermes_path_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("PUFFO_HERMES_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/hermes")
    assert cli_bin.resolve_hermes_bin() == "/usr/local/bin/hermes"


def test_hermes_returns_none_on_full_miss(monkeypatch):
    monkeypatch.delenv("PUFFO_HERMES_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(cli_bin, "_hermes_bundle_paths", lambda: [])
    assert cli_bin.resolve_hermes_bin() is None


def test_hermes_resolver_uses_its_own_env(tmp_path, monkeypatch):
    """``resolve_hermes_bin`` reads ``PUFFO_HERMES_BIN`` only — make
    sure namespace doesn't leak from the codex / claude env vars.
    """
    fake_hermes = _make_exe(tmp_path, "fake_hermes")
    monkeypatch.setenv("PUFFO_HERMES_BIN", str(fake_hermes))
    monkeypatch.setenv("PUFFO_CODEX_BIN", "/should/not/leak")
    monkeypatch.setenv("PUFFO_CLAUDE_BIN", "/should/not/leak")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(cli_bin, "_hermes_bundle_paths", lambda: [])
    assert cli_bin.resolve_hermes_bin() == str(fake_hermes)


@pytest.mark.parametrize("platform_value,want_substr", [
    ("darwin", ".local/bin/hermes"),
    ("linux", ".local/bin/hermes"),
    ("win32", "hermes"),
])
def test_hermes_bundle_paths_per_platform(platform_value, want_substr, monkeypatch):
    monkeypatch.setattr(cli_bin.sys, "platform", platform_value)
    paths = cli_bin._hermes_bundle_paths()
    assert paths, f"no candidates for platform {platform_value!r}"
    first = paths[0].as_posix() if platform_value != "win32" else str(paths[0])
    assert want_substr in first
