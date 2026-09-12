"""PUF-430 — on archive (and delete) the daemon leaves every space it
still belongs to, so the agent stops showing up in channel rosters and
the @-mention picker. Covers the fanout itself, the owner-space skip,
and the ordering contract with the device revoke (leave first, always —
the revoke 401s the credential that signs the leave)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from _portal_support import isolated_home
from test_import_module import _seed_source_agent

pytestmark = pytest.mark.asyncio

SLUG = "alpha-bot"
AGENT_ID = "alpha"


def _make_mock_app(state):
    app = web.Application()

    async def subkeys(request):
        state["calls"].append(("POST", "/devices/subkeys"))
        return web.json_response({"ok": True})

    async def spaces(request):
        state["calls"].append(("GET", "/spaces"))
        if state.get("spaces_status"):
            return web.json_response({"error": "nope"}, status=state["spaces_status"])
        return web.json_response({"spaces": state["spaces"]})

    async def events(request):
        body = await request.json()
        state["calls"].append(("POST", "/spaces/events"))
        state["posted"].append(body)
        space_id = body["space_id"]
        if space_id in state["reject_owner"]:
            return web.json_response(
                {"error": "owner must transfer ownership before leaving"},
                status=403,
            )
        if space_id in state["reject_status"]:
            return web.json_response(
                {"error": "rejected"}, status=state["reject_status"][space_id]
            )
        if space_id in state["fail"]:
            return web.json_response({"error": "induced failure"}, status=500)
        return web.json_response({"ok": True, "applied": 1})

    async def revoke(request):
        state["calls"].append(("POST", "/revoke"))
        return web.json_response({"ok": True})

    app.router.add_post("/devices/subkeys", subkeys)
    app.router.add_get("/spaces", spaces)
    app.router.add_post("/spaces/events", events)
    app.router.add_post("/devices/{device_id}/revoke", revoke)
    return app


@pytest_asyncio.fixture
async def mock_server():
    state = {
        "calls": [],
        "posted": [],
        "spaces": [],
        "fail": set(),
        "reject_owner": set(),
        "reject_status": {},
        "spaces_status": 0,
    }
    server = TestServer(_make_mock_app(state))
    await server.start_server()
    try:
        yield server, state
    finally:
        await server.close()


@pytest.fixture(autouse=True)
def fresh_home():
    isolated_home()
    yield


def _seed(server, *, spaces: list[dict]) -> tuple[Path, dict]:
    """Materialise an agent dir with a real keystore identity pointed at
    the mock server, and arm the server's ``GET /spaces`` response."""
    import os

    url = str(server.make_url("/")).rstrip("/")
    home = os.environ["PUFFO_AGENT_HOME"]
    _seed_source_agent(home, AGENT_ID, SLUG, url)
    return Path(home) / "agents" / AGENT_ID, {"url": url, "spaces": spaces}


def _home() -> str:
    import os

    return os.environ["PUFFO_AGENT_HOME"]


def _member(space_id: str) -> dict:
    return {"space_id": space_id, "role": "member", "joined_at": 0}


def _owner(space_id: str) -> dict:
    return {"space_id": space_id, "role": "owner", "joined_at": 0}


# ────────────────────────────────────────────────────────────────────
# The fanout
# ────────────────────────────────────────────────────────────────────


