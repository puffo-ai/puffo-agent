"""Zoom Phase 1 (read-only) against a fake Zoom: projection both ways for all
four read tools, the one refresh, the 4711 insufficient-scope split that never
refreshes, pagination consumed (not merely advertised), and the no-leak
sentinels — including the recordings scrub that drops the bearer links / PII
while keeping the resource handles."""

import json
import logging
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from puffo_agent.portal import zoom
from puffo_agent.portal.oauth_connector import ConnectorError
from tests.test_oauth_connector import FRESH, TOKEN, Routed, Scripted, Wallet

API = "https://zoom.test/v2"

USER = {"display_name": "Ada", "email": "ada@x", "type": 2,
        "pmi": 1234567890, "timezone": "America/New_York"}
MEETING = {"id": "87", "topic": "Standup", "start_time": "2026-03-01T09:00:00Z",
           "duration": 30, "join_url": "https://zoom.us/j/87"}


def wallet(**kw):
    return Wallet(zoom.CONNECTOR, accounts=kw.pop("accounts", ((0, "work-zoom"),)), **kw)


def _clean(payload, *secrets):
    blob = json.dumps(payload, default=str)
    return not any(s in blob for s in secrets)


def test_the_types_are_the_ones_puffo_server_provisions():
    """Literal server names — daemon half of the credential-type contract."""
    assert (zoom.CONNECTOR.rt_type, zoom.CONNECTOR.at_type) == (
        "PUFFO_ZOOM_OAUTH_v1", "PUFFO_ZOOM_OAUTH_AT_v1")
    assert zoom.CONNECTOR.name == "zoom"


@pytest.mark.asyncio
async def test_whoami_selects_the_summary_fields():
    api = Routed(API, {"/users/me": (200, {**USER, "phone_number": "+1", "role_name": "Owner"})})
    out = await zoom.whoami(wallet(), transport=api, api=API)
    # exactly the five spec fields — the extra body keys are dropped, not echoed
    assert out == {"display_name": "Ada", "email": "ada@x", "type": 2,
                   "pmi": 1234567890, "timezone": "America/New_York"}
    (method, url, headers, body), = api.requests
    assert method == "GET" and url == f"{API}/users/me" and body is None
    assert headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_whoami_reverse_absent_fields_are_empty_not_fabricated():
    out = await zoom.whoami(wallet(), transport=Routed(API, {"/users/me": (200, {})}), api=API)
    assert out == {"display_name": "", "email": "", "type": None, "pmi": None, "timezone": ""}


@pytest.mark.asyncio
async def test_get_meeting_shapes_the_summary_and_quotes_the_id():
    api = Routed(API, {"/meetings/87": (200, {**MEETING, "password": "s3cret", "host_id": "h1"})})
    out = await zoom.get_meeting(wallet(), meeting_id=" 87 ", transport=api, api=API)
    assert out == {"topic": "Standup", "start": "2026-03-01T09:00:00Z",
                   "duration": 30, "join_url": "https://zoom.us/j/87"}
    (method, url, _, _), = api.requests
    assert method == "GET" and url == f"{API}/meetings/87"  # trimmed + path-quoted


@pytest.mark.asyncio
async def test_get_meeting_reverse_defaults_and_requires_the_id():
    out = await zoom.get_meeting(wallet(), meeting_id="9",
                                 transport=Routed(API, {"/meetings/9": (200, {})}), api=API)
    assert out == {"topic": "", "start": "", "duration": None, "join_url": ""}
    bad = Scripted()
    with pytest.raises(ConnectorError) as exc:
        await zoom.get_meeting(wallet(), meeting_id=" ", transport=bad, api=API)
    assert exc.value.code == "bad_request" and bad.requests == []  # refused before any HTTP


REC_P1 = {"meetings": [{"id": 111, "uuid": "u1", "topic": "Wk1",
                        "start_time": "2026-01-05T10:00:00Z", "duration": 60,
                        "recording_count": 2, "total_size": 1024}],
          "next_page_token": "p2"}
REC_P2 = {"meetings": [{"id": 222, "uuid": "u2", "topic": "Wk2",
                        "start_time": "2026-01-12T10:00:00Z", "duration": 30,
                        "recording_count": 1, "total_size": 512}],
          "next_page_token": ""}


