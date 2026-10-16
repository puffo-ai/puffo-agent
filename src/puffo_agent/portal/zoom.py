"""Zoom as the connected account — Phase 1: read-only GETs.

Phase 1 is deliberately narrow (spec §3.2): the tool face is the *verified*
capability face — ``whoami`` / ``get_meeting`` / ``list_recordings`` /
``get_meeting_recordings``, all GETs against scopes task-010 proved. The write
ops (``create_meeting`` / ``update_meeting``) are Phase 2, gated on the Owner's
start-timing call; they are not in this module yet.

Only a 401 is ever refreshed + retried once. A 400 carrying Zoom body code
**4711** is an insufficient-scope refusal — the same scope keeps on refresh, so
it is never retried (see ``_ok``); Zoom names the missing granular scope in the
message, which is passed through to the guidance (it is a scope enum, not a
secret).
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
    name="zoom", label="Zoom account",
    rt_type="PUFFO_ZOOM_OAUTH_v1", at_type="PUFFO_ZOOM_OAUTH_AT_v1",
)
API = "https://api.zoom.us/v2"

# Zoom's insufficient-scope refusal: HTTP 400 with this body code. It keeps the
# same scope on refresh, so (like a 403) it must never trigger one — see _ok.
INSUFFICIENT_SCOPE = 4711

# Zoom's recordings-list page size. A summary tool pulls a modest page (Zoom's
# ceiling for this endpoint is 300); fixed, not caller-tunable, per spec §3.2 —
# the caller pages with page_token, not by resizing.
RECORDINGS_PAGE_SIZE = 30


async def whoami(
    credentials: AgentCredentials | None,
    *,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    account = await pick_account(CONNECTOR, credentials, from_account)
    data = await _request(account, "GET", f"{api}/users/me", transport)
    logger.info("zoom whoami: ok")
    # Select fields — never echo the raw body (§5.7): the user object also
    # carries phone, dept, role, etc. that this summary does not surface.
    return {
        "display_name": str(data.get("display_name") or ""),
        "email": str(data.get("email") or ""),
        "type": data.get("type"),
        "pmi": data.get("pmi"),
        "timezone": str(data.get("timezone") or ""),
    }


async def get_meeting(
    credentials: AgentCredentials | None,
    *,
    meeting_id: str,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    if not meeting_id.strip():
        raise ConnectorError("bad_request", "meeting_id is required")
    account = await pick_account(CONNECTOR, credentials, from_account)
    data = await _request(
        account, "GET", f"{api}/meetings/{_quote(meeting_id.strip())}", transport,
    )
    logger.info("zoom get_meeting: %s", meeting_id.strip())
    return _meeting_summary(data)


async def list_recordings(
    credentials: AgentCredentials | None,
    *,
    from_date: str,
    to_date: str,
    page_token: str = "",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Cloud recordings for the connected user over [from_date, to_date], one
    page at a time. ``page_token`` is a previous call's ``next_page_token``."""
    if not from_date.strip() or not to_date.strip():
        raise ConnectorError("bad_request", "from_date and to_date are required (YYYY-MM-DD)")
    account = await pick_account(CONNECTOR, credentials, from_account)
    params: dict[str, Any] = {
        "from": from_date.strip(), "to": to_date.strip(), "page_size": RECORDINGS_PAGE_SIZE,
    }
    if page_token.strip():
        params["next_page_token"] = page_token.strip()
    data = await _request(account, "GET", f"{api}/users/me/recordings", transport, params=params)
    rows = [_recording_meeting_row(m) for m in data.get("meetings") or [] if isinstance(m, dict)]
    logger.info("zoom list_recordings: %d meeting(s)", len(rows))
    return {
        "recordings": rows,
        "count": len(rows),
        "next_page_token": str(data.get("next_page_token") or ""),
    }


