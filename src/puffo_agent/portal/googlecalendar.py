"""Google Calendar as the connected account: list / create / update / delete.

v1 is deliberately narrow (Owner decision 2, 2026-10-16): every write carries
**no attendees** and a fixed **``sendUpdates=none``** — an agent creating,
editing, or deleting an event never sends an invite or notification to anyone.
Outbound mail is ``gmail_send``'s job, with its own confirmation discipline; the
calendar tools do not open a second outbound channel. Opening attendees is a
separate v2 with its own design + acceptance round.

Only the write ops are non-idempotent; a lost answer is reported ``unknown``
(it may have happened) rather than retried. A delete that returns 404 is a
success — the event is already gone (idempotent).
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from .credentials import AgentCredentials
from .oauth_connector import (
    Account,
    Connector,
    ConnectorError,
    ConnectorLostAnswer,
    Transport,
    call,
    pick_account,
)

logger = logging.getLogger(__name__)

CONNECTOR = Connector(
    name="googlecalendar", label="Google Calendar account",
    rt_type="PUFFO_GOOGLECALENDAR_OAUTH_v1", at_type="PUFFO_GOOGLECALENDAR_OAUTH_AT_v1",
)
API = "https://www.googleapis.com/calendar/v3"

# Google's events.list hard ceiling; a summary tool defaults far below it.
MAX_RESULTS = 2500
# v1 boundary (spec §5.5): every write is silent. Changing this is a new Owner
# decision + review, not a code tweak.
SEND_UPDATES = "none"


async def list_events(
    credentials: AgentCredentials | None,
    *,
    time_min: str = "",
    time_max: str = "",
    query: str = "",
    limit: int = 10,
    calendar_id: str = "primary",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    account = await pick_account(CONNECTOR, credentials, from_account)
    params: dict[str, Any] = {
        "singleEvents": "true", "orderBy": "startTime", "maxResults": _limit(limit, 10),
    }
    if time_min.strip():
        params["timeMin"] = time_min.strip()
    if time_max.strip():
        params["timeMax"] = time_max.strip()
    if query.strip():
        params["q"] = query.strip()
    data = await _request(
        account, "GET", f"{api}/calendars/{_quote(calendar_id)}/events", transport, params=params,
    )
    events = [_event_row(e) for e in data.get("items") or [] if isinstance(e, dict)]
    logger.info("calendar list_events: %d event(s)", len(events))
    return {
        "events": events,
        "count": len(events),
        "next_page_token": str(data.get("nextPageToken") or ""),
    }


async def list_calendars(
    credentials: AgentCredentials | None,
    *,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    account = await pick_account(CONNECTOR, credentials, from_account)
    data = await _request(account, "GET", f"{api}/users/me/calendarList", transport)
    calendars = [_calendar_row(c) for c in data.get("items") or [] if isinstance(c, dict)]
    logger.info("calendar list_calendars: %d calendar(s)", len(calendars))
    return {"calendars": calendars, "count": len(calendars)}


async def create_event(
    credentials: AgentCredentials | None,
    *,
    summary: str,
    start: str,
    end: str,
    description: str = "",
    all_day: str = "false",
    calendar_id: str = "primary",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Create an event. Raises only when definitely not created; a lost answer
    is returned as ``unknown``. ``all_day`` arrives as a string over RPC (the
    connector body rejects bools); see the tool shell."""
    if not summary.strip():
        raise ConnectorError("bad_request", "summary is required")
    if not start.strip() or not end.strip():
        raise ConnectorError("bad_request", "start and end are required")
    account = await pick_account(CONNECTOR, credentials, from_account)
    whole_day = _as_bool(all_day)
    payload: dict[str, Any] = {
        "summary": summary.strip(),
        "start": _time_field(start.strip(), whole_day),
        "end": _time_field(end.strip(), whole_day),
    }
    if description.strip():
        payload["description"] = description.strip()
    try:
        status, data = await call(
            CONNECTOR, account, "POST", f"{api}/calendars/{_quote(calendar_id)}/events",
            params={"sendUpdates": SEND_UPDATES}, payload=payload,
            transport=transport, refused=_refused,
        )
    except ConnectorLostAnswer as exc:
        return _unknown(str(exc))
    if status >= 500:
        return _unknown(f"Google answered {status}")
    if 200 <= status < 300 and not isinstance(data, dict):
        # The write did happen; an unreadable 2xx body must be `unknown`, not an
        # error the shell would read as "not created" and retry (duplicate event).
        return _unknown("Google answered 2xx with an unreadable body")
    event = _ok(status, data, retryable=False)
    logger.info("calendar create_event: %s", event.get("id", ""))
    return {"event_id": str(event.get("id") or ""), "html_link": str(event.get("htmlLink") or "")}