@pytest.mark.asyncio
async def test_list_recordings_shapes_rows_pins_window_and_fixed_page_size():
    api = Routed(API, {"/users/me/recordings": (200, REC_P1)})
    out = await zoom.list_recordings(wallet(), from_date=" 2026-01-01 ", to_date="2026-01-31",
                                     transport=api, api=API)
    assert out == {"recordings": [{"meeting_id": "111", "uuid": "u1", "topic": "Wk1",
                                   "start_time": "2026-01-05T10:00:00Z", "duration": 60,
                                   "recording_count": 2, "total_size": 1024}],
                   "count": 1, "next_page_token": "p2"}
    q = parse_qs(urlparse(api.requests[0][1]).query)
    assert q["from"] == ["2026-01-01"] and q["to"] == ["2026-01-31"]  # trimmed
    assert q["page_size"] == [str(zoom.RECORDINGS_PAGE_SIZE)]  # fixed, not caller-tunable
    assert "next_page_token" not in q  # no cursor on the first page


@pytest.mark.asyncio
async def test_list_recordings_pagination_is_consumed_not_just_advertised():
    """测试姬 ① oracle: the only non-vacuous proof of pagination is that the
    returned token, fed back, actually fetches the *next* page — not merely that
    a token comes back (the #471 Calendar dead-cursor lesson)."""
    api = Routed(API, {"/users/me/recordings": [(200, REC_P1), (200, REC_P2)]})
    page1 = await zoom.list_recordings(wallet(), from_date="2026-01-01", to_date="2026-01-31",
                                       transport=api, api=API)
    assert page1["next_page_token"] == "p2" and page1["recordings"][0]["meeting_id"] == "111"
    # feed the cursor back — the SECOND request must carry it, and return page 2
    page2 = await zoom.list_recordings(wallet(), from_date="2026-01-01", to_date="2026-01-31",
                                       page_token=page1["next_page_token"], transport=api, api=API)
    q2 = parse_qs(urlparse(api.requests[1][1]).query)
    assert q2["next_page_token"] == ["p2"]                    # cursor consumed, not just echoed
    assert page2["recordings"][0]["meeting_id"] == "222"      # genuinely the next page
    assert page2["next_page_token"] == ""


@pytest.mark.asyncio
async def test_list_recordings_reverse_defaults_and_requires_the_window():
    api = Routed(API, {"/users/me/recordings": (200, {"meetings": [{"id": 9}, "junk"]})})
    out = await zoom.list_recordings(wallet(), from_date="a", to_date="b", transport=api, api=API)
    assert out["recordings"] == [{"meeting_id": "9", "uuid": "", "topic": "", "start_time": "",
                                  "duration": None, "recording_count": None, "total_size": None}]
    assert out["count"] == 1 and out["next_page_token"] == ""
    bad = Scripted()
    for kw in ({"from_date": " ", "to_date": "b"}, {"from_date": "a", "to_date": " "}):
        with pytest.raises(ConnectorError) as exc:
            await zoom.list_recordings(wallet(), transport=bad, api=API, **kw)
        assert exc.value.code == "bad_request"
    assert bad.requests == []  # refused before any HTTP


@pytest.mark.asyncio
async def test_get_meeting_recordings_lists_file_metadata_without_download_links():
    body = {"recording_files": [{
        "id": "rf1", "file_type": "MP4", "file_extension": "MP4", "file_size": 2048,
        "recording_type": "shared_screen", "recording_start": "2026-01-05T10:00:00Z",
        "recording_end": "2026-01-05T11:00:00Z",
        "download_url": "https://zoom.us/rec/dl/xyz", "play_url": "https://zoom.us/rec/play/xyz",
    }], "password": "rec-pass-7f3a", "host_email": "host-7f3a@x"}
    api = Routed(API, {"/meetings/111/recordings": (200, body)})
    out = await zoom.get_meeting_recordings(wallet(), meeting_id=" 111 ", transport=api, api=API)
    assert out == {"recording_files": [{
        "recording_id": "rf1", "file_type": "MP4", "file_extension": "MP4", "file_size": 2048,
        "recording_type": "shared_screen", "recording_start": "2026-01-05T10:00:00Z",
        "recording_end": "2026-01-05T11:00:00Z"}], "count": 1}
    # none of the bearer links / pii surface in the projection
    assert _clean(out, "https://zoom.us/rec/dl/xyz", "https://zoom.us/rec/play/xyz",
                  "rec-pass-7f3a", "host-7f3a@x")
    (method, url, _, _), = api.requests
    assert method == "GET" and url == f"{API}/meetings/111/recordings"  # trimmed + path-quoted
    bad = Scripted()
    with pytest.raises(ConnectorError) as exc:
        await zoom.get_meeting_recordings(wallet(), meeting_id=" ", transport=bad, api=API)
    assert exc.value.code == "bad_request" and bad.requests == []


