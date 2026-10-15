"""Google Calendar against a fake Google: projection both ways, refresh, the
403 reason split (insufficientPermissions vs accessNotConfigured), the all-day
wire-encoding, idempotent delete, and the no-leak sentinel."""

import json
import logging
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from puffo_agent.portal import googlecalendar as gcal
from puffo_agent.portal.oauth_connector import ConnectorError
from tests.test_oauth_connector import FRESH, TOKEN, Routed, Scripted, Wallet

API = "https://gcal.test/v3"
EVENTS = "/calendars/primary/events"

TIMED = {"id": "ev1", "summary": "Standup", "htmlLink": "https://cal/ev1",
         "start": {"dateTime": "2026-03-01T09:00:00Z"}, "end": {"dateTime": "2026-03-01T09:15:00Z"}}
ALLDAY = {"id": "ev2", "summary": "Holiday",
          "start": {"date": "2026-03-02"}, "end": {"date": "2026-03-03"}}


def wallet(**kw):
    return Wallet(gcal.CONNECTOR, accounts=kw.pop("accounts", ((0, "work-cal"),)), **kw)


def _clean(payload, *secrets):
    blob = json.dumps(payload, default=str)
    return not any(s in blob for s in secrets)


def test_the_types_are_the_ones_puffo_server_provisions():
    """Literal server names — daemon half of the credential-type contract."""
    assert (gcal.CONNECTOR.rt_type, gcal.CONNECTOR.at_type) == (
        "PUFFO_GOOGLECALENDAR_OAUTH_v1", "PUFFO_GOOGLECALENDAR_OAUTH_AT_v1")
    assert gcal.CONNECTOR.name == "googlecalendar"


@pytest.mark.asyncio
async def test_list_events_shapes_rows_pins_query_params_and_clamps_the_limit():
    api = Routed(API, {EVENTS: (200, {"items": [TIMED, ALLDAY, "junk"], "nextPageToken": "n2"})})
    out = await gcal.list_events(wallet(), time_min="2026-03-01T00:00:00Z",
                                 time_max="2026-04-01T00:00:00Z", query=" team ", limit=9999,
                                 transport=api, api=API)
    assert out == {
        "events": [
            {"event_id": "ev1", "summary": "Standup", "start": "2026-03-01T09:00:00Z",
             "end": "2026-03-01T09:15:00Z", "all_day": False},
            {"event_id": "ev2", "summary": "Holiday", "start": "2026-03-02",
             "end": "2026-03-03", "all_day": True},
        ],
        "count": 2, "next_page_token": "n2",
    }
    (method, url, headers, body), = api.requests
    assert method == "GET" and url.startswith(f"{API}{EVENTS}?") and body is None
    assert headers["authorization"] == f"Bearer {TOKEN}"
    q = parse_qs(urlparse(url).query)
    assert q["singleEvents"] == ["true"] and q["orderBy"] == ["startTime"]
    assert q["maxResults"] == [str(gcal.MAX_RESULTS)]  # 9999 clamped to the ceiling
    assert q["timeMin"] == ["2026-03-01T00:00:00Z"] and q["timeMax"] == ["2026-04-01T00:00:00Z"]
    assert q["q"] == ["team"]


@pytest.mark.asyncio
async def test_list_events_reverse_absent_fields_are_empty_not_fabricated():
    """Reverse projection (spec §6 required Calendar case): an event with no
    end / an all-day event projects empty, never a fabricated default."""
    api = Routed(API, {EVENTS: (200, {"items": [
        {"id": "e0"},                                   # no summary/start/end
        {"id": "e1", "start": {"date": "2026-03-02"}},  # all-day, no end
    ]})})
    out = await gcal.list_events(wallet(), transport=api, api=API)
    assert out["events"][0] == {"event_id": "e0", "summary": "", "start": "", "end": "",
                                "all_day": False}
    assert out["events"][1] == {"event_id": "e1", "summary": "", "start": "2026-03-02",
                                "end": "", "all_day": True}
    assert out["next_page_token"] == ""
    # default limit (10) when unspecified, and no window/q params
    assert parse_qs(urlparse(api.requests[0][1]).query)["maxResults"] == ["10"]


@pytest.mark.asyncio
async def test_list_calendars_shapes_rows_and_reverse_defaults():
    api = Routed(API, {"/users/me/calendarList": (200, {"items": [
        {"id": "primary", "summary": "Me", "primary": True, "accessRole": "owner"},
        {"id": "team@x", "accessRole": "reader"},  # no summary/primary -> "" / False
        "junk",
    ]})})
    out = await gcal.list_calendars(wallet(), transport=api, api=API)
    assert out == {"calendars": [
        {"calendar_id": "primary", "summary": "Me", "primary": True, "access_role": "owner"},
        {"calendar_id": "team@x", "summary": "", "primary": False, "access_role": "reader"},
    ], "count": 2}
    assert api.requests[0][0] == "GET" and api.requests[0][1] == f"{API}/users/me/calendarList"


