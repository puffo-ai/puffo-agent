"""Tests for ``sync_host_credentials_view`` — the per-agent credential
*view* plumbing for cli-local agents.

Model under test (replaces the pre-1.0.7a2 symlink sharing):

  * The agent's ``.credentials.json`` is a sanitized copy of the
    host's: full blob minus ``claudeAiOauth.refreshToken``. Only the
    daemon ever holds the (single-use, rotating) refresh token, so
    concurrent agent claude processes can't race a refresh into an
    Anthropic token-family revocation.
  * Idempotent: matching view content -> "view (fresh)", no rewrite.
  * Self-healing: agent-side drift (garbage, stale token) is
    overwritten from the host blob on the next sync.
  * Migration: a legacy symlink is replaced by a view file; the host
    file it pointed at is never modified.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.state import (
    sanitize_claude_credentials_blob,
    sync_host_credentials_view,
)


HOST_CREDS = {
    "claudeAiOauth": {
        "accessToken": "at-123",
        "refreshToken": "rt-secret-456",
        "expiresAt": 1900000000000,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }
}


def _symlinks_available(tmp_path: Path) -> bool:
    probe = tmp_path / "_probe_link"
    target = tmp_path / "_probe_target"
    target.write_text("x", encoding="utf-8")
    try:
        os.symlink(target, probe)
    except (OSError, NotImplementedError):
        target.unlink(missing_ok=True)
        return False
    probe.unlink()
    target.unlink()
    return True


def _write_host(host: Path, creds: dict | None = None) -> Path:
    host_creds = host / ".claude" / ".credentials.json"
    host_creds.parent.mkdir(parents=True, exist_ok=True)
    host_creds.write_text(json.dumps(creds or HOST_CREDS), encoding="utf-8")
    return host_creds


def _agent_view(agent: Path) -> Path:
    return agent / ".claude" / ".credentials.json"


# ── sanitizer ─────────────────────────────────────────────────


def test_sanitize_strips_refresh_token_only():
    view = json.loads(sanitize_claude_credentials_blob(json.dumps(HOST_CREDS)))
    oauth = view["claudeAiOauth"]
    assert "refreshToken" not in oauth
    assert oauth["accessToken"] == "at-123"
    assert oauth["expiresAt"] == 1900000000000
    assert oauth["scopes"] == ["user:inference"]


def test_sanitize_rejects_non_json():
    assert sanitize_claude_credentials_blob("not json {") is None


def test_sanitize_tolerates_missing_oauth_section():
    blob = json.dumps({"somethingElse": True})
    assert json.loads(sanitize_claude_credentials_blob(blob)) == {
        "somethingElse": True
    }


# ── view creation ─────────────────────────────────────────────


def test_view_written_without_refresh_token(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host(host)

    mode = sync_host_credentials_view(host, agent)

    assert mode == "view"
    view = _agent_view(agent)
    assert view.exists() and not view.is_symlink()
    data = json.loads(view.read_text(encoding="utf-8"))
    assert "refreshToken" not in data["claudeAiOauth"]
    assert data["claudeAiOauth"]["accessToken"] == "at-123"


def test_view_file_is_owner_only(tmp_path):
    if os.name == "nt":
        import pytest
        pytest.skip("posix permission bits")
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host(host)
    sync_host_credentials_view(host, agent)
    mode = _agent_view(agent).stat().st_mode & 0o777
    assert mode == 0o600


def test_view_idempotent(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host(host)

    assert sync_host_credentials_view(host, agent) == "view"
    before = _agent_view(agent).stat().st_mtime_ns
    assert sync_host_credentials_view(host, agent) == "view (fresh)"
    # No rewrite -> no mtime churn.
    assert _agent_view(agent).stat().st_mtime_ns == before


def test_view_tracks_host_rotation(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host(host)
    sync_host_credentials_view(host, agent)

    rotated = json.loads(json.dumps(HOST_CREDS))
    rotated["claudeAiOauth"]["accessToken"] = "at-789"
    rotated["claudeAiOauth"]["refreshToken"] = "rt-new-000"
    _write_host(host, rotated)

    assert sync_host_credentials_view(host, agent) == "view"
    data = json.loads(_agent_view(agent).read_text(encoding="utf-8"))
    assert data["claudeAiOauth"]["accessToken"] == "at-789"
    assert "refreshToken" not in data["claudeAiOauth"]


# ── self-healing ──────────────────────────────────────────────


def test_view_heals_agent_side_garbage(tmp_path):
    """A failed in-CLI refresh can mangle the agent's file (observed:
    zeroed expiresAt). The next sync rewrites it from the host blob."""
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host(host)
    sync_host_credentials_view(host, agent)

    _agent_view(agent).write_text("{}", encoding="utf-8")

    assert sync_host_credentials_view(host, agent) == "view"
    data = json.loads(_agent_view(agent).read_text(encoding="utf-8"))
    assert data["claudeAiOauth"]["accessToken"] == "at-123"


# ── legacy symlink migration ──────────────────────────────────


def test_migrates_legacy_symlink_without_touching_host(tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    host_creds = _write_host(host)
    host_blob_before = host_creds.read_text(encoding="utf-8")

    view = _agent_view(agent)
    view.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(host_creds, view)

    mode = sync_host_credentials_view(host, agent)

    assert mode == "view (migrated-from-symlink)"
    assert not view.is_symlink()
    data = json.loads(view.read_text(encoding="utf-8"))
    assert "refreshToken" not in data["claudeAiOauth"]
    # The host file (the old symlink target) keeps its refresh token.
    assert host_creds.read_text(encoding="utf-8") == host_blob_before
    assert "rt-secret-456" in host_creds.read_text(encoding="utf-8")


def test_migrates_broken_symlink(tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host(host)

    view = _agent_view(agent)
    view.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "ghost", view)

    assert sync_host_credentials_view(host, agent) == (
        "view (migrated-from-symlink)"
    )
    assert not view.is_symlink()
    assert "refreshToken" not in json.loads(
        view.read_text(encoding="utf-8")
    )["claudeAiOauth"]


# ── degenerate hosts ──────────────────────────────────────────


def test_no_host_file(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    assert sync_host_credentials_view(host, agent) == "no-host-file"
    assert not _agent_view(agent).exists()


def test_chmod_failure_is_swallowed(tmp_path, monkeypatch):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host(host)

    real_chmod = Path.chmod

    def _fail_chmod(self, *args, **kwargs):
        if ".credentials.json.tmp" in self.name:
            raise OSError("simulated chmod failure")
        return real_chmod(self, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", _fail_chmod)

    assert sync_host_credentials_view(host, agent) == "view"
    view = _agent_view(agent)
    assert "refreshToken" not in json.loads(
        view.read_text(encoding="utf-8")
    )["claudeAiOauth"]


def test_agent_read_error_falls_through_to_rewrite(tmp_path, monkeypatch):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host(host)

    # Pre-place a file so the "not migrated" path enters read-compare.
    view = _agent_view(agent)
    view.parent.mkdir(parents=True, exist_ok=True)
    view.write_text("stale-and-unreadable", encoding="utf-8")

    real_read = Path.read_text

    def _fail_agent_read(self, *args, **kwargs):
        if self == view:
            raise OSError("simulated read failure")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _fail_agent_read)

    # Read-compare raised → falls through to write; final content is the view.
    assert sync_host_credentials_view(host, agent) == "view"
    monkeypatch.setattr(Path, "read_text", real_read)
    assert "refreshToken" not in json.loads(
        view.read_text(encoding="utf-8")
    )["claudeAiOauth"]


def test_concurrent_syncs_produce_valid_view(tmp_path):
    """The race this whole design fixes: N concurrent writers hitting
    the same agent view must all leave a complete, refresh-token-free
    file behind. Also pins that the tmp+rename doesn't leak a partial
    write on a losing thread."""
    import threading

    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host(host)

    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def _run():
        try:
            barrier.wait()
            for _ in range(20):
                sync_host_credentials_view(host, agent)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent sync raised: {errors!r}"
    view = _agent_view(agent)
    assert view.exists() and not view.is_symlink()
    data = json.loads(view.read_text(encoding="utf-8"))
    assert data["claudeAiOauth"]["accessToken"] == "at-123"
    assert "refreshToken" not in data["claudeAiOauth"]


def test_write_failure_returns_write_failed(tmp_path, monkeypatch):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host(host)

    def _boom(target, blob):
        raise OSError("simulated write failure")

    monkeypatch.setattr(
        "puffo_agent.portal.state._write_credential_view", _boom,
    )
    assert sync_host_credentials_view(host, agent) == "write-failed"
    assert not _agent_view(agent).exists()


def test_unparseable_host_file_leaves_agent_view_alone(tmp_path):
    host = tmp_path / "host"
    agent = tmp_path / "agent"
    _write_host(host)
    sync_host_credentials_view(host, agent)
    good_view = _agent_view(agent).read_text(encoding="utf-8")

    (host / ".claude" / ".credentials.json").write_text(
        "corrupted {", encoding="utf-8",
    )

    assert sync_host_credentials_view(host, agent) == "unparseable-host-file"
    # Existing (still-valid) view untouched.
    assert _agent_view(agent).read_text(encoding="utf-8") == good_view
