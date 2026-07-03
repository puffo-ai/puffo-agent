"""Tests for ``sync_host_codex_auth_view`` — the per-agent codex auth
*view* written into each agent's ``$CODEX_HOME``.

Mirrors ``test_host_credentials.py``'s shape, with one codex-specific
wrinkle: codex hard-fails deserialisation when ``tokens.refresh_token``
is *missing* (serde non-optional field), so the sanitized view keeps
the key with an empty-string value instead of dropping it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.portal.state import (
    sanitize_codex_auth_blob,
    sync_host_codex_auth_view,
)


HOST_AUTH = {
    "OPENAI_API_KEY": None,
    "auth_mode": "chatgpt",
    "tokens": {
        "id_token": "idt-abc",
        "access_token": "at-123",
        "refresh_token": "rt-secret-456",
        "account_id": "acct-1",
    },
    "last_refresh": "2026-07-02T00:00:00Z",
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


def _write_host(host: Path, auth: dict | None = None) -> Path:
    host_auth = host / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True, exist_ok=True)
    host_auth.write_text(json.dumps(auth or HOST_AUTH), encoding="utf-8")
    return host_auth


# ── sanitizer ─────────────────────────────────────────────────


def test_sanitize_blanks_refresh_token_keeps_key():
    view = json.loads(sanitize_codex_auth_blob(json.dumps(HOST_AUTH)))
    tokens = view["tokens"]
    # Key must survive (codex serde requires it) but hold no secret.
    assert tokens["refresh_token"] == ""
    assert tokens["access_token"] == "at-123"
    assert tokens["id_token"] == "idt-abc"
    assert view["auth_mode"] == "chatgpt"


def test_sanitize_rejects_non_json():
    assert sanitize_codex_auth_blob("nope {") is None


def test_sanitize_tolerates_api_key_only_auth():
    """API-key mode has ``tokens: null`` — pass through untouched."""
    blob = json.dumps({"OPENAI_API_KEY": "sk-x", "tokens": None})
    assert json.loads(sanitize_codex_auth_blob(blob)) == {
        "OPENAI_API_KEY": "sk-x",
        "tokens": None,
    }


# ── view lifecycle ────────────────────────────────────────────


def test_view_written_with_blanked_refresh_token(tmp_path):
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"

    mode = sync_host_codex_auth_view(_write_host(host).parent.parent, agent_codex)

    assert mode == "view"
    view = agent_codex / "auth.json"
    assert view.exists() and not view.is_symlink()
    data = json.loads(view.read_text(encoding="utf-8"))
    assert data["tokens"]["refresh_token"] == ""
    assert data["tokens"]["access_token"] == "at-123"


def test_view_idempotent(tmp_path):
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host(host)

    assert sync_host_codex_auth_view(host, agent_codex) == "view"
    assert sync_host_codex_auth_view(host, agent_codex) == "view (fresh)"


def test_view_tracks_host_rotation(tmp_path):
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host(host)
    sync_host_codex_auth_view(host, agent_codex)

    rotated = json.loads(json.dumps(HOST_AUTH))
    rotated["tokens"]["access_token"] = "at-789"
    rotated["tokens"]["refresh_token"] = "rt-new-000"
    _write_host(host, rotated)

    assert sync_host_codex_auth_view(host, agent_codex) == "view"
    data = json.loads((agent_codex / "auth.json").read_text(encoding="utf-8"))
    assert data["tokens"]["access_token"] == "at-789"
    assert data["tokens"]["refresh_token"] == ""


def test_migrates_legacy_symlink_without_touching_host(tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlinks unavailable on this host")
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    host_auth = _write_host(host)
    host_blob_before = host_auth.read_text(encoding="utf-8")

    view = agent_codex / "auth.json"
    view.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(host_auth, view)

    mode = sync_host_codex_auth_view(host, agent_codex)

    assert mode == "view (migrated-from-symlink)"
    assert not view.is_symlink()
    data = json.loads(view.read_text(encoding="utf-8"))
    assert data["tokens"]["refresh_token"] == ""
    # Host keeps the real refresh token.
    assert host_auth.read_text(encoding="utf-8") == host_blob_before
    assert "rt-secret-456" in host_auth.read_text(encoding="utf-8")


def test_no_host_file(tmp_path):
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    assert sync_host_codex_auth_view(host, agent_codex) == "no-host-file"
    assert not (agent_codex / "auth.json").exists()


def test_chmod_failure_is_swallowed(tmp_path, monkeypatch):
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host(host)

    real_chmod = Path.chmod

    def _fail_chmod(self, *args, **kwargs):
        if "auth.json.tmp" in self.name:
            raise OSError("simulated chmod failure")
        return real_chmod(self, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", _fail_chmod)

    assert sync_host_codex_auth_view(host, agent_codex) == "view"
    data = json.loads((agent_codex / "auth.json").read_text(encoding="utf-8"))
    assert data["tokens"]["refresh_token"] == ""


def test_agent_read_error_falls_through_to_rewrite(tmp_path, monkeypatch):
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host(host)

    view = agent_codex / "auth.json"
    view.parent.mkdir(parents=True, exist_ok=True)
    view.write_text("stale-and-unreadable", encoding="utf-8")

    real_read = Path.read_text

    def _fail_agent_read(self, *args, **kwargs):
        if self == view:
            raise OSError("simulated read failure")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _fail_agent_read)

    assert sync_host_codex_auth_view(host, agent_codex) == "view"
    monkeypatch.setattr(Path, "read_text", real_read)
    assert json.loads(view.read_text(encoding="utf-8"))["tokens"]["refresh_token"] == ""


def test_concurrent_syncs_produce_valid_view(tmp_path):
    """Race pin: N concurrent codex-view writers must leave a complete,
    refresh-token-blanked file behind, no partial-write leaks."""
    import threading

    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host(host)

    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def _run():
        try:
            barrier.wait()
            for _ in range(20):
                sync_host_codex_auth_view(host, agent_codex)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent sync raised: {errors!r}"
    view = agent_codex / "auth.json"
    assert view.exists() and not view.is_symlink()
    data = json.loads(view.read_text(encoding="utf-8"))
    assert data["tokens"]["access_token"] == "at-123"
    assert data["tokens"]["refresh_token"] == ""


def test_write_failure_returns_write_failed(tmp_path, monkeypatch):
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    _write_host(host)

    def _boom(target, blob):
        raise OSError("simulated write failure")

    monkeypatch.setattr(
        "puffo_agent.portal.state._write_credential_view", _boom,
    )
    assert sync_host_codex_auth_view(host, agent_codex) == "write-failed"
    assert not (agent_codex / "auth.json").exists()


def test_unparseable_host_file_leaves_view_alone(tmp_path):
    host = tmp_path / "host"
    agent_codex = tmp_path / "agent" / ".codex"
    host_auth = _write_host(host)
    sync_host_codex_auth_view(host, agent_codex)
    good_view = (agent_codex / "auth.json").read_text(encoding="utf-8")

    host_auth.write_text("corrupted {", encoding="utf-8")

    assert sync_host_codex_auth_view(host, agent_codex) == (
        "unparseable-host-file"
    )
    assert (agent_codex / "auth.json").read_text(encoding="utf-8") == good_view
