"""PUF-328: ``migrate_owned_agents`` syncs soul alongside machine_id.

When the daemon switches to linking-machine-mode (link approval OR
startup re-assert), ``link.migrate_owned_agents`` historically only
stamped ``machine_id`` via ``POST /agents/me/heartbeat``. The
server's identity ``soul`` field stayed empty for every agent the
operator hadn't manually edited via the bridge since linking → the
web profile pane rendered an empty soul-section. PUF-328 (FB-337
fix-shape) extends the migrate flow to also PATCH the agent's soul.

Tests stub ``PuffoCoreHttpClient`` and ``sync_agent_profile`` so
the migrate path can be driven end-to-end without a live server.
"""

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
    """Captures the ``/heartbeat`` POST so a test can assert on the
    machine_id payload. The constructor signature mirrors
    ``PuffoCoreHttpClient(server_url, keystore, slug)``."""

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
    """Replace ``PuffoCoreHttpClient`` everywhere ``link.py`` imports
    it from. The lazy import lives inside ``migrate_owned_agents``,
    so we patch at the source module."""
    from puffo_agent.crypto import http_client as http_mod

    _reset_fake_http()
    monkeypatch.setattr(http_mod, "PuffoCoreHttpClient", _FakeHttp)


def _patch_machine_id(monkeypatch, machine_id: str = "mch_test_42") -> None:
    monkeypatch.setattr(link_mod, "current_machine_id", lambda: machine_id)


def _patch_sync_agent_profile(monkeypatch, raises: Exception | None = None):
    """Replace ``profile_sync.sync_agent_profile`` and record calls.
    Returns a list the test can introspect."""
    calls: list[tuple[str, dict]] = []
    from puffo_agent.portal import profile_sync as ps_mod

    async def _stub(cfg, patch):
        calls.append((cfg.id, dict(patch)))
        if raises is not None:
            raise raises

    monkeypatch.setattr(ps_mod, "sync_agent_profile", _stub)
    return calls


# ─── happy path: soul sync fires after successful machine_id stamp ─


@pytest.mark.asyncio
async def test_migrate_syncs_soul_after_machine_id(monkeypatch):
    home = isolated_home()
    write_test_agent(home, "scout", owner_root_pubkey=_OWNER_PK)
    # Override the seeded ``# test profile\n`` with deterministic content
    # so the test can verify the body byte-for-byte.
    Path(home, "agents", "scout", "profile.md").write_text(
        "# Scout\n\nA fast scout agent.\n", encoding="utf-8",
    )

    _patch_machine_id(monkeypatch)
    _patch_http(monkeypatch)
    soul_calls = _patch_sync_agent_profile(monkeypatch)

    reported = await migrate_owned_agents(_OWNER_PK)

    assert reported == 1
    # One heartbeat post (machine_id stamp).
    assert len(_FakeHttp.posts) == 1
    path, body = _FakeHttp.posts[0]
    assert path == "/agents/me/heartbeat"
    assert body["machine_id"] == "mch_test_42"
    assert body["status"] == "idle"
    # One soul sync with the actual profile.md body.
    assert len(soul_calls) == 1
    agent_id, patch = soul_calls[0]
    assert agent_id == "scout"
    assert patch == {"soul": "# Scout\n\nA fast scout agent.\n"}
    # Connection was closed exactly once for the agent.
    assert _FakeHttp.close_calls == 1


# ─── machine_id stamp failure short-circuits soul sync ────────────


@pytest.mark.asyncio
async def test_migrate_skips_soul_when_machine_id_stamp_fails(monkeypatch, caplog):
    """If /heartbeat refuses us, the next PATCH would almost certainly
    fail too (auth, server down). Skip the soul sync to keep the
    error-log volume sane on a degraded server."""
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

    assert reported == 0  # machine_id stamp didn't succeed
    assert soul_calls == []  # soul sync skipped
    assert any("machine_id stamp rejected" in rec.message for rec in caplog.records)


# ─── missing profile.md: skip soul sync but keep machine_id stamp ─


@pytest.mark.asyncio
async def test_migrate_handles_missing_profile_md_gracefully(monkeypatch):
    home = isolated_home()
    write_test_agent(home, "scout", owner_root_pubkey=_OWNER_PK)
    # Delete profile.md to simulate the edge case (agent created before
    # profile-required + then re-discovered).
    Path(home, "agents", "scout", "profile.md").unlink()

    _patch_machine_id(monkeypatch)
    _patch_http(monkeypatch)
    soul_calls = _patch_sync_agent_profile(monkeypatch)

    reported = await migrate_owned_agents(_OWNER_PK)

    assert reported == 1  # heartbeat still landed
    assert soul_calls == []  # no soul to sync; no PATCH attempted
    assert _FakeHttp.close_calls == 1


# ─── soul-sync HTTP error logs but doesn't unwind the machine_id ──


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

    # Heartbeat still landed (the soul-sync failure is downstream
    # of the heartbeat success).
    assert reported == 1
    assert len(_FakeHttp.posts) == 1  # the heartbeat fired
    assert len(soul_calls) == 1  # the soul-sync attempt also fired
    # The warning surfaces the partial-success state for the operator.
    assert any(
        "soul sync rejected" in rec.message
        and "machine_id stamp already succeeded" in rec.message
        for rec in caplog.records
    )


# ─── non-owned agents are still skipped ───────────────────────────


@pytest.mark.asyncio
async def test_migrate_skips_non_owned_agents(monkeypatch):
    home = isolated_home()
    # Owned by a DIFFERENT operator → not in our purview.
    write_test_agent(home, "scout", owner_root_pubkey="other-operator-pk")

    _patch_machine_id(monkeypatch)
    _patch_http(monkeypatch)
    soul_calls = _patch_sync_agent_profile(monkeypatch)

    reported = await migrate_owned_agents(_OWNER_PK)

    assert reported == 0
    assert _FakeHttp.posts == []
    assert soul_calls == []


# ─── multiple owned agents: all get soul sync ─────────────────────


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
    # Both agents got their soul synced.
    by_agent = {agent_id: patch for agent_id, patch in soul_calls}
    assert by_agent == {
        "scout": {"soul": "Scout body"},
        "ranger": {"soul": "Ranger body"},
    }
    # One close per agent.
    assert _FakeHttp.close_calls == 2


# ─── current_machine_id None: whole migrate is a no-op ────────────


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