@pytest.mark.asyncio
async def test_a_bad_token_is_refreshed_once_then_named_as_the_reason():
    api, w = Scripted((401, {}), (200, {"items": []})), wallet()
    assert (await gcal.list_events(w, transport=api, api=API))["count"] == 0
    assert [h["authorization"] for _, _, h, _ in api.requests] == [f"Bearer {TOKEN}", f"Bearer {FRESH}"]
    assert w.refreshes == 1 and len(api.requests) == 2  # §5.8 positive control: 401 -> refresh
    with pytest.raises(ConnectorError) as exc:
        await gcal.list_events(wallet(), transport=Scripted((401, {}), (401, {})), api=API)
    assert exc.value.code == "not_authorized" and "reconnect" in str(exc.value)


@pytest.mark.asyncio
async def test_a_403_insufficient_permissions_is_insufficient_scope_and_never_refreshes():
    """§5.8: a 403 keeps the same scope on refresh, so it must not trigger one —
    refresh-count == 0 and a single HTTP attempt."""
    w, api = wallet(), Scripted((403, {"error": {"code": 403, "message": "Insufficient Permission",
                                                  "errors": [{"reason": "insufficientPermissions"}]}}))
    with pytest.raises(ConnectorError) as exc:
        await gcal.list_calendars(w, transport=api, api=API)
    assert exc.value.code == "insufficient_scope"
    assert w.refreshes == 0 and len(api.requests) == 1


@pytest.mark.asyncio
async def test_a_403_access_not_configured_is_api_not_enabled_a_separate_case():
    """Kept separate from insufficientPermissions on purpose (one test, one kill):
    accessNotConfigured (Calendar API disabled) must NOT be conflated with a scope
    denial, and still never refreshes."""
    w, api = wallet(), Scripted((403, {"error": {"code": 403, "message": "has not been used",
                                                  "errors": [{"reason": "accessNotConfigured"}]}}))
    with pytest.raises(ConnectorError) as exc:
        await gcal.create_event(w, summary="x", start="s", end="e", transport=api, api=API)
    assert exc.value.code == "api_not_enabled"
    assert w.refreshes == 0 and len(api.requests) == 1


@pytest.mark.asyncio
async def test_404_is_not_found_other_reasons_carry_their_code_and_bad_body_is_bad_reply():
    with pytest.raises(ConnectorError) as exc:
        await gcal.list_events(wallet(), transport=Scripted(
            (404, {"error": {"errors": [{"reason": "notFound"}], "message": "Not Found"}})), api=API)
    assert exc.value.code == "not_found"
    with pytest.raises(ConnectorError) as exc:
        await gcal.list_events(wallet(), transport=Scripted(
            (400, {"error": {"errors": [{"reason": "badRequest"}], "message": "bad"}})), api=API)
    assert exc.value.code == "gcal_badRequest" and "bad" in str(exc.value)
    with pytest.raises(ConnectorError) as exc:
        await gcal.list_events(wallet(), transport=Scripted((200, b"<html>")), api=API)
    assert exc.value.code == "bad_reply"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, kind", [
    (aiohttp.ServerDisconnectedError(), "unknown"), ((503, {}), "unknown"),
    ((429, {}), "error"),
    # a 2xx the write surely happened for, but an unreadable body: `unknown`,
    # NOT error — error would mean "not created" and the agent would retry.
    ((200, b"<html>"), "unknown")],
    ids=["disconnect", "5xx", "429", "2xx-unreadable"])
async def test_create_event_lost_answer_is_unknown_and_never_reposted(answer, kind):
    api = Scripted(answer)
    if kind == "error":
        with pytest.raises(ConnectorError):
            await gcal.create_event(wallet(), summary="x", start="s", end="e", transport=api, api=API)
    else:
        out = await gcal.create_event(wallet(), summary="x", start="s", end="e", transport=api, api=API)
        assert out["status"] == "unknown" and "may have happened" in out["detail"]
    assert len(api.requests) == 1


