"""PUF-401: role_short is single-source-derived from role.

Covers the canonical ``derive_role_short`` (state.py), the thin
bridge/CLI wrappers delegating to it, the control-WS role-write path
that used to orphan role_short, and the daemon-startup backfill that
repairs a stale role_short instead of reverting the server to it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from _bridge_support import (  # noqa: E402
    isolated_home,
    make_user,
    pair_request_body,
    signed_headers,
    write_test_agent,
)
from puffo_agent.crypto.encoding import base64url_encode  # noqa: E402
from puffo_agent.portal.api.server import build_app  # noqa: E402
from puffo_agent.portal.state import DaemonConfig  # noqa: E402

from puffo_agent.portal.api.handlers import _derive_role_short  # noqa: E402
from puffo_agent.portal.cli import _derive_role_short_cli  # noqa: E402
from puffo_agent.portal.control.client import execute_command  # noqa: E402
from puffo_agent.portal.profile_sync import sync_full_profile  # noqa: E402
from puffo_agent.portal.state import AgentConfig, derive_role_short  # noqa: E402


# ─── canonical derive_role_short (single source) ──────────────────


def test_derive_role_short_edge_cases():
    assert derive_role_short("coder: main puffo-core coder") == "coder"
    assert derive_role_short("reviewer: code review specialist") == "reviewer"
    # trailing whitespace before the colon is tolerated
    assert derive_role_short("coder :  desc") == "coder"
    # no colon → empty
    assert derive_role_short("just a description") == ""
    assert derive_role_short("") == ""
    # empty prefix / empty suffix → empty
    assert derive_role_short(": missing prefix") == ""
    assert derive_role_short("only-prefix:") == ""
    assert derive_role_short("only-prefix:   ") == ""
    # whitespace inside the prefix → not a clean chip label
    assert derive_role_short("two words: desc") == ""
    # overlong prefix (>32 chars) → empty
    assert derive_role_short("a" * 33 + ": x") == ""
    # exactly 32 is accepted
    assert derive_role_short("a" * 32 + ": x") == "a" * 32


def test_wrappers_delegate_to_canonical():
    """The bridge + CLI helpers must be thin wrappers — identical output
    to the one canonical implementation for every shape."""
    cases = [
        "coder: main coder",
        "plain text no colon",
        ": empty-prefix",
        "trailing:",
        "two words: nope",
        "a" * 33 + ": overlong",
        "coder :  desc",
        "",
    ]
    for c in cases:
        assert _derive_role_short(c) == derive_role_short(c), c
        assert _derive_role_short_cli(c) == derive_role_short(c), c


# ─── control-WS role-write path no longer orphans role_short ───────


@pytest.fixture()
def home():
    return isolated_home()


@pytest.mark.asyncio
async def test_control_edit_role_sets_derived_role_short(home, monkeypatch):
    write_test_agent(home, "scout")

    posted: dict = {}

    async def _fake_sync(cfg, patch):
        posted.update(patch)

    monkeypatch.setattr(
        "puffo_agent.portal.api.handlers._sync_agent_profile", _fake_sync
    )

    res = await execute_command(
        "edit", "scout", {"role": "CPO: chief product officer"}
    )
    assert res["ok"] is True

    cfg = AgentConfig.load("scout")
    assert cfg.role == "CPO: chief product officer"
    # role_short is derived, not orphaned to the pre-edit value
    assert cfg.role_short == "CPO"
    # and the derived chip rides the server patch
    assert posted["role"] == "CPO: chief product officer"
    assert posted["role_short"] == "CPO"


@pytest.mark.asyncio
async def test_control_edit_role_without_colon_clears_chip(home, monkeypatch):
    write_test_agent(home, "scout")
    monkeypatch.setattr(
        "puffo_agent.portal.api.handlers._sync_agent_profile",
        _noop_sync,
    )
    res = await execute_command("edit", "scout", {"role": "researcher"})
    assert res["ok"] is True
    cfg = AgentConfig.load("scout")
    assert cfg.role == "researcher"
    assert cfg.role_short == ""


async def _noop_sync(cfg, patch):
    return None


# ─── daemon-startup backfill (the load-bearing no-revert fix) ──────


def _write_agent_with_roles(home: str, agent_id: str, role: str, role_short: str):
    write_test_agent(home, agent_id)
    cfg = AgentConfig.load(agent_id)
    cfg.role = role
    cfg.role_short = role_short
    cfg.save()
    return cfg


class _FakeHttp:
    posted: list[dict] = []

    def __init__(self, *a, **kw):
        pass

    async def patch(self, path, body):
        _FakeHttp.posted.append(body)
        return {}

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_startup_backfill_repairs_stale_role_short(monkeypatch):
    home = isolated_home()
    # Stale on-disk orphan: role updated to a CPO role but role_short
    # still carries a previous "Receptionist" chip.
    _write_agent_with_roles(
        home, "chip-bot", "CPO: chief product officer", "Receptionist"
    )
    _FakeHttp.posted = []
    monkeypatch.setattr(
        "puffo_agent.crypto.http_client.PuffoCoreHttpClient", _FakeHttp
    )

    cfg = AgentConfig.load("chip-bot")
    await sync_full_profile(cfg)

    # persisted to agent.yml
    reloaded = AgentConfig.load("chip-bot")
    assert reloaded.role_short == "CPO"
    # corrected value synced to the server, not the stale one
    assert _FakeHttp.posted[-1]["role_short"] == "CPO"
    # role itself untouched
    assert reloaded.role == "CPO: chief product officer"


@pytest.mark.asyncio
async def test_startup_backfill_idempotent(monkeypatch):
    home = isolated_home()
    _write_agent_with_roles(
        home, "chip-bot", "CPO: chief product officer", "CPO"
    )
    _FakeHttp.posted = []
    monkeypatch.setattr(
        "puffo_agent.crypto.http_client.PuffoCoreHttpClient", _FakeHttp
    )

    cfg = AgentConfig.load("chip-bot")
    mtime_before = os.path.getmtime(
        os.path.join(home, "agents", "chip-bot", "agent.yml")
    )
    await sync_full_profile(cfg)
    mtime_after = os.path.getmtime(
        os.path.join(home, "agents", "chip-bot", "agent.yml")
    )
    # already matched → no rewrite (no thrash)
    assert mtime_before == mtime_after
    assert AgentConfig.load("chip-bot").role_short == "CPO"


@pytest.mark.asyncio
async def test_startup_backfill_skips_empty_role(monkeypatch):
    home = isolated_home()
    # role empty, role_short empty → nothing to derive, no write
    write_test_agent(home, "blank-bot")
    _FakeHttp.posted = []
    monkeypatch.setattr(
        "puffo_agent.crypto.http_client.PuffoCoreHttpClient", _FakeHttp
    )
    cfg = AgentConfig.load("blank-bot")
    await sync_full_profile(cfg)
    reloaded = AgentConfig.load("blank-bot")
    assert reloaded.role == ""
    assert reloaded.role_short == ""


@pytest.mark.asyncio
async def test_startup_backfill_preserves_behavior_fields(monkeypatch):
    """Non-regression: the backfill touches only role_short — soul /
    profile.md long role / triggers / runtime are unchanged."""
    home = isolated_home()
    _write_agent_with_roles(
        home, "chip-bot", "CPO: chief product officer", "stale"
    )
    profile_path = AgentConfig.load("chip-bot").resolve_profile_path()
    profile_path.write_text(
        "# chip-bot\n\n# Soul\n\nI lead product.\n", encoding="utf-8"
    )
    before_triggers = AgentConfig.load("chip-bot").triggers
    before_runtime_kind = AgentConfig.load("chip-bot").runtime.kind

    _FakeHttp.posted = []
    monkeypatch.setattr(
        "puffo_agent.crypto.http_client.PuffoCoreHttpClient", _FakeHttp
    )
    cfg = AgentConfig.load("chip-bot")
    await sync_full_profile(cfg)

    reloaded = AgentConfig.load("chip-bot")
    assert reloaded.role_short == "CPO"
    # behavior fields intact
    assert reloaded.triggers == before_triggers
    assert reloaded.runtime.kind == before_runtime_kind
    # soul section still in profile.md; synced body unchanged
    assert profile_path.read_text(encoding="utf-8").count("# Soul") == 1
    assert _FakeHttp.posted[-1]["soul"] == "I lead product."


# ─── deprecated explicit-override → accept-but-warn (PUF-401) ──────
# The bridge PATCH and the CLI `agent profile` are the two live
# override surfaces; both now derive authoritatively and log a
# deprecation warning when a differing explicit role_short is dropped
# (never silently). Provision + `agent create` share this exact
# derive-and-warn logic.


_HOST = {"Host": "127.0.0.1:63387"}


@pytest.mark.asyncio
async def test_bridge_update_profile_warns_on_explicit_role_short(
    monkeypatch, caplog
):
    home = isolated_home()
    user = make_user()
    write_test_agent(
        home,
        "chip-bot",
        owner_root_pubkey=base64url_encode(user.root_key.public_key_bytes()),
    )

    async def _noop_sync(cfg, patch):
        return None

    monkeypatch.setattr(
        "puffo_agent.portal.api.handlers._sync_agent_profile", _noop_sync
    )

    app = build_app(DaemonConfig().bridge)
    server = TestServer(app)
    async with TestClient(server) as c:
        body = pair_request_body(user)
        h = signed_headers(user, "POST", "/v1/pair", body)
        h.update(_HOST)
        assert (await c.post("/v1/pair", data=body, headers=h)).status == 200

        payload = json.dumps(
            {"role": "coder: main puffo-core coder", "role_short": "BOGUS"}
        ).encode("utf-8")
        h = signed_headers(
            user, "PATCH", "/v1/agents/chip-bot/profile", payload
        )
        h.update(_HOST)
        h["content-type"] = "application/json"
        with caplog.at_level("WARNING"):
            r = await c.patch(
                "/v1/agents/chip-bot/profile", data=payload, headers=h
            )
        assert r.status == 200, await r.text()
        # derived value wins over the explicit override
        assert (await r.json())["role_short"] == "coder"
    # the drop is announced, not silent
    assert "deprecated (PUF-401)" in caplog.text
    assert "BOGUS" in caplog.text


def test_cli_agent_profile_warns_on_explicit_role_short(
    monkeypatch, capsys
):
    home = isolated_home()
    write_test_agent(home, "chip-bot")

    async def _noop_sync(cfg, patch):
        return None

    monkeypatch.setattr(
        "puffo_agent.portal.profile_sync.sync_agent_profile", _noop_sync
    )

    args = argparse.Namespace(
        id="chip-bot",
        role="coder: main puffo-core coder",
        role_short="BOGUS",
        display_name=None,
    )
    from puffo_agent.portal.cli import cmd_agent_profile

    assert cmd_agent_profile(args) == 0
    err = capsys.readouterr().err
    assert "deprecated (PUF-401)" in err
    assert "BOGUS" in err
    # derive still wins on disk
    assert AgentConfig.load("chip-bot").role_short == "coder"


def test_cli_agent_create_warns_on_explicit_role_short(monkeypatch, capsys):
    isolated_home()
    monkeypatch.setattr(
        "puffo_agent.portal.cli._resolve_api_key_for_create", lambda **k: ""
    )
    args = argparse.Namespace(
        id="chip-bot",
        runtime="chat-local",
        provider=None,
        api_key=None,
        model=None,
        role="coder: main puffo-core coder",
        role_short="BOGUS",
        display_name=None,
        no_mention=False,
        no_dm=False,
        profile=None,
    )
    from puffo_agent.portal.cli import cmd_agent_create

    assert cmd_agent_create(args) == 0
    err = capsys.readouterr().err
    assert "deprecated (PUF-401)" in err
    assert "BOGUS" in err
    assert AgentConfig.load("chip-bot").role_short == "coder"