async def get_meeting_recordings(
    credentials: AgentCredentials | None,
    *,
    meeting_id: str,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """The recording files of one meeting (metadata only — Phase 1 lists, it does
    not download or replay). ``meeting_id`` from ``zoom_list_recordings``."""
    if not meeting_id.strip():
        raise ConnectorError("bad_request", "meeting_id is required")
    account = await pick_account(CONNECTOR, credentials, from_account)
    data = await _request(
        account, "GET", f"{api}/meetings/{_quote(meeting_id.strip())}/recordings", transport,
    )
    files = [_recording_file_row(f) for f in data.get("recording_files") or [] if isinstance(f, dict)]
    logger.info("zoom get_meeting_recordings: %s (%d file(s))", meeting_id.strip(), len(files))
    return {"recording_files": files, "count": len(files)}


def _refused(status: int, data: Any) -> bool:
    # Only an expired/invalid token (401) is worth one refresh + retry. A 4711
    # (insufficient scope) keeps the same scope on refresh, so it is never
    # retried (see _ok) — and it arrives as 400, not 401, so it never matches here.
    return status == 401


async def _request(
    account: Account, method: str, url: str, transport, *, params: dict | None = None,
) -> dict:
    status, data = await call(
        CONNECTOR, account, method, url, params=params, transport=transport, refused=_refused,
    )
    return _ok(status, data)


def _ok(status: int, data: Any, *, retryable: bool = True) -> dict:
    """429: retryable on reads, definite on writes. 400 + code 4711 is an
    insufficient-scope refusal kept apart from a generic 400 (never refreshed)."""
    if status == 429:
        if retryable:
            _fail("rate_limited")
            raise ConnectorLostAnswer("Zoom rate-limited the request; try again shortly")
        raise _refusal("rate_limited", "Zoom rate-limited the request; this step was not applied")
    if status >= 500:
        _fail("no_answer")
        raise ConnectorLostAnswer(f"Zoom answered {status}")
    if not isinstance(data, dict):
        raise _refusal("bad_reply", f"Zoom answered {status} with no readable body")
    if 200 <= status < 300:
        return data
    code, message = _error_detail(data)
    if status == 401:
        raise _refusal("not_authorized", "Zoom refused the token; reconnect the account")
    if code == INSUFFICIENT_SCOPE:
        raise _refusal(
            "insufficient_scope",
            f"Zoom refused (insufficient scope): {message}. Reconnect the account and grant "
            "the missing Zoom scope it names above.",
        )
    if status == 404:
        raise _refusal(
            "not_found",
            "Zoom returned not-found: no such meeting or recording, or it is not on this account.",
        )
    raise _refusal(f"zoom_{code or status}", f"Zoom refused ({code or status}) {message}".strip())


def _error_detail(data: dict) -> tuple[Any, str]:
    """Zoom's ``{code, message}`` error body → (code, message). The numeric code
    drives classification (4711 = insufficient scope); message is clamped and
    carries the named scope (a scope enum, not a secret)."""
    return data.get("code"), str(data.get("message") or "")[:200]


def _refusal(code: str, message: str) -> ConnectorError:
    _fail(code)
    return ConnectorError(code, message)


def _fail(code: str) -> None:
    logger.warning("zoom: failed (%s)", code)


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _meeting_summary(meeting: dict) -> dict:
    """One meeting summary (spec §3.2). Absent fields project empty/None, never
    fabricated; ``start`` reads Zoom's ``start_time``."""
    return {
        "topic": str(meeting.get("topic") or ""),
        "start": str(meeting.get("start_time") or ""),
        "duration": meeting.get("duration"),
        "join_url": str(meeting.get("join_url") or ""),
    }


def _recording_meeting_row(meeting: dict) -> dict:
    """One recording session's summary (spec §3.2). ``meeting_id``/``uuid`` are
    the resource handles the file-list tool takes next. Sensitive body fields
    (download_url, play_url, password, host_email) are deliberately NOT selected
    — Phase 1 lists metadata; download/replay is a later phase."""
    return {
        "meeting_id": str(meeting.get("id") or ""),
        "uuid": str(meeting.get("uuid") or ""),
        "topic": str(meeting.get("topic") or ""),
        "start_time": str(meeting.get("start_time") or ""),
        "duration": meeting.get("duration"),
        "recording_count": meeting.get("recording_count"),
        "total_size": meeting.get("total_size"),
    }


def _recording_file_row(rfile: dict) -> dict:
    """One recording file's metadata (spec §3.2). ``download_url``/``play_url``
    are bearer URLs to the recording itself and are deliberately NOT selected in
    Phase 1 — listing only; surfacing them would open the suspended download
    capability through a side door and plant a standing leak surface."""
    return {
        "recording_id": str(rfile.get("id") or ""),
        "file_type": str(rfile.get("file_type") or ""),
        "file_extension": str(rfile.get("file_extension") or ""),
        "file_size": rfile.get("file_size"),
        "recording_type": str(rfile.get("recording_type") or ""),
        "recording_start": str(rfile.get("recording_start") or ""),
        "recording_end": str(rfile.get("recording_end") or ""),
    }