@pytest.mark.asyncio
async def test_recordings_never_leak_links_or_pii_but_keep_the_handles(caplog):
    """§5.7 for recordings (测试姬 ②): the bearer links / password / host_email
    are planted into the raw bodies; a projection that echoed them (or a log that
    embedded the raw body) would leak. The resource handles meeting_id / uuid /
    recording_id are the opposite — they MUST survive (they are the next call's
    input), so this also asserts they are kept.

    Projection ∧ log are real detectors, each with a positive control. The error
    face is empty by construction, NOT a detector — the assertion is KEPT as a
    documented latent tripwire (not deleted): empty today, it only gains teeth if
    a future change breaks the property below. Construction attempt (per
    [[pinned-observable-makes-assertion-empty]], 测试姬 ii, object-verified by Bob
    §5 + 测试姬): a recording payload can reach a raised ConnectorError only through
    its message, but every raise message is sourced from static text or
    ``_error_detail(data)`` — which reads solely the error body's own
    ``{code, message[:200]}``. Recording payloads live in the 2xx success body
    (``return data``, before any error path) and ``_error_detail`` never reads
    them, so today no input routes a recording field into str(exc)."""
    caplog.set_level(logging.DEBUG)
    meeting = {"id": 111, "uuid": "u1", "topic": "Wk1", "host_email": TOKEN, "password": FRESH}
    rfile = {"id": "rf1", "file_type": "MP4", "download_url": TOKEN, "play_url": FRESH}
    out_list = await zoom.list_recordings(
        wallet(), from_date="a", to_date="b",
        transport=Routed(API, {"/users/me/recordings": (200, {"meetings": [meeting]})}), api=API)
    out_files = await zoom.get_meeting_recordings(
        wallet(), meeting_id="111",
        transport=Routed(API, {"/meetings/111/recordings": (200, {"recording_files": [rfile]})}),
        api=API)
    with pytest.raises(ConnectorError) as exc:
        await zoom.list_recordings(wallet(), from_date="a", to_date="b",
                                   transport=Scripted((404, {"code": 3001, "message": "x"})), api=API)
    for secret in (TOKEN, FRESH):
        assert _clean(out_list, secret) and _clean(out_files, secret)  # projection scrub (real)
        assert secret not in caplog.text                               # log scrub (real)
        assert secret not in str(exc.value)  # error face: by-construction-empty, kept as tripwire
    # the legitimate handles survive (not scrubbed) — the positive half
    assert out_list["recordings"][0]["meeting_id"] == "111"
    assert out_list["recordings"][0]["uuid"] == "u1"
    assert out_files["recording_files"][0]["recording_id"] == "rf1"
    assert not _clean({"leaked": TOKEN}, TOKEN)  # the sentinels are not vacuous


@pytest.mark.asyncio
async def test_a_bad_token_is_refreshed_once_then_named_as_the_reason():
    api, w = Scripted((401, {}), (200, USER)), wallet()
    assert (await zoom.whoami(w, transport=api, api=API))["email"] == "ada@x"
    assert [h["authorization"] for _, _, h, _ in api.requests] == [f"Bearer {TOKEN}", f"Bearer {FRESH}"]
    assert w.refreshes == 1 and len(api.requests) == 2  # §5.8 positive control: 401 -> refresh
    with pytest.raises(ConnectorError) as exc:
        await zoom.whoami(wallet(), transport=Scripted((401, {}), (401, {})), api=API)
    assert exc.value.code == "not_authorized" and "reconnect" in str(exc.value)