@pytest.mark.asyncio
async def test_create_event_timed_vs_all_day_encoding_sendupdates_and_no_attendees():
    api = Scripted((200, {"id": "c1", "htmlLink": "https://cal/c1"}))
    out = await gcal.create_event(wallet(), summary=" Sync ", start="2026-03-01T09:00:00Z",
                                  end="2026-03-01T10:00:00Z", description=" notes ",
                                  all_day="false", transport=api, api=API)
    assert out == {"event_id": "c1", "html_link": "https://cal/c1"}
    (method, url, _, body), = api.requests
    assert method == "POST" and url == f"{API}{EVENTS}?sendUpdates=none"  # v1: always silent
    assert body == {"summary": "Sync", "description": "notes",
                    "start": {"dateTime": "2026-03-01T09:00:00Z"},
                    "end": {"dateTime": "2026-03-01T10:00:00Z"}}
    assert "attendees" not in body  # v1 boundary: never an invite
    api2 = Scripted((200, {"id": "c2", "htmlLink": "l"}))
    await gcal.create_event(wallet(), summary="Off", start="2026-03-02", end="2026-03-03",
                            all_day="true", transport=api2, api=API)
    assert api2.requests[0][3]["start"] == {"date": "2026-03-02"}  # all-day -> bare date
    assert api2.requests[0][3]["end"] == {"date": "2026-03-03"}


@pytest.mark.asyncio
async def test_create_event_parses_all_day_strictly_and_requires_its_fields():
    """Correction ② (Bob 247837): the shell is the sole producer and sends only
    "true"/"false"; anything else is a producer bug refused as invalid — never a
    lax "non-true -> False" that would swallow the bug."""
    bad = Scripted()
    for value in ("True", "FALSE", "1", "yes", ""):
        with pytest.raises(ConnectorError) as exc:
            await gcal.create_event(wallet(), summary="x", start="s", end="e",
                                    all_day=value, transport=bad, api=API)
        assert exc.value.code == "bad_request"
    for kw in ({"summary": " "}, {"start": " "}, {"end": " "}):
        with pytest.raises(ConnectorError) as exc:
            await gcal.create_event(wallet(), **{"summary": "x", "start": "s", "end": "e", **kw},
                                    transport=bad, api=API)
        assert exc.value.code == "bad_request"
    assert bad.requests == []  # every rejection precedes any HTTP call


@pytest.mark.asyncio
async def test_update_event_patches_only_given_fields_and_returns_ok():
    api = Scripted((200, {"id": "ev1"}))
    out = await gcal.update_event(wallet(), event_id=" ev1 ", summary=" Renamed ",
                                  start="2026-03-01T09:00:00Z", transport=api, api=API)
    assert out == {"ok": True, "event_id": "ev1"}
    (method, url, _, body), = api.requests
    assert method == "PATCH" and url == f"{API}{EVENTS}/ev1?sendUpdates=none"
    assert body == {"summary": "Renamed", "start": {"dateTime": "2026-03-01T09:00:00Z"}}
    bad = Scripted()
    for kw in ({"event_id": "ev1"}, {"event_id": " ", "summary": "x"}):
        with pytest.raises(ConnectorError) as exc:
            await gcal.update_event(wallet(), transport=bad, api=API, **kw)
        assert exc.value.code == "bad_request"
    assert bad.requests == []


@pytest.mark.asyncio
async def test_update_event_lost_answer_is_unknown():
    out = await gcal.update_event(wallet(), event_id="ev1", summary="x",
                                  transport=Scripted(aiohttp.ServerDisconnectedError()), api=API)
    assert out["status"] == "unknown"


@pytest.mark.asyncio
async def test_delete_event_is_idempotent_on_404_and_still_raises_a_real_refusal():
    api = Scripted((204, {}))
    out = await gcal.delete_event(wallet(), event_id=" ev1 ", transport=api, api=API)
    assert out == {"ok": True}
    (method, url, _, _), = api.requests
    assert method == "DELETE" and url == f"{API}{EVENTS}/ev1?sendUpdates=none"
    gone = Scripted((404, {"error": {"errors": [{"reason": "notFound"}]}}))
    assert (await gcal.delete_event(wallet(), event_id="ev1", transport=gone, api=API)) == {"ok": True}
    out = await gcal.delete_event(wallet(), event_id="ev1",
                                  transport=Scripted(aiohttp.ServerDisconnectedError()), api=API)
    assert out["status"] == "unknown"
    with pytest.raises(ConnectorError) as exc:  # a 403 is not silently a success
        await gcal.delete_event(wallet(), event_id="ev1", transport=Scripted(
            (403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}})), api=API)
    assert exc.value.code == "insufficient_scope"
    bad = Scripted()
    with pytest.raises(ConnectorError) as exc:
        await gcal.delete_event(wallet(), event_id=" ", transport=bad, api=API)
    assert exc.value.code == "bad_request" and bad.requests == []