async def update_event(
    credentials: AgentCredentials | None,
    *,
    event_id: str,
    summary: str = "",
    start: str = "",
    end: str = "",
    description: str = "",
    calendar_id: str = "primary",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Patch an event. Raises only when definitely not applied; a lost answer
    is returned as ``unknown``."""
    if not event_id.strip():
        raise ConnectorError("bad_request", "event_id is required")
    patch: dict[str, Any] = {}
    if summary.strip():
        patch["summary"] = summary.strip()
    if start.strip():
        patch["start"] = {"dateTime": start.strip()}
    if end.strip():
        patch["end"] = {"dateTime": end.strip()}
    if description.strip():
        patch["description"] = description.strip()
    if not patch:
        raise ConnectorError(
            "bad_request", "nothing to update: pass summary, start, end, or description")
    account = await pick_account(CONNECTOR, credentials, from_account)
    eid = event_id.strip()
    try:
        status, data = await call(
            CONNECTOR, account, "PATCH",
            f"{api}/calendars/{_quote(calendar_id)}/events/{_quote(eid)}",
            params={"sendUpdates": SEND_UPDATES}, payload=patch,
            transport=transport, refused=_refused,
        )
    except ConnectorLostAnswer as exc:
        return _unknown(str(exc))
    if status >= 500:
        return _unknown(f"Google answered {status}")
    if 200 <= status < 300 and not isinstance(data, dict):
        return _unknown("Google answered 2xx with an unreadable body")
    event = _ok(status, data, retryable=False)
    logger.info("calendar update_event: %s", eid)
    return {"ok": True, "event_id": str(event.get("id") or eid)}


async def delete_event(
    credentials: AgentCredentials | None,
    *,
    event_id: str,
    calendar_id: str = "primary",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Delete an event. A 404 is success — the event is already gone. Raises
    only when definitely not deleted; a lost answer is returned as ``unknown``."""
    if not event_id.strip():
        raise ConnectorError("bad_request", "event_id is required")
    account = await pick_account(CONNECTOR, credentials, from_account)
    eid = event_id.strip()
    try:
        status, data = await call(
            CONNECTOR, account, "DELETE",
            f"{api}/calendars/{_quote(calendar_id)}/events/{_quote(eid)}",
            params={"sendUpdates": SEND_UPDATES}, transport=transport, refused=_refused,
        )
    except ConnectorLostAnswer as exc:
        return _unknown(str(exc))
    if status >= 500:
        return _unknown(f"Google answered {status}")
    if 200 <= status < 300 or status == 404:
        logger.info("calendar delete_event: %s", eid)
        return {"ok": True}
    _ok(status, data, retryable=False)  # classify + raise (403/401/429/…)
    raise ConnectorError("bad_reply", f"Google answered {status} unexpectedly")  # pragma: no cover


def _refused(status: int, data: Any) -> bool:
    # Only an expired/invalid token (401) is worth one refresh + retry. A 403
    # (insufficientPermissions / accessNotConfigured) keeps the same scope on
    # refresh, so it is never retried (see _ok).
    return status == 401


async def _request(
    account: Account, method: str, url: str, transport, *, params: dict | None = None,
) -> dict:
    status, data = await call(
        CONNECTOR, account, method, url, params=params, transport=transport, refused=_refused,
    )
    return _ok(status, data)


def _ok(status: int, data: Any, *, retryable: bool = True) -> dict:
    """429: retryable on reads, definite on writes. 403 reasons stay apart:
    insufficientPermissions -> insufficient_scope; accessNotConfigured ->
    api_not_enabled (never conflated)."""
    if status == 429:
        if retryable:
            _fail("rate_limited")
            raise ConnectorLostAnswer("Google rate-limited the request; try again shortly")
        raise _refusal("rate_limited", "Google rate-limited the request; this step was not applied")
    if status >= 500:
        _fail("no_answer")
        raise ConnectorLostAnswer(f"Google answered {status}")
    if not isinstance(data, dict):
        raise _refusal("bad_reply", f"Google answered {status} with no readable body")
    if 200 <= status < 300:
        return data
    reason, message = _error_detail(data)
    if status == 401:
        raise _refusal("not_authorized", "Google refused the token; reconnect the account")
    if status == 403 and reason == "accessNotConfigured":
        raise _refusal(
            "api_not_enabled",
            "Google refused (accessNotConfigured): the Calendar API is not enabled for this "
            "project in the Google console. Enable it there, then retry.",
        )
    if status == 403 and reason == "insufficientPermissions":
        raise _refusal(
            "insufficient_scope",
            "Google refused (insufficientPermissions): reconnect the account and grant Calendar "
            "access (the calendar.calendarlist.readonly scope may have been left unchecked).",
        )
    if status == 404:
        raise _refusal(
            "not_found",
            "Google returned not-found: no such calendar or event, or it is not on this account.",
        )
    raise _refusal(f"gcal_{reason or status}", f"Google refused ({reason or status}) {message}".strip())


def _error_detail(data: dict) -> tuple[str, str]:
    """Google's ``{error:{code,message,errors:[{reason}],status}}`` → (reason, message).
    The reason drives classification (task-012); absent fields stay empty."""
    err = data.get("error")
    if not isinstance(err, dict):
        return "", ""
    message = str(err.get("message") or "")[:200]
    reason = ""
    errors = err.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict) and item.get("reason"):
                reason = str(item.get("reason"))
                break
    return reason or str(err.get("status") or ""), message