@pytest.mark.asyncio
async def test_a_4711_is_insufficient_scope_passes_the_name_through_and_never_refreshes():
    """§5.8 / invariant 8: a 4711 keeps the same scope on refresh, so it must not
    trigger one — refresh-count == 0 and a single HTTP attempt. The named granular
    scope (a scope enum, not a secret) is passed through to the guidance."""
    w = wallet()
    body = {"code": 4711, "message": "Invalid access token, does not contain scopes:"
                                     "[meeting:read:meeting]"}
    api = Scripted((400, body))
    with pytest.raises(ConnectorError) as exc:
        await zoom.get_meeting(w, meeting_id="87", transport=api, api=API)
    assert exc.value.code == "insufficient_scope"
    assert "meeting:read:meeting" in str(exc.value)  # scope name reaches the guidance
    assert w.refreshes == 0 and len(api.requests) == 1  # never refreshed, single attempt


@pytest.mark.asyncio
async def test_4711_counting_control_single_attempt_no_refresh():
    w, api = wallet(), Scripted((400, {"code": 4711, "message": "scopes:[x]"}))
    with pytest.raises(ConnectorError):
        await zoom.whoami(w, transport=api, api=API)
    assert w.refreshes == 0 and len(api.requests) == 1


@pytest.mark.asyncio
async def test_404_is_not_found_other_codes_carry_their_code_and_bad_body_is_bad_reply():
    with pytest.raises(ConnectorError) as exc:
        await zoom.get_meeting(wallet(), meeting_id="87",
                               transport=Scripted((404, {"code": 3001, "message": "not found"})), api=API)
    assert exc.value.code == "not_found"
    with pytest.raises(ConnectorError) as exc:
        await zoom.get_meeting(wallet(), meeting_id="87",
                               transport=Scripted((400, {"code": 300, "message": "bad"})), api=API)
    assert exc.value.code == "zoom_300" and "bad" in str(exc.value)
    with pytest.raises(ConnectorError) as exc:
        await zoom.whoami(wallet(), transport=Scripted((200, b"<html>")), api=API)
    assert exc.value.code == "bad_reply"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, kind", [
    (aiohttp.ServerDisconnectedError(), "lost"), ((503, {}), "lost"), ((429, {}), "lost")],
    ids=["disconnect", "5xx", "429-read"])
async def test_reads_treat_disconnect_5xx_and_429_as_a_lost_answer(answer, kind):
    from puffo_agent.portal.oauth_connector import ConnectorLostAnswer
    with pytest.raises(ConnectorLostAnswer):
        await zoom.whoami(wallet(), transport=Scripted(answer), api=API)


@pytest.mark.asyncio
async def test_the_token_never_reaches_the_projection_log_or_error(caplog):
    """§5.7 scrub sentinel with teeth: the token is planted into NON-projected
    body fields (the user object's phone/role), so a projection that echoed the
    raw body rather than selecting fields would leak it. Plus log/error scrub and
    a positive control."""
    caplog.set_level(logging.DEBUG)
    bugged = {**USER, "phone_number": TOKEN, "role_name": FRESH}
    out = await zoom.whoami(wallet(), transport=Routed(API, {"/users/me": (200, bugged)}), api=API)
    with pytest.raises(ConnectorError) as exc:
        await zoom.get_meeting(wallet(), meeting_id="87", transport=Scripted(
            (404, {"code": 3001, "message": "x"})), api=API)
    for secret in (TOKEN, FRESH):
        assert _clean(out, secret)
        assert secret not in str(exc.value)
        assert secret not in caplog.text
    assert not _clean({"leaked": TOKEN}, TOKEN)  # the sentinel is not vacuous


@pytest.mark.asyncio
async def test_multi_account_held_empty_one_many_and_from_account_hit_miss():
    ok = (200, USER)
    with pytest.raises(ConnectorError) as exc:
        await zoom.whoami(wallet(accounts=[]), transport=Scripted(ok), api=API)
    assert exc.value.code == "no_account"
    assert (await zoom.whoami(wallet(accounts=[(0, "a")]), transport=Scripted(ok), api=API))["email"]
    with pytest.raises(ConnectorError) as exc:
        await zoom.whoami(wallet(accounts=[(0, "a"), (1, "b")]), transport=Scripted(ok), api=API)
    assert exc.value.code == "ambiguous_account"
    assert (await zoom.whoami(wallet(accounts=[(0, "a"), (1, "b")]), from_account="b",
                              transport=Scripted(ok), api=API))["email"]
    with pytest.raises(ConnectorError) as exc:
        await zoom.whoami(wallet(accounts=[(0, "a")]), from_account="c",
                          transport=Scripted(ok), api=API)
    assert exc.value.code == "unknown_account"


