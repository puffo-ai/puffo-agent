"""``migrate_owned_agents`` syncs soul alongside machine_id.
``PuffoCoreHttpClient`` + ``sync_agent_profile`` are stubbed so the
migrate path runs end-to-end without a live server."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _bridge_support import isolated_home, write_test_agent

from puffo_agent.portal.control import link as link_mod
from puffo_agent.portal.control.link import migrate_owned_agents


_OWNER_PK = "owner-root-pk-test"


class _FakeHttp:
    posts: list[tuple[str, dict]] = []
    close_calls = 0

    def __init__(self, server_url, keystore, slug):
        self.server_url = server_url
        self.slug = slug

    async def post(self, path: str, body: dict) -> dict:
        _FakeHttp.posts.append((path, body))
        return {}

    async def close(self) -> None:
        _FakeHttp.close_calls += 1


def _reset_fake_http() -> None:
    _FakeHttp.posts = []
    _FakeHttp.close_calls = 0


def _patch_http(monkeypatch) -> None:
    # link.py imports PuffoCoreHttpClient lazily inside
    # migrate_owned_agents; patch at the source module.
    from puffo_agent.crypto import http_client as http_mod

    _reset_fake_http()
    monkeypatch.setattr(http_mod, "PuffoCoreHttpClient", _FakeHttp)


def _patch_machine_id(monkeypatch, machine_id: str = "mch_test_42") -> None:
    monkeypatch.setattr(link_mod, "current_machine_id", lambda: machine_id)


def _patch_sync_agent_profile(monkeypatch, raises: Exception | None = None):
    calls: list[tuple[str, dict]] = []
    from puffo_agent.portal import profile_sync as ps_mod

    async def _stub(cfg, patch):
        calls.append((cfg.id, dict(patch)))
        if raises is not None:
            raise raises

    monkeypatch.setattr(ps_mod, "sync_agent_profile", _stub)
    return calls


@pytest.mark.asyncio
async def test_migrate_syncs_soul_after_machine_id(monkeypatch):
    home = isolated_home()
    write_test_agent(home, "scout", owner_root_pubkey=_OWNER_PK)
    Path(home, "agents", "scout", "profile.md").write_text(
        "# Scout\n\nA fast scout agent.\n", encoding="utf-8",
    )

    _patch_machine_id(monkeypatch)
    _patch_http(monkeypatch)
    soul_calls = _patch_sync_agent_profile(monkeypatch)

    reported = await migrate_owned_agents(_OWNER_PK)

    assert reported == 1
    assert len(_FakeHttp.posts) == 1
    path, body = _FakeHttp.posts[0]
    assert path == "/agents/me/heartbeat"
    assert body["machine_id"] == "mch_test_42"
    assert body["status"] == "idle"
    assert len(soul_calls) == 1
    agent_id, patch = soul_calls[0]
    assert agent_id == "scout"
    assert patch == {"soul": "# Scout\n\nA fast scout agent.\n"}
    assert _FakeHttp.close_calls == 1


@pytest.mark.asyncio
async def test_migrate_skips_soul_when_machine_id_stamp_fails(monkeypatch, caplog):
    home = isolated_home()
    write_test_agent(home, "scout", owner_root_pubkey=_OWNER_PK)

    _patch_machine_id(monkeypatch)
    soul_calls = _patch_sync_agent_profile(monkeypatch)

    from puffo_agent.crypto.http_client import HttpError

    class _FailingHttp(_FakeHttp):
        async def post(self, path, body):
            raise HttpError(503, "service unavailable")

    from puffo_agent.crypto import http_client as http_mod
    _reset_fake_http()
    monkeypatch.setattr(http_mod, "PuffoCoreHttpClient", _FailingHttp)

    with caplog.at_level(logging.WARNING, logger="puffo_agent.portal.control.link"):
        reported = await migrate_owned_agents(_OWNER_PK)

    assert reported == 0
    assert soul_calls == []
    assert any("machine_id stamp rejected" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_migrate_handles_missing_profile_md_gracefully(monkeypatch):
    home = isolated_home()
    write_test_agent(home, "scout", owner_root_pubkey=_OWNER_PK)
    Path(home, "agents", "scout", "profile.md").unlink()

    _patch_machine_id(monkeypatch)
    _patch_http(monkeypatch)
    soul_calls = _patch_sync_agent_profile(monkeypatch)

    reported = await migrate_owned_agents(_OWNER_PK)

    assert reported == 1
    assert soul_calls == []
    assert _FakeHttp.close_calls == 1


@pytest.mark.asyncio
async def test_soul_sync_failure_logs_but_machine_id_remains(monkeypatch, caplog):
    home = isolated_home()
    write_test_agent(home, "scout", owner_root_pubkey=_OWNER_PK)

    _patch_machine_id(monkeypatch)
    _patch_http(monkeypatch)
    from puffo_agent.crypto.http_client import HttpError

    soul_calls = _patch_sync_agent_profile(
        monkeypatch, raises=HttpError(500, "server unhappy"),
    )

    with caplog.at_level(logging.WARNING, logger="puffo_agent.portal.control.link"):
        reported = await migrate_owned_agents(_OWNER_PK)

    assert reported == 1
    assert len(_FakeHttp.posts) == 1
    assert len(soul_calls) == 1
    # WARN must surface the partial-success state explicitly.
    assert any(
        "soul sync rejected" in rec.message
        and "machine_id landed" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_migrate_skips_non_owned_agents(monkeypatch):
    home = isolated_home()
    write_test_agent(home, "scout", owner_root_pubkey="other-operator-pk")

    _patch_machine_id(monkeypatch)
    _patch_http(monkeypatch)
    soul_calls = _patch_sync_agent_profile(monkeypatch)

    reported = await migrate_owned_agents(_OWNER_PK)

    assert reported == 0
    assert _FakeHttp.posts == []
    assert soul_calls == []


@pytest.mark.asyncio
async def test_migrate_syncs_soul_for_every_owned_agent(monkeypatch):
    home = isolated_home()
    write_test_agent(home, "scout", owner_root_pubkey=_OWNER_PK)
    write_test_agent(home, "ranger", owner_root_pubkey=_OWNER_PK)

    Path(home, "agents", "scout", "profile.md").write_text(
        "Scout body", encoding="utf-8",
    )
    Path(home, "agents", "ranger", "profile.md").write_text(
        "Ranger body", encoding="utf-8",
    )

    _patch_machine_id(monkeypatch)
    _patch_http(monkeypatch)
    soul_calls = _patch_sync_agent_profile(monkeypatch)

    reported = await migrate_owned_agents(_OWNER_PK)

    assert reported == 2
    by_agent = {agent_id: patch for agent_id, patch in soul_calls}
    assert by_agent == {
        "scout": {"soul": "Scout body"},
        "ranger": {"soul": "Ranger body"},
    }
    assert _FakeHttp.close_calls == 2


@pytest.mark.asyncio
async def test_migrate_noop_when_machine_unlinked(monkeypatch):
    home = isolated_home()
    write_test_agent(home, "scout", owner_root_pubkey=_OWNER_PK)

    monkeypatch.setattr(link_mod, "current_machine_id", lambda: "")
    _patch_http(monkeypatch)
    soul_calls = _patch_sync_agent_profile(monkeypatch)

    reported = await migrate_owned_agents(_OWNER_PK)
    assert reported == 0
    assert _FakeHttp.posts == []
    assert soul_calls == []