async def test_leave_all_spaces_signs_one_leave_per_space(mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, seeded = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_one"), _member("sp_two")]

    result = await imp.leave_all_spaces(adir, slug=SLUG)

    assert result.left == ["sp_one", "sp_two"]
    assert result.failed == []
    assert result.skipped_owner == []
    assert [b["space_id"] for b in state["posted"]] == ["sp_one", "sp_two"]
    for body in state["posted"]:
        assert len(body["events"]) == 1
        event = body["events"][0]
        assert event["kind"] == "leave_space"
        assert event["signer_slug"] == SLUG
        assert event["signer_subkey_id"]
        assert event["signature"]
        assert event["payload"]["space_id"] == body["space_id"]
        assert event["payload"]["effective_from"] > 0
        assert event["payload"]["nonce"]


async def test_leave_all_spaces_no_memberships_posts_nothing(mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = []

    result = await imp.leave_all_spaces(adir, slug=SLUG)

    assert result.left == []
    assert state["posted"] == []


async def test_owned_space_is_skipped_without_a_round_trip(mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_owner("sp_owned"), _member("sp_joined")]

    result = await imp.leave_all_spaces(adir, slug=SLUG)

    assert result.skipped_owner == ["sp_owned"]
    assert result.left == ["sp_joined"]
    assert result.failed == []
    # Pre-filtered on the role from GET /spaces — the owned space never
    # reaches the server.
    assert [b["space_id"] for b in state["posted"]] == ["sp_joined"]


async def test_server_owner_rejection_is_skipped_not_failed(mock_server):
    """Role in GET /spaces can be stale; the server's own rejection is
    equally permanent and must not defer the revoke."""
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_stale"), _member("sp_ok")]
    state["reject_owner"] = {"sp_stale"}

    result = await imp.leave_all_spaces(adir, slug=SLUG)

    assert result.skipped_owner == ["sp_stale"]
    assert result.left == ["sp_ok"]
    assert result.failed == []


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_non_403_4xx_is_permanent_and_never_deferred(mock_server, status):
    """AC #4 — the server repeating an identical rejection can't be fixed
    by retrying, so it must not park the revoke forever."""
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_rejected"), _member("sp_ok")]
    state["reject_status"] = {"sp_rejected": status}

    result = await imp.leave_all_spaces(adir, slug=SLUG)

    assert result.skipped_permanent == ["sp_rejected"]
    assert result.failed == []
    assert result.left == ["sp_ok"]


@pytest.mark.parametrize("status", [403, 429])
async def test_403_and_429_stay_retryable(mock_server, status):
    """A non-owner 403 can be an auth blip and 429 is explicitly a
    rate-limit — both are worth another pass."""
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_blipped")]
    state["reject_status"] = {"sp_blipped": status}

    result = await imp.leave_all_spaces(adir, slug=SLUG)

    assert result.failed == ["sp_blipped"]
    assert result.skipped_permanent == []


async def test_permanent_rejection_does_not_defer_the_revoke(mock_server):
    from puffo_agent.portal import daemon as dmn
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_rejected")]
    state["reject_status"] = {"sp_rejected": 400}

    proceed = await dmn._leave_spaces_before_revoke(AGENT_ID, adir, slug=SLUG)

    assert proceed is True
    assert not imp.archived_pending_leave_path(adir).exists()
    assert not imp.archived_pending_revoke_path(adir).exists()


async def test_one_failing_space_does_not_abort_the_others(mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_bad"), _member("sp_good")]
    state["fail"] = {"sp_bad"}

    result = await imp.leave_all_spaces(adir, slug=SLUG)

    assert result.failed == ["sp_bad"]
    assert result.left == ["sp_good"]
    assert "500" in result.last_error


async def test_enumerate_failure_raises(mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces_status"] = 503

    with pytest.raises(Exception):
        await imp.leave_all_spaces(adir, slug=SLUG)
    assert state["posted"] == []


# ────────────────────────────────────────────────────────────────────
# Daemon wiring: leave before revoke, defer on transient failure
# ────────────────────────────────────────────────────────────────────


async def test_daemon_helper_returns_true_and_writes_no_marker(mock_server):
    from puffo_agent.portal import daemon as dmn
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_one"), _owner("sp_owned")]

    proceed = await dmn._leave_spaces_before_revoke(AGENT_ID, adir, slug=SLUG)

    assert proceed is True
    assert not imp.archived_pending_leave_path(adir).exists()
    assert not imp.archived_pending_revoke_path(adir).exists()


async def test_transient_failure_defers_revoke_and_marks_both(mock_server):
    from puffo_agent.portal import daemon as dmn
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_bad")]
    state["fail"] = {"sp_bad"}

    proceed = await dmn._leave_spaces_before_revoke(AGENT_ID, adir, slug=SLUG)

    assert proceed is False
    leave_marker = json.loads(
        imp.archived_pending_leave_path(adir).read_text(encoding="utf-8")
    )
    assert leave_marker["kind"] == "archive_leave_spaces"
    assert leave_marker["slug"] == SLUG
    assert leave_marker["space_ids"] == ["sp_bad"]
    # The revoke marker is what gets the sweep to finish the job after
    # the leave lands — without it the deferred revoke never happens.
    revoke_marker = json.loads(
        imp.archived_pending_revoke_path(adir).read_text(encoding="utf-8")
    )
    assert "deferred behind pending space-leave" in revoke_marker["last_error"]


async def test_enumerate_failure_defers_revoke(mock_server):
    from puffo_agent.portal import daemon as dmn
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces_status"] = 503

    proceed = await dmn._leave_spaces_before_revoke(AGENT_ID, adir, slug=SLUG)

    assert proceed is False
    marker = json.loads(
        imp.archived_pending_leave_path(adir).read_text(encoding="utf-8")
    )
    # Nothing was enumerated, so the sweep must re-query rather than
    # trust a list we never obtained.
    assert marker["space_ids"] == []


class _FakeDaemon:
    def __init__(self):
        self.stopped = []

    async def _stop_worker(self, agent_id):
        self.stopped.append(agent_id)


async def _run_flag(monkeypatch, mock_server, which: str, order: list[str]):
    """Drive the real ``_archive_on_flag`` / ``_delete_on_flag`` against
    the seeded agent dir, recording the leave/revoke call order."""
    from puffo_agent.portal import daemon as dmn
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    _seed(server, spaces=[])
    state["spaces"] = [_member("sp_one")]

    async def no_report(cfg, status):
        return True

    real_leave = imp.leave_all_spaces

    async def spy_leave(archived_dir, *, slug):
        order.append("leave")
        return await real_leave(archived_dir, slug=slug)

    async def spy_revoke(archived_dir, *, slug):
        order.append("revoke")

    monkeypatch.setattr(dmn, "_report_lifecycle", no_report)
    monkeypatch.setattr(imp, "leave_all_spaces", spy_leave)
    monkeypatch.setattr(imp, "revoke_archived_device", spy_revoke)

    fake = _FakeDaemon()
    handler = (
        dmn.Daemon._archive_on_flag if which == "archive" else dmn.Daemon._delete_on_flag
    )
    await handler(fake, AGENT_ID)
    return state


async def test_archive_on_flag_leaves_spaces_before_revoking(monkeypatch, mock_server):
    order: list[str] = []
    state = await _run_flag(monkeypatch, mock_server, "archive", order)

    assert order == ["leave", "revoke"]
    assert [b["space_id"] for b in state["posted"]] == ["sp_one"]


async def test_delete_on_flag_leaves_spaces_before_revoking(monkeypatch, mock_server):
    """A deleted agent ghosts the roster exactly like an archived one."""
    order: list[str] = []
    state = await _run_flag(monkeypatch, mock_server, "delete", order)

    assert order == ["leave", "revoke"]
    assert [b["space_id"] for b in state["posted"]] == ["sp_one"]


async def test_cli_archive_leaves_spaces_before_revoking(monkeypatch, mock_server):
    """`puffo-agent agent archive <id>` reaches the same end state as the
    archive flag — an operator archiving from the CLI would otherwise
    reproduce the original bug through a different trigger."""
    import argparse
    import asyncio

    from puffo_agent.portal import cli
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    _seed(server, spaces=[])
    state["spaces"] = [_member("sp_one")]

    order: list[str] = []
    real_leave = imp.leave_all_spaces

    async def spy_leave(archived_dir, *, slug):
        order.append("leave")
        return await real_leave(archived_dir, slug=slug)

    async def spy_revoke(archived_dir, *, slug):
        order.append("revoke")

    monkeypatch.setattr(imp, "leave_all_spaces", spy_leave)
    monkeypatch.setattr(imp, "revoke_archived_device", spy_revoke)

    # cmd_agent_archive is sync and owns its own asyncio.run, so it has to
    # run off-loop — otherwise it can't nest inside the test's loop, and
    # that loop is what serves the mock server.
    rc = await asyncio.get_running_loop().run_in_executor(
        None, cli.cmd_agent_archive, argparse.Namespace(id=AGENT_ID)
    )

    assert rc == 0
    assert order == ["leave", "revoke"]
    assert [b["space_id"] for b in state["posted"]] == ["sp_one"]


async def test_cli_archive_defers_the_revoke_when_a_leave_fails(
    monkeypatch, mock_server, capsys
):
    """The CLI honours the same defer contract as the daemon — revoking
    here would 401 the credential the queued leave still needs."""
    import argparse
    import asyncio

    from puffo_agent.portal import cli
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    _seed(server, spaces=[])
    state["spaces"] = [_member("sp_bad")]
    state["fail"] = {"sp_bad"}

    revoked: list[str] = []

    async def spy_revoke(archived_dir, *, slug):
        revoked.append(slug)

    monkeypatch.setattr(imp, "revoke_archived_device", spy_revoke)

    rc = await asyncio.get_running_loop().run_in_executor(
        None, cli.cmd_agent_archive, argparse.Namespace(id=AGENT_ID)
    )

    assert rc == 0
    assert revoked == []
    dest = next(
        d for d in (Path(_home()) / "archived").iterdir() if d.is_dir()
    )
    assert imp.archived_pending_leave_path(dest).exists()
    assert imp.archived_pending_revoke_path(dest).exists()
    assert "revoke deferred" in capsys.readouterr().err


# ────────────────────────────────────────────────────────────────────
# Retry sweep
# ────────────────────────────────────────────────────────────────────


def _archived_copy(adir: Path) -> Path:
    """Move the seeded agent dir under ``archived/`` the way the archive
    flow does, so the sweep can find it."""
    import shutil

    from puffo_agent.portal.state import archived_dir

    dest = archived_dir() / f"{AGENT_ID}-ws-20260811-000000"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(adir), str(dest))
    return dest


async def test_sweep_retries_leave_then_revoke(monkeypatch, mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_one")]
    dest = _archived_copy(adir)
    imp.write_archived_pending_leave(
        dest, slug=SLUG, space_ids=["sp_one"], last_error="boom"
    )
    imp.write_archived_pending_revoke(
        dest,
        server_url=str(server.make_url("/")).rstrip("/"),
        slug=SLUG,
        device_id="dev_x",
        last_error="deferred",
    )

    order: list[str] = []
    real_leave = imp.leave_all_spaces

    async def spy_leave(archived_dir, *, slug):
        order.append("leave")
        return await real_leave(archived_dir, slug=slug)

    async def spy_revoke(*args, **kwargs):
        order.append("revoke")

    monkeypatch.setattr(imp, "leave_all_spaces", spy_leave)
    monkeypatch.setattr(imp, "self_revoke_device", spy_revoke)

    retried = await imp.sweep_archived_pending_revokes()

    assert order == ["leave", "revoke"]
    assert retried == 1
    assert not imp.archived_pending_leave_path(dest).exists()
    assert not imp.archived_pending_revoke_path(dest).exists()


async def test_sweep_holds_the_revoke_while_the_leave_fails(monkeypatch, mock_server):
    """Revoking first would 401 the credential the leave needs, so a
    still-failing leave must keep the revoke parked."""
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_bad")]
    state["fail"] = {"sp_bad"}
    dest = _archived_copy(adir)
    imp.write_archived_pending_leave(
        dest, slug=SLUG, space_ids=["sp_bad"], last_error="boom"
    )
    imp.write_archived_pending_revoke(
        dest,
        server_url=str(server.make_url("/")).rstrip("/"),
        slug=SLUG,
        device_id="dev_x",
        last_error="deferred",
    )

    revoked: list[str] = []

    async def spy_revoke(*args, **kwargs):
        revoked.append("revoke")

    monkeypatch.setattr(imp, "self_revoke_device", spy_revoke)

    retried = await imp.sweep_archived_pending_revokes()

    assert revoked == []
    assert retried == 0
    assert imp.archived_pending_leave_path(dest).exists()
    assert imp.archived_pending_revoke_path(dest).exists()


async def test_sweep_picks_up_a_leave_only_marker(monkeypatch, mock_server):
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_one")]
    dest = _archived_copy(adir)
    imp.write_archived_pending_leave(
        dest, slug=SLUG, space_ids=[], last_error="boom"
    )

    await imp.sweep_archived_pending_revokes()

    assert [b["space_id"] for b in state["posted"]] == ["sp_one"]
    assert not imp.archived_pending_leave_path(dest).exists()


async def test_sweep_retry_requeries_instead_of_trusting_the_marker(mock_server):
    """AC #6 — a space left on the prior pass is gone from GET /spaces, so
    the retry must not re-fire for it just because the marker names it."""
    from puffo_agent.portal import import_agents as imp

    server, state = mock_server
    adir, _ = _seed(server, spaces=[])
    state["spaces"] = [_member("sp_still_in")]
    dest = _archived_copy(adir)
    imp.write_archived_pending_leave(
        dest,
        slug=SLUG,
        space_ids=["sp_already_left", "sp_still_in"],
        last_error="boom",
    )

    await imp.sweep_archived_pending_revokes()

    assert [b["space_id"] for b in state["posted"]] == ["sp_still_in"]


async def test_sweep_marks_an_unparseable_leave_marker_broken(mock_server):
    from puffo_agent.portal import import_agents as imp

    server, _ = mock_server
    adir, _ = _seed(server, spaces=[])
    dest = _archived_copy(adir)
    marker = imp.archived_pending_leave_path(dest)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("not json", encoding="utf-8")

    await imp.sweep_archived_pending_revokes()

    assert not marker.exists()
    assert marker.with_suffix(marker.suffix + ".broken").exists()
