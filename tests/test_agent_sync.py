"""Profile sync — reload.flag drop, control-WS flag differentiation,
sync_full_profile."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _bridge_support import isolated_home, write_test_agent  # noqa: E402

from puffo_agent.portal.profile_sync import (  # noqa: E402
    sync_full_profile,
    write_reload_flag,
)
from puffo_agent.portal.state import AgentConfig  # noqa: E402


# ── write_reload_flag ─────────────────────────────────────────────


def test_write_reload_flag_drops_versioned_json():
    home = isolated_home()
    write_test_agent(home, "flag-bot")
    cfg = AgentConfig.load("flag-bot")
    write_reload_flag(cfg, reason="unit test")
    flag_path = cfg.resolve_workspace_dir() / ".puffo-agent" / "reload.flag"
    assert flag_path.exists()
    payload = json.loads(flag_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["reason"] == "unit test"
    assert isinstance(payload["requested_at"], int)


# ── sync_full_profile ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_full_profile_sends_every_field_plus_soul(tmp_path, monkeypatch):
    home = isolated_home()
    write_test_agent(home, "full-bot")
    cfg = AgentConfig.load("full-bot")
    cfg.display_name = "Full Bot"
    cfg.role = "Dev: tester"
    cfg.role_short = "Dev"
    cfg.avatar_url = "https://example.test/avatar.png"
    cfg.save()
    profile_path = cfg.resolve_profile_path()
    profile_path.write_text("# Full Bot\n\nfull system prompt\n", encoding="utf-8")

    posted: list[tuple[str, dict]] = []

    class _FakeHttp:
        def __init__(self, *a, **kw):
            pass

        async def patch(self, path: str, body: dict) -> dict:
            posted.append((path, body))
            return {}

        async def close(self) -> None:
            pass

    monkeypatch.setattr(
        "puffo_agent.crypto.http_client.PuffoCoreHttpClient",
        _FakeHttp,
    )
    await sync_full_profile(cfg)

    assert len(posted) == 1
    path, body = posted[0]
    assert path == "/identities/self"
    assert body["display_name"] == "Full Bot"
    assert body["role"] == "Dev: tester"
    assert body["role_short"] == "Dev"
    assert body["avatar_url"] == "https://example.test/avatar.png"
    assert body["soul"] == "# Full Bot\n\nfull system prompt\n"


@pytest.mark.asyncio
async def test_sync_full_profile_skips_soul_when_profile_md_absent(monkeypatch):
    home = isolated_home()
    write_test_agent(home, "no-md-bot")
    cfg = AgentConfig.load("no-md-bot")
    cfg.display_name = "No MD"
    cfg.save()
    try:
        cfg.resolve_profile_path().unlink()
    except FileNotFoundError:
        pass

    posted: list[tuple[str, dict]] = []

    class _FakeHttp:
        def __init__(self, *a, **kw):
            pass

        async def patch(self, path: str, body: dict) -> dict:
            posted.append((path, body))
            return {}

        async def close(self) -> None:
            pass

    monkeypatch.setattr(
        "puffo_agent.crypto.http_client.PuffoCoreHttpClient",
        _FakeHttp,
    )
    await sync_full_profile(cfg)

    assert len(posted) == 1
    _, body = posted[0]
    assert "soul" not in body
    assert body["display_name"] == "No MD"


# ── control-WS op=edit flag differentiation ──────────────────────


def _patch_sync(monkeypatch):
    """Stub the server PATCH so op=edit can run end-to-end without HTTP."""
    sent: list[dict] = []

    async def _fake_sync(cfg, patch):
        sent.append(dict(patch))

    monkeypatch.setattr(
        "puffo_agent.portal.api.handlers._sync_agent_profile",
        _fake_sync,
    )
    return sent


@pytest.mark.asyncio
async def test_control_edit_profile_only_writes_reload_flag(monkeypatch):
    home = isolated_home()
    write_test_agent(home, "edit-bot")
    cfg = AgentConfig.load("edit-bot")
    cfg.display_name = "Edit Bot"
    cfg.state = "running"
    cfg.save()
    _patch_sync(monkeypatch)

    from puffo_agent.portal.control import client as ctrl

    result = await ctrl.execute_command(
        op="edit",
        agent_slug="edit-bot",
        params={"display_name": "Renamed Bot"},
        server_url="https://example.test",
        paired_root_pubkey="op-pk",
    )
    assert result == {"ok": True}

    workspace = AgentConfig.load("edit-bot").resolve_workspace_dir()
    reload_flag = workspace / ".puffo-agent" / "reload.flag"
    restart_flag = Path(home) / "agents" / "edit-bot" / ".puffo-agent" / "restart.flag"
    assert reload_flag.exists()
    assert not restart_flag.exists()


@pytest.mark.asyncio
async def test_control_edit_runtime_writes_restart_flag(monkeypatch):
    home = isolated_home()
    write_test_agent(home, "rt-bot")
    cfg = AgentConfig.load("rt-bot")
    cfg.state = "running"
    cfg.save()
    _patch_sync(monkeypatch)

    from puffo_agent.portal.control import client as ctrl

    result = await ctrl.execute_command(
        op="edit",
        agent_slug="rt-bot",
        params={"runtime": {"model": "claude-sonnet-4-7"}},
        server_url="https://example.test",
        paired_root_pubkey="op-pk",
    )
    assert result == {"ok": True}

    workspace = AgentConfig.load("rt-bot").resolve_workspace_dir()
    reload_flag = workspace / ".puffo-agent" / "reload.flag"
    restart_flag = Path(home) / "agents" / "rt-bot" / ".puffo-agent" / "restart.flag"
    assert restart_flag.exists()
    assert not reload_flag.exists()


@pytest.mark.asyncio
async def test_full_sync_all_owned_agents_at_startup_fans_out(monkeypatch):
    # The daemon-startup helper should call sync_full_profile once
    # per agent that has a configured puffo_core block + keystore.
    home = isolated_home()
    write_test_agent(home, "alpha-bot")
    write_test_agent(home, "beta-bot")

    called: list[str] = []

    async def _fake_sync(cfg):
        called.append(cfg.id)

    monkeypatch.setattr(
        "puffo_agent.portal.profile_sync.sync_full_profile",
        _fake_sync,
    )

    # KeyStore.for_agent(...).load_session is the gate; stub it open.
    class _FakeKS:
        @staticmethod
        def for_agent(agent_id):
            class _Inst:
                def load_session(self, slug):
                    return object()
            return _Inst()

    monkeypatch.setattr(
        "puffo_agent.crypto.keystore.KeyStore", _FakeKS,
    )

    from puffo_agent.portal import daemon as daemon_mod
    await daemon_mod._full_sync_all_owned_agents_at_startup()
    assert sorted(called) == ["alpha-bot", "beta-bot"]


@pytest.mark.asyncio
async def test_full_sync_skips_agents_without_keystore(monkeypatch):
    # Agent dir exists but the keystore can't be loaded → skip
    # silently. Common case for not-yet-finalised registrations.
    home = isolated_home()
    write_test_agent(home, "skeleton-bot")

    called: list[str] = []

    async def _fake_sync(cfg):
        called.append(cfg.id)

    monkeypatch.setattr(
        "puffo_agent.portal.profile_sync.sync_full_profile",
        _fake_sync,
    )

    class _FakeKS:
        @staticmethod
        def for_agent(agent_id):
            class _Inst:
                def load_session(self, slug):
                    raise RuntimeError("no session")
            return _Inst()

    monkeypatch.setattr(
        "puffo_agent.crypto.keystore.KeyStore", _FakeKS,
    )

    from puffo_agent.portal import daemon as daemon_mod
    await daemon_mod._full_sync_all_owned_agents_at_startup()
    assert called == []


@pytest.mark.asyncio
async def test_control_edit_paused_agent_drops_no_flag(monkeypatch):
    # Paused workers have no live CLI subprocess to reload — flag drop
    # would be a no-op anyway. Avoid the stale flag.
    home = isolated_home()
    write_test_agent(home, "paused-bot")
    cfg = AgentConfig.load("paused-bot")
    cfg.state = "paused"
    cfg.save()
    _patch_sync(monkeypatch)

    from puffo_agent.portal.control import client as ctrl

    await ctrl.execute_command(
        op="edit",
        agent_slug="paused-bot",
        params={"soul": "fresh prompt"},
        server_url="https://example.test",
        paired_root_pubkey="op-pk",
    )

    workspace = AgentConfig.load("paused-bot").resolve_workspace_dir()
    assert not (workspace / ".puffo-agent" / "reload.flag").exists()
    assert not (Path(home) / "agents" / "paused-bot" / ".puffo-agent" / "restart.flag").exists()