@pytest.mark.asyncio
async def test_multi_account_held_empty_one_many_and_from_account_hit_miss():
    ok = (200, {"items": []})
    with pytest.raises(ConnectorError) as exc:
        await gcal.list_events(wallet(accounts=[]), transport=Scripted(ok), api=API)
    assert exc.value.code == "no_account"
    assert (await gcal.list_events(wallet(accounts=[(0, "a")]), transport=Scripted(ok), api=API))["count"] == 0
    with pytest.raises(ConnectorError) as exc:
        await gcal.list_events(wallet(accounts=[(0, "a"), (1, "b")]), transport=Scripted(ok), api=API)
    assert exc.value.code == "ambiguous_account"
    assert (await gcal.list_events(wallet(accounts=[(0, "a"), (1, "b")]), from_account="b",
                                   transport=Scripted(ok), api=API))["count"] == 0
    with pytest.raises(ConnectorError) as exc:
        await gcal.list_events(wallet(accounts=[(0, "a")]), from_account="c",
                               transport=Scripted(ok), api=API)
    assert exc.value.code == "unknown_account"


@pytest.mark.asyncio
async def test_the_token_never_reaches_the_projection_log_or_error(caplog):
    """§5.7 scrub sentinel with teeth: the token is planted into NON-projected
    body fields (an event's raw extras), so a projection that echoed the raw body
    rather than selecting fields would leak it. Plus log/error scrub + positive control."""
    caplog.set_level(logging.DEBUG)
    bugged = {**TIMED, "secret_echo": TOKEN, "creator": {"email": FRESH}}
    out = await gcal.list_events(wallet(), transport=Routed(API, {EVENTS: (200, {"items": [bugged]})}),
                                 api=API)
    with pytest.raises(ConnectorError) as exc:
        await gcal.list_events(wallet(), transport=Scripted(
            (404, {"error": {"errors": [{"reason": "notFound"}], "message": "x"}})), api=API)
    for secret in (TOKEN, FRESH):
        assert _clean(out, secret)
        assert secret not in str(exc.value)
        assert secret not in caplog.text
    assert not _clean({"leaked": TOKEN}, TOKEN)  # the sentinel is not vacuous


@pytest.mark.asyncio
async def test_the_route_validates_each_op_rejects_a_bool_all_day_and_reaches_the_handler(monkeypatch):
    from puffo_agent.portal import rpc_service

    app = web.Application()
    app.router.add_post("/v1/rpc/{agent_id}/googlecalendar",
                        rpc_service.connector_route("googlecalendar"))
    seen = []

    async def connector(ctx, *, provider, op, **fields):
        seen.append((provider, op, fields))
        return {"ok": 1}

    monkeypatch.setattr(rpc_service.host_mcp_handler, "connector", connector)
    previous = rpc_service._RPC_RESOLVER
    rpc_service._RPC_RESOLVER = lambda agent_id: NS(credentials=wallet())
    try:
        async with TestClient(TestServer(app)) as http:
            miss = await http.post("/v1/rpc/a/googlecalendar", json={"op": "create_event", "summary": "x"})
            assert miss.status == 400  # missing required start/end
            # a bool all_day is rejected at the RPC body — this is exactly why the
            # tool shell string-encodes it (rpc_service._check_op_body bans bools).
            as_bool = await http.post("/v1/rpc/a/googlecalendar", json={
                "op": "create_event", "summary": "x", "start": "s", "end": "e", "all_day": True})
            assert as_bool.status == 400
            assert (await http.post("/v1/rpc/a/googlecalendar", json={"op": "nope"})).status == 400
            resp = await http.post("/v1/rpc/a/googlecalendar", json={
                "op": "create_event", "summary": "x", "start": "s", "end": "e", "all_day": "true"})
            assert resp.status == 200 and await resp.json() == {"ok": 1}
            assert seen[-1] == ("googlecalendar", "create_event",
                                {"summary": "x", "start": "s", "end": "e", "all_day": "true"})
    finally:
        rpc_service._RPC_RESOLVER = previous


@pytest.mark.asyncio
async def test_the_tool_shell_keeps_a_bool_signature_and_encodes_it_to_a_string():
    """Correction ① (Bob 247837): the agent-visible signature stays `all_day: bool`;
    the shell alone encodes it to the "true"/"false" string the RPC body accepts."""
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
    await registered["calendar_create_event"]("Sync", "s", "e", all_day=True)
    assert calls[-1] == ("googlecalendar", "create_event",
                         {"summary": "Sync", "start": "s", "end": "e", "description": "",
                          "all_day": "true", "calendar_id": "primary", "from_account": ""})
    await registered["calendar_create_event"]("Sync", "s", "e")  # default bool False
    assert calls[-1][2]["all_day"] == "false"