def _refusal(code: str, message: str) -> ConnectorError:
    _fail(code)
    return ConnectorError(code, message)


def _unknown(detail: str) -> dict:
    logger.warning("calendar write: outcome unknown")
    return {
        "status": "unknown",
        "detail": f"{detail}. The change may have happened; check the calendar before "
                  "trying again.",
    }


def _fail(code: str) -> None:
    logger.warning("googlecalendar: failed (%s)", code)


def _limit(asked: int, default: int, *, ceiling: int = MAX_RESULTS) -> int:
    return max(1, min(int(asked or default), ceiling))


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _as_bool(value: Any) -> bool:
    # all_day crosses the RPC boundary as a string the shell alone produces, and
    # the shell only ever sends "true"/"false". Parse strictly: anything else is
    # a producer bug, and refusing it (invalid) beats a lax "non-true -> False"
    # that would silently swallow that bug into a wrong all-day flag.
    if value == "true":
        return True
    if value == "false":
        return False
    raise ConnectorError("bad_request", 'all_day must be the string "true" or "false"')


def _time_field(value: str, all_day: bool) -> dict:
    # All-day events carry a bare `date`; timed events carry an RFC-3339 `dateTime`.
    return {"date": value} if all_day else {"dateTime": value}


def _event_row(event: dict) -> dict:
    """One event summary. A missing summary/end projects empty — never fabricated."""
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    end = event.get("end") if isinstance(event.get("end"), dict) else {}
    return {
        "event_id": str(event.get("id") or ""),
        "summary": str(event.get("summary") or ""),
        "start": _when(start),
        "end": _when(end),
        "all_day": "date" in start and "dateTime" not in start,
    }


def _when(when: dict) -> str:
    # `dateTime` for timed events, `date` for all-day; absent -> "" (no fabrication).
    return str(when.get("dateTime") or when.get("date") or "")


def _calendar_row(cal: dict) -> dict:
    return {
        "calendar_id": str(cal.get("id") or ""),
        "summary": str(cal.get("summary") or ""),
        "primary": bool(cal.get("primary")),
        "access_role": str(cal.get("accessRole") or ""),
    }