@pytest.mark.asyncio
async def test_the_route_validates_each_op_and_reaches_the_handler(monkeypatch):
    from puffo_agent.portal import rpc_service

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/zoom", rpc_service.connector_route("zoom"))
    seen = []

    async def connector(ctx, *, provider, op, **fields):
        seen.append((provider, op, fields))
        return {"ok": 1}

    monkeypatch.setattr(rpc_service.host_mcp_handler, "connector", connector)
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: NS(credentials=wallet())
    try:
        async with TestClient(TestServer(app)) as http:
            miss = await http.post("/v1/rpc/a/zoom", json={"op": "get_meeting"})
            assert miss.status == 400  # missing required meeting_id
            assert (await http.post("/v1/rpc/a/zoom", json={"op": "nope"})).status == 400
            resp = await http.post("/v1/rpc/a/zoom", json={"op": "whoami"})
            assert resp.status == 200 and await resp.json() == {"ok": 1}
            assert seen[-1] == ("zoom", "whoami", {})
            resp = await http.post("/v1/rpc/a/zoom", json={"op": "get_meeting", "meeting_id": "87"})
            assert resp.status == 200
            assert seen[-1] == ("zoom", "get_meeting", {"meeting_id": "87"})
            # list_recordings requires the date window; the page_token cursor rides through
            miss = await http.post("/v1/rpc/a/zoom", json={"op": "list_recordings", "from_date": "a"})
            assert miss.status == 400  # missing required to_date
            resp = await http.post("/v1/rpc/a/zoom", json={
                "op": "list_recordings", "from_date": "a", "to_date": "b", "page_token": "p2"})
            assert resp.status == 200
            assert seen[-1] == ("zoom", "list_recordings",
                                {"from_date": "a", "to_date": "b", "page_token": "p2"})
            miss = await http.post("/v1/rpc/a/zoom", json={"op": "get_meeting_recordings"})
            assert miss.status == 400  # missing required meeting_id
            resp = await http.post("/v1/rpc/a/zoom", json={
                "op": "get_meeting_recordings", "meeting_id": "111"})
            assert resp.status == 200
            assert seen[-1] == ("zoom", "get_meeting_recordings", {"meeting_id": "111"})
    finally:
        rpc_service._RPC_RESOLVER = previous


@pytest.mark.asyncio
async def test_the_tool_shell_registers_the_read_tools_and_forwards_args():
    from puffo_agent.mcp import core_connector_tools

    calls = []

    class FakeRpc:
        async def connector(self, provider, op, **kw):
            calls.append((provider, op, kw))
            return {}

    registered = {}

    class FakeMcp:
        def tool(self):
            def decorate(fn):
                registered[fn.__name__] = fn
                return fn
            return decorate

    core_connector_tools.register_connector_tools(FakeMcp(), NS(rpc_client=FakeRpc()))
    assert {"zoom_whoami", "zoom_get_meeting",
            "zoom_list_recordings", "zoom_get_meeting_recordings"} <= set(registered)
    await registered["zoom_whoami"]()
    assert calls[-1] == ("zoom", "whoami", {"from_account": ""})
    await registered["zoom_get_meeting"]("87")
    assert calls[-1] == ("zoom", "get_meeting", {"meeting_id": "87", "from_account": ""})
    await registered["zoom_list_recordings"]("2026-01-01", "2026-01-31")
    assert calls[-1] == ("zoom", "list_recordings", {"from_date": "2026-01-01",
                         "to_date": "2026-01-31", "page_token": "", "from_account": ""})
    await registered["zoom_get_meeting_recordings"]("111")
    assert calls[-1] == ("zoom", "get_meeting_recordings",
                         {"meeting_id": "111", "from_account": ""})
