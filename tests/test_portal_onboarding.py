"""Link-driven onboarding: machine_id migration, link auto-start, the
lifecycle 4xx give-up, and /v1/info machine_id."""
from __future__ import annotations

import argparse
import json
import types

import pytest

from puffo_agent.crypto.http_client import HttpError


def _fake_cfg(agent_id="a1", state="paused", configured=True,
              server_url="https://srv", slug="slug1"):
    pc = types.SimpleNamespace(
        is_configured=lambda: configured, server_url=server_url, slug=slug,
    )
    return types.SimpleNamespace(id=agent_id, state=state, puffo_core=pc)


class _RecordingHttp:
    posts: list = []

    def __init__(self, server_url, keystore, slug):
        self._url = server_url

    async def post(self, path, body):
        _RecordingHttp.posts.append((self._url, path, body))

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_posts():
    _RecordingHttp.posts = []
    yield


def _stub_http(monkeypatch, cls=_RecordingHttp):
    monkeypatch.setattr("puffo_agent.crypto.http_client.PuffoCoreHttpClient", cls)
    monkeypatch.setattr(
        "puffo_agent.crypto.keystore.KeyStore",
        types.SimpleNamespace(for_agent=lambda _id: object()),
    )


# ── F1: machine_id migration ────────────────────────────────────────

@pytest.mark.asyncio
async def test_migrate_stamps_machine_id_for_owned_configured_only(monkeypatch):
    from puffo_agent.portal.control import link

    _stub_http(monkeypatch)
    monkeypatch.setattr(link, "current_machine_id", lambda: "mac_X")
    monkeypatch.setattr(link, "discover_agents", lambda: ["own", "own_unconf", "foreign"])
    owners = {"own": True, "own_unconf": True, "foreign": False}
    monkeypatch.setattr(link, "is_owner", lambda aid, root: owners[aid])
    cfgs = {
        "own": _fake_cfg("own", state="running", configured=True),
        "own_unconf": _fake_cfg("own_unconf", configured=False),
        "foreign": _fake_cfg("foreign", configured=True),
    }
    monkeypatch.setattr(link.AgentConfig, "load", lambda aid: cfgs[aid])

    n = await link.migrate_owned_agents("ROOT")

    assert n == 1
    assert len(_RecordingHttp.posts) == 1
    _url, path, body = _RecordingHttp.posts[0]
    assert path == "/agents/me/heartbeat"
    assert body == {"status": "idle", "machine_id": "mac_X"}  # running → idle


@pytest.mark.asyncio
async def test_migrate_reports_paused_state(monkeypatch):
    from puffo_agent.portal.control import link

    _stub_http(monkeypatch)
    monkeypatch.setattr(link, "current_machine_id", lambda: "mac_X")
    monkeypatch.setattr(link, "discover_agents", lambda: ["p"])
    monkeypatch.setattr(link, "is_owner", lambda aid, root: True)
    monkeypatch.setattr(link.AgentConfig, "load", lambda aid: _fake_cfg("p", state="paused"))

    await link.migrate_owned_agents("ROOT")
    assert _RecordingHttp.posts[0][2]["status"] == "paused"


@pytest.mark.asyncio
async def test_migrate_noop_when_unlinked(monkeypatch):
    from puffo_agent.portal.control import link

    monkeypatch.setattr(link, "current_machine_id", lambda: None)
    assert await link.migrate_owned_agents("ROOT") == 0


@pytest.mark.asyncio
async def test_migrate_survives_one_agent_failing(monkeypatch):
    from puffo_agent.portal.control import link

    class _Boom(_RecordingHttp):
        async def post(self, path, body):
            if self._url == "https://bad":
                raise HttpError(401, "chain validation failed")
            await super().post(path, body)

    _stub_http(monkeypatch, _Boom)
    monkeypatch.setattr(link, "current_machine_id", lambda: "mac_X")
    monkeypatch.setattr(link, "discover_agents", lambda: ["bad", "good"])
    monkeypatch.setattr(link, "is_owner", lambda aid, root: True)
    cfgs = {
        "bad": _fake_cfg("bad", server_url="https://bad"),
        "good": _fake_cfg("good", server_url="https://good"),
    }
    monkeypatch.setattr(link.AgentConfig, "load", lambda aid: cfgs[aid])

    n = await link.migrate_owned_agents("ROOT")
    assert n == 1  # good still reported despite bad raising
    assert [p[0] for p in _RecordingHttp.posts] == ["https://good"]


# ── F3: link auto-starts the daemon ─────────────────────────────────

def _link_ns():
    return argparse.Namespace(name=None, server_url="https://x", not_open=True)


def test_cmd_link_autostarts_when_daemon_down(monkeypatch):
    from puffo_agent.portal import cli
    from puffo_agent.portal import background as bg
    from puffo_agent.portal.control import link

    spawned = []
    monkeypatch.setattr(cli, "is_daemon_alive", lambda: False)
    monkeypatch.setattr(bg, "spawn_background", lambda **kw: spawned.append(kw) or 0)

    async def _fake_run_link(url, name, open_browser=True):
        return 0

    monkeypatch.setattr(link, "run_link", _fake_run_link)
    assert cli.cmd_link(_link_ns()) == 0
    assert spawned == [{}]


