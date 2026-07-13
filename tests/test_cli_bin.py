"""Unit tests for ``puffo_agent.agent.cli_bin``."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from puffo_agent.agent import cli_bin


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Isolate the on-disk cache + neutralize the (subprocess) real-PATH
    # reconstruction so unit tests never fork PowerShell / a login shell.
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli_bin, "_real_path", lambda: "")
    cli_bin._resolve_memcache.clear()
    monkeypatch.setattr(cli_bin, "_real_path_cache", None)
    yield
    cli_bin._resolve_memcache.clear()


def _make_exe(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


def test_env_override_wins(tmp_path, monkeypatch):
    """``$PUFFO_CODEX_BIN`` beats PATH + bundle paths."""
    fake = _make_exe(tmp_path, "fake_codex")
    monkeypatch.setenv("PUFFO_CODEX_BIN", str(fake))
    monkeypatch.setattr("shutil.which", lambda name, path=None: "/some/other/codex")
    assert cli_bin.resolve_codex_bin() == str(fake)


def test_env_override_ignored_when_file_missing(tmp_path, monkeypatch):
    """A pointer to a non-existent file is treated as "not set" and
    falls through to PATH — protects against stale env vars."""
    bogus = tmp_path / "nope" / "codex"
    monkeypatch.setenv("PUFFO_CODEX_BIN", str(bogus))
    monkeypatch.setattr("shutil.which", lambda name, path=None: "/from/path/codex")
    assert cli_bin.resolve_codex_bin() == "/from/path/codex"


def test_path_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("PUFFO_CODEX_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name, path=None: "/usr/local/bin/codex")
    assert cli_bin.resolve_codex_bin() == "/usr/local/bin/codex"


def test_bundle_path_hit_when_env_and_path_miss(tmp_path, monkeypatch):
    """The Mac LaunchAgent case: PATH misses Codex.app, but the
    bundle path resolves the binary."""
    bundled = _make_exe(tmp_path, "codex_bundled")
    monkeypatch.delenv("PUFFO_CODEX_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    monkeypatch.setattr(cli_bin, "_codex_bundle_paths", lambda: [bundled])
    assert cli_bin.resolve_codex_bin() == str(bundled)


def test_returns_none_on_full_miss(monkeypatch):
    monkeypatch.delenv("PUFFO_CODEX_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    monkeypatch.setattr(cli_bin, "_codex_bundle_paths", lambda: [])
    assert cli_bin.resolve_codex_bin() is None


def test_claude_resolver_uses_its_own_env(tmp_path, monkeypatch):
    """``resolve_claude_bin`` reads ``PUFFO_CLAUDE_BIN``, not
    ``PUFFO_CODEX_BIN`` — make sure the namespacing is correct."""
    fake_claude = _make_exe(tmp_path, "fake_claude")
    monkeypatch.setenv("PUFFO_CLAUDE_BIN", str(fake_claude))
    monkeypatch.setenv("PUFFO_CODEX_BIN", "/should/not/leak")
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    monkeypatch.setattr(cli_bin, "_claude_bundle_paths", lambda: [])
    assert cli_bin.resolve_claude_bin() == str(fake_claude)


@pytest.mark.parametrize("platform_value,want_first", [
    ("darwin", "/Applications/ChatGPT.app/Contents/Resources/codex"),
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


def test_darwin_bundle_paths_include_chatgpt_app(monkeypatch):
    monkeypatch.setattr(cli_bin.sys, "platform", "darwin")
    paths = [p.as_posix() for p in cli_bin._codex_bundle_paths()]
    assert "/Applications/ChatGPT.app/Contents/Resources/codex" in paths
    assert any(
        p.endswith("Applications/ChatGPT.app/Contents/Resources/codex")
        and p != "/Applications/ChatGPT.app/Contents/Resources/codex"
        for p in paths
    ), "expected the ~/Applications ChatGPT.app path too"
    # ChatGPT.app preferred over a leftover Codex.app copy — _first_existing
    # takes the first hit.
    assert paths.index("/Applications/ChatGPT.app/Contents/Resources/codex") < paths.index(
        "/Applications/Codex.app/Contents/Resources/codex"
    )


def test_hermes_env_override_wins(tmp_path, monkeypatch):
    """``$PUFFO_HERMES_BIN`` beats PATH + bundle paths."""
    fake = _make_exe(tmp_path, "fake_hermes")
    monkeypatch.setenv("PUFFO_HERMES_BIN", str(fake))
    monkeypatch.setattr("shutil.which", lambda name, path=None: "/some/other/hermes")
    assert cli_bin.resolve_hermes_bin() == str(fake)


def test_hermes_path_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("PUFFO_HERMES_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name, path=None: "/usr/local/bin/hermes")
    assert cli_bin.resolve_hermes_bin() == "/usr/local/bin/hermes"


def test_hermes_returns_none_on_full_miss(monkeypatch):
    monkeypatch.delenv("PUFFO_HERMES_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
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
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    monkeypatch.setattr(cli_bin, "_hermes_bundle_paths", lambda: [])
    assert cli_bin.resolve_hermes_bin() == str(fake_hermes)


def test_real_path_used_when_process_path_misses(monkeypatch):
    """When the process PATH misses, resolution retries against the
    reconstructed real PATH."""
    monkeypatch.delenv("PUFFO_CODEX_BIN", raising=False)
    monkeypatch.setattr(cli_bin, "_real_path", lambda: "/real/bin")
    monkeypatch.setattr(cli_bin, "_codex_bundle_paths", lambda: [])
    monkeypatch.setattr(
        "shutil.which",
        lambda name, path=None: "/real/bin/codex" if path == "/real/bin" else None,
    )
    assert cli_bin.resolve_codex_bin() == "/real/bin/codex"


def test_resolved_path_survives_restart_via_disk_cache(tmp_path, monkeypatch):
    """First resolve writes resolved_clis.json; a later resolve (fresh
    process, PATH now missing) reads it back instead of re-searching."""
    real = _make_exe(tmp_path, "codex_real")
    monkeypatch.delenv("PUFFO_CODEX_BIN", raising=False)
    monkeypatch.setattr(cli_bin, "_codex_bundle_paths", lambda: [])
    monkeypatch.setattr("shutil.which", lambda name, path=None: str(real))
    assert cli_bin.resolve_codex_bin() == str(real)

    # Simulate a restart: in-memory cache gone, binary no longer on PATH.
    cli_bin._resolve_memcache.clear()
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    assert cli_bin.resolve_codex_bin() == str(real)  # served from disk cache


def test_disk_cache_rejected_when_binary_gone(tmp_path, monkeypatch):
    """A cached path that no longer exists falls through to a fresh
    lookup instead of returning a dead path."""
    real = _make_exe(tmp_path, "codex_real")
    monkeypatch.delenv("PUFFO_CODEX_BIN", raising=False)
    monkeypatch.setattr(cli_bin, "_codex_bundle_paths", lambda: [])
    monkeypatch.setattr("shutil.which", lambda name, path=None: str(real))
    assert cli_bin.resolve_codex_bin() == str(real)

    real.unlink()  # uninstalled
    cli_bin._resolve_memcache.clear()
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    assert cli_bin.resolve_codex_bin() is None


def test_merge_path_dedups_and_drops_empties():
    sep = os.pathsep
    merged = cli_bin._merge_path(f"/a{sep}/b", f"/b{sep}{sep}/c")
    assert merged.split(sep) == ["/a", "/b", "/c"]


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


# ── credential presence (UI 3-state status) ──────────────────────────


def test_codex_has_credentials_true_when_auth_file_exists(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    assert cli_bin.codex_has_credentials(home=tmp_path) is True


def test_codex_has_credentials_false_when_dir_missing(tmp_path):
    assert cli_bin.codex_has_credentials(home=tmp_path) is False


def test_codex_has_credentials_false_when_file_missing_but_dir_exists(tmp_path):
    (tmp_path / ".codex").mkdir()
    assert cli_bin.codex_has_credentials(home=tmp_path) is False


def test_claude_has_credentials_true_when_file_exists(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    assert cli_bin.claude_has_credentials(home=tmp_path) is True


def test_claude_has_credentials_false_when_file_missing(tmp_path, monkeypatch):
    # On macOS we'd also probe Keychain; force the platform off so the
    # test runs identically across CI envs.
    monkeypatch.setattr("puffo_agent.agent.cli_bin.sys.platform", "linux")
    assert cli_bin.claude_has_credentials(home=tmp_path) is False


def test_claude_has_credentials_macos_falls_back_to_keychain(tmp_path, monkeypatch):
    """When the file is missing on macOS, the Keychain probe (``security
    find-generic-password``) decides. rc=0 → True, rc!=0 → False."""
    import subprocess as _sp

    monkeypatch.setattr("puffo_agent.agent.cli_bin.sys.platform", "darwin")

    class _RC:
        def __init__(self, code):
            self.returncode = code

    monkeypatch.setattr(
        "puffo_agent.agent.cli_bin.subprocess.run",
        lambda *a, **kw: _RC(0),
    )
    assert cli_bin.claude_has_credentials(home=tmp_path) is True

    monkeypatch.setattr(
        "puffo_agent.agent.cli_bin.subprocess.run",
        lambda *a, **kw: _RC(44),
    )
    assert cli_bin.claude_has_credentials(home=tmp_path) is False


def test_claude_has_credentials_macos_checks_both_keychain_services(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("puffo_agent.agent.cli_bin.sys.platform", "darwin")

    class _RC:
        def __init__(self, code):
            self.returncode = code

    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        service = cmd[cmd.index("-s") + 1]
        calls.append(service)
        return _RC(0 if service == "Claude Code" else 44)

    monkeypatch.setattr("puffo_agent.agent.cli_bin.subprocess.run", fake_run)
    assert cli_bin.claude_has_credentials(home=tmp_path) is True
    assert calls == ["Claude Code-credentials", "Claude Code"]


def test_claude_has_credentials_keychain_probe_failure_treated_as_false(
    tmp_path, monkeypatch,
):
    """Timeout / subprocess error must NOT raise into the UI poll."""
    import subprocess as _sp

    monkeypatch.setattr("puffo_agent.agent.cli_bin.sys.platform", "darwin")

    def boom(*_a, **_kw):
        raise _sp.TimeoutExpired(cmd="security", timeout=2)

    monkeypatch.setattr("puffo_agent.agent.cli_bin.subprocess.run", boom)
    assert cli_bin.claude_has_credentials(home=tmp_path) is False