def test_cmd_link_skips_autostart_when_daemon_running(monkeypatch):
    from puffo_agent.portal import cli
    from puffo_agent.portal import background as bg
    from puffo_agent.portal.control import link

    spawned = []
    monkeypatch.setattr(cli, "is_daemon_alive", lambda: True)
    monkeypatch.setattr(bg, "spawn_background", lambda **kw: spawned.append(kw) or 0)

    async def _fake_run_link(url, name, open_browser=True):
        return 0

    monkeypatch.setattr(link, "run_link", _fake_run_link)
    cli.cmd_link(_link_ns())
    assert spawned == []


# ── lifecycle give-up (daemon._report_lifecycle) ────────────────────

def _stub_lifecycle_http(monkeypatch, raises):
    class _H:
        def __init__(self, *a):
            pass

        async def post(self, *a):
            if raises:
                raise raises

        async def close(self):
            pass

    monkeypatch.setattr("puffo_agent.crypto.http_client.PuffoCoreHttpClient", _H)
    monkeypatch.setattr(
        "puffo_agent.crypto.keystore.KeyStore",
        types.SimpleNamespace(for_agent=lambda _id: object()),
    )
    monkeypatch.setattr("puffo_agent.portal.control.store.current_machine_id", lambda: "mac_X")


@pytest.mark.asyncio
async def test_report_lifecycle_settled_on_4xx(monkeypatch):
    from puffo_agent.portal import daemon

    _stub_lifecycle_http(monkeypatch, HttpError(401, "chain validation failed"))
    # 4xx is permanent for this (agent, server) → settled so the caller stops.
    assert await daemon._report_lifecycle(_fake_cfg(), "paused") is True


@pytest.mark.asyncio
async def test_report_lifecycle_retries_on_5xx(monkeypatch):
    from puffo_agent.portal import daemon

    _stub_lifecycle_http(monkeypatch, HttpError(503, "upstream down"))
    assert await daemon._report_lifecycle(_fake_cfg(), "paused") is False


# ── /v1/info machine_id ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_info_includes_machine_id(monkeypatch):
    from puffo_agent.portal.api import handlers

    monkeypatch.setattr("puffo_agent.portal.control.store.current_machine_id", lambda: "mac_INFO")
    monkeypatch.setattr(handlers, "load_pairing", lambda: None)
    monkeypatch.setattr(handlers, "discover_agents", lambda: [])

    resp = await handlers.info(None)
    data = json.loads(resp.body)
    assert data["machine_id"] == "mac_INFO"


# ── machine unlink CLI wiring ───────────────────────────────────────

def test_cmd_unlink_passes_operator_and_server(monkeypatch):
    from puffo_agent.portal import cli
    from puffo_agent.portal.control import link

    seen = {}

    async def _fake_unlink(slug, expected_server_url=None):
        seen["slug"] = slug
        seen["server"] = expected_server_url
        return 0

    monkeypatch.setattr(link, "run_unlink", _fake_unlink)
    rc = cli.cmd_unlink(argparse.Namespace(operator="op-x", server_url="https://s"))
    assert rc == 0
    assert seen == {"slug": "op-x", "server": "https://s"}


@pytest.mark.asyncio
async def test_run_unlink_refuses_server_mismatch(monkeypatch):
    from puffo_agent.portal.control import link

    monkeypatch.setattr(
        link, "get_pairing",
        lambda slug: types.SimpleNamespace(server_url="https://prod", operator_root_pubkey="R"),
    )
    rc = await link.run_unlink("op", expected_server_url="https://staging")
    assert rc == 2  # paired on a different server → refused, nothing torn down


# ── friendly device name ─────────────────────────────────────────────

def test_compose_device_name_cleans_sku_and_placeholders():
    from puffo_agent.portal.control.link import _compose_device_name

    assert _compose_device_name("Razer", "Blade 14 - RZ09-0370") == "Razer Blade 14"
    # Model already carries the maker → don't double it up.
    assert _compose_device_name("Dell Inc.", "Dell Inc. XPS 13") == "Dell Inc. XPS 13"
    # OEM placeholder strings are unusable.
    assert _compose_device_name("System manufacturer", "System Product Name") is None
    assert _compose_device_name("Razer", "To be filled by O.E.M.") is None
    # Maker-less but a real model still works.
    assert _compose_device_name("", "MacBookPro18,3") == "MacBookPro18,3"


def test_friendly_device_name_falls_back_to_hostname(monkeypatch):
    from puffo_agent.portal.control import link

    monkeypatch.setattr(link, "_windows_device_name", lambda: None)
    monkeypatch.setattr(link.socket, "gethostname", lambda: "fallback-host")
    assert link.friendly_device_name() == "fallback-host"
