"""Notion as the connected workspace: search, read a page, and page writes.

Only ``create_page`` / ``update_page`` are non-idempotent; a lost answer is
reported as ``unknown`` (it may have been applied) rather than retried.

v1 works on Notion *pages*: ``create_page`` puts a child page under a parent
page and ``update_page`` edits a page's title / appends paragraphs. Creating
rows under a database (schema-keyed properties) is a separate, later op.
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
    name="notion", label="Notion workspace",
    rt_type="PUFFO_NOTION_OAUTH_v1", at_type="PUFFO_NOTION_OAUTH_AT_v1",
)
API = "https://api.notion.com/v1"
# Pinned: a version bump is a reviewed PR, not a runtime surprise.
NOTION_VERSION = "2022-06-28"
HEADERS = {"notion-version": NOTION_VERSION}

MAX_RESULTS = 100
MAX_TEXT_CHARS = 4000
# Notion rejects a single rich_text content chunk longer than 2000 chars.
MAX_BLOCK_CHARS = 2000
# One children GET/PATCH page.
MAX_CHILD_BLOCKS = 100


async def search(
    credentials: AgentCredentials | None,
    *,
    query: str = "",
    limit: int = 10,
    page_token: str = "",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    account = await pick_account(CONNECTOR, credentials, from_account)
    payload: dict[str, Any] = {"page_size": _limit(limit, 10)}
    if query.strip():
        payload["query"] = query.strip()
    if page_token.strip():
        payload["start_cursor"] = page_token.strip()
    data = await _request(account, "POST", f"{api}/search", transport, payload=payload)
    results = [_search_row(r) for r in data.get("results") or [] if isinstance(r, dict)]
    logger.info("notion search: %d result(s)", len(results))
    return {
        "results": results,
        "count": len(results),
        "next_page_token": str(data.get("next_cursor") or ""),
        "has_more": bool(data.get("has_more")),
    }


async def read_page(
    credentials: AgentCredentials | None,
    *,
    page_id: str,
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    if not page_id.strip():
        raise ConnectorError("bad_request", "page_id is required")
    account = await pick_account(CONNECTOR, credentials, from_account)
    pid = page_id.strip()
    page = await _request(account, "GET", f"{api}/pages/{_quote(pid)}", transport)
    children = await _request(
        account, "GET", f"{api}/blocks/{_quote(pid)}/children", transport,
        params={"page_size": MAX_CHILD_BLOCKS},
    )
    text, truncated = _blocks_text(children.get("results"))
    logger.info("notion read_page: %s", pid)
    return {
        "page_id": str(page.get("id") or ""),
        "title": _title_of(page),
        "url": str(page.get("url") or ""),
        "last_edited": str(page.get("last_edited_time") or ""),
        "archived": bool(page.get("archived")),
        "properties": _properties_summary(page.get("properties")),
        "content": text,
        "content_truncated": truncated,
        "has_more_blocks": bool(children.get("has_more")),
    }


async def create_page(
    credentials: AgentCredentials | None,
    *,
    parent_id: str,
    title: str,
    content: str = "",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Create a child page under a parent *page*. Raises only when definitely
    not created; a lost answer is returned as ``unknown``."""
    if not parent_id.strip():
        raise ConnectorError("bad_request", "parent_id is required")
    if not title.strip():
        raise ConnectorError("bad_request", "title is required")
    account = await pick_account(CONNECTOR, credentials, from_account)
    payload: dict[str, Any] = {
        "parent": {"page_id": parent_id.strip()},
        "properties": {"title": {"title": _text_rich(title.strip())}},
    }
    children, truncated = _child_blocks(content)
    if children:
        payload["children"] = children
    try:
        status, data = await call(
            CONNECTOR, account, "POST", f"{api}/pages",
            payload=payload, headers=HEADERS, transport=transport, refused=_refused,
        )
    except ConnectorLostAnswer as exc:
        return _unknown(str(exc))
    if status >= 500:
        return _unknown(f"Notion answered {status}")
    if 200 <= status < 300 and not isinstance(data, dict):
        # The write did happen; an unreadable 2xx body must not surface as an
        # error — the shell contract reads "error" as "not applied -> retry",
        # which would create the page twice.
        return _unknown("Notion answered 2xx with an unreadable body")
    page = _ok(status, data, retryable=False)
    logger.info("notion create_page: %s", page.get("id", ""))
    return {
        "page_id": str(page.get("id") or ""),
        "url": str(page.get("url") or ""),
        "content_truncated": truncated,
    }


async def update_page(
    credentials: AgentCredentials | None,
    *,
    page_id: str,
    title: str = "",
    append_content: str = "",
    from_account: str = "",
    transport: Transport | None = None,
    api: str = API,
) -> dict:
    """Set a page's title and/or append paragraphs. Raises only when definitely
    not applied; a lost answer is returned as ``unknown``."""
    if not page_id.strip():
        raise ConnectorError("bad_request", "page_id is required")
    if not title.strip() and not append_content.strip():
        raise ConnectorError("bad_request", "nothing to update: pass title or append_content")
    account = await pick_account(CONNECTOR, credentials, from_account)
    pid = page_id.strip()
    truncated = False
    if title.strip():
        lost = await _write(
            account, "PATCH", f"{api}/pages/{_quote(pid)}",
            {"properties": {"title": {"title": _text_rich(title.strip())}}}, transport,
        )
        if lost is not None:
            return lost
    if append_content.strip():
        blocks, truncated = _child_blocks(append_content)
        lost = await _write(
            account, "PATCH", f"{api}/blocks/{_quote(pid)}/children",
            {"children": blocks}, transport,
        )
        if lost is not None:
            return lost
    logger.info("notion update_page: %s", pid)
    return {"ok": True, "page_id": pid, "content_truncated": truncated}


async def _write(account: Account, method: str, url: str, payload: dict, transport) -> dict | None:
    """One write leg. Returns an ``unknown`` dict when the answer was lost,
    else ``None`` after a definite success (a definite failure raises)."""
    try:
        status, data = await call(
            CONNECTOR, account, method, url,
            payload=payload, headers=HEADERS, transport=transport, refused=_refused,
        )
    except ConnectorLostAnswer as exc:
        return _unknown(str(exc))
    if status >= 500:
        return _unknown(f"Notion answered {status}")
    if 200 <= status < 300 and not isinstance(data, dict):
        return _unknown("Notion answered 2xx with an unreadable body")
    _ok(status, data, retryable=False)
    return None


def _refused(status: int, data: Any) -> bool:
    # Only an expired/invalid token (401) is worth one refresh + retry; a 403
    # keeps the same scope on refresh, so it is never retried (see _ok).
    return status == 401


async def _request(
    account: Account, method: str, url: str, transport, *, params: dict | None = None,
    payload: dict | None = None,
) -> dict:
    status, data = await call(
        CONNECTOR, account, method, url, params=params, payload=payload,
        headers=HEADERS, transport=transport, refused=_refused,
    )
    return _ok(status, data)


def _ok(status: int, data: Any, *, retryable: bool = True) -> dict:
    """429: retryable on reads, definite on writes. 403 ⊥ 404 are kept apart."""
    if status == 429:
        if retryable:
            _fail("rate_limited")
            raise ConnectorLostAnswer("Notion rate-limited the request; try again shortly")
        raise _refusal("rate_limited", "Notion rate-limited the request; this step was not applied")
    if status >= 500:
        _fail("no_answer")
        raise ConnectorLostAnswer(f"Notion answered {status}")
    if not isinstance(data, dict):
        raise _refusal("bad_reply", f"Notion answered {status} with no readable body")
    if 200 <= status < 300:
        return data
    code = str(data.get("code") or "")
    if status == 401:
        raise _refusal("not_authorized", "Notion refused the token; reconnect the workspace")
    if status == 403 or code == "restricted_resource":
        raise _refusal(
            "insufficient_capability",
            "Notion refused (restricted_resource/403): the integration lacks this capability "
            "(e.g. comments), or the target is not shared with it. Add the integration via the "
            "page ••• → Connections, or have the Owner adjust the integration's capabilities.",
        )
    if status == 404 or code == "object_not_found":
        raise _refusal(
            "not_shared_or_missing",
            "Notion returned not-found: the object does not exist, or (more often) it is not "
            "shared with this integration — share the page/database with it.",
        )
    message = str(data.get("message") or "")[:200]
    raise _refusal(f"notion_{code or status}", f"Notion refused ({code or status}) {message}".strip())


def _refusal(code: str, message: str) -> ConnectorError:
    _fail(code)
    return ConnectorError(code, message)


def _unknown(detail: str) -> dict:
    logger.warning("notion write: outcome unknown")
    return {
        "status": "unknown",
        "detail": f"{detail}. The change may have been applied; check the page before "
                  "trying again.",
    }


def _fail(code: str) -> None:
    logger.warning("notion: failed (%s)", code)


def _limit(asked: int, default: int, *, ceiling: int = MAX_RESULTS) -> int:
    return max(1, min(int(asked or default), ceiling))


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _chunk(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _text_rich(text: str) -> list[dict]:
    """Rich-text payload, split so no chunk exceeds Notion's 2000-char cap."""
    return [{"type": "text", "text": {"content": chunk}} for chunk in _chunk(text, MAX_BLOCK_CHARS)]


def _paragraphs(content: str) -> list[dict]:
    """Each non-blank line becomes a paragraph block; long lines are split."""
    blocks: list[dict] = []
    for line in (content or "").split("\n"):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        for chunk in _chunk(line, MAX_BLOCK_CHARS):
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
            })
    return blocks


def _child_blocks(content: str) -> tuple[list[dict], bool]:
    """Paragraph blocks for a write body, and whether the list was cut at
    Notion's one-page ceiling. Writes stay as honest as read_page: the caller
    surfaces the flag instead of dropping blocks silently."""
    blocks = _paragraphs(content)
    if len(blocks) > MAX_CHILD_BLOCKS:
        return blocks[:MAX_CHILD_BLOCKS], True
    return blocks, False


def _rich_text(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    return "".join(str(i.get("plain_text") or "") for i in items if isinstance(i, dict))


def _search_row(obj: dict) -> dict:
    """One search hit: its id, kind, title, and last-edit stamp — nothing more."""
    return {
        "id": str(obj.get("id") or ""),
        "object": str(obj.get("object") or ""),
        "title": _title_of(obj),
        "last_edited": str(obj.get("last_edited_time") or ""),
    }


def _title_of(obj: dict) -> str:
    """A database's top-level title array, or a page's title-typed property.

    Absent title projects as "" — never a fabricated placeholder.
    """
    if isinstance(obj.get("title"), list):
        return _rich_text(obj["title"])
    props = obj.get("properties")
    if isinstance(props, dict):
        for prop in props.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                return _rich_text(prop.get("title"))
    return ""


def _blocks_text(results: Any) -> tuple[str, bool]:
    """Join one level of blocks' rich text. Blocks without text are skipped,
    not padded with a placeholder. Truncates at MAX_TEXT_CHARS with a flag."""
    lines: list[str] = []
    for block in results if isinstance(results, list) else []:
        if not isinstance(block, dict):
            continue
        body = block.get(block.get("type"))
        text = _rich_text(body.get("rich_text")) if isinstance(body, dict) else ""
        if text:
            lines.append(text)
    joined = "\n".join(lines)
    if len(joined) > MAX_TEXT_CHARS:
        return joined[:MAX_TEXT_CHARS], True
    return joined, False


def _properties_summary(props: Any) -> dict:
    """{name: {type, value}} for a page's properties. A missing/empty value
    projects as its empty form, never a fabricated default."""
    out: dict[str, Any] = {}
    if not isinstance(props, dict):
        return out
    for name, prop in props.items():
        if isinstance(prop, dict):
            out[name] = {"type": str(prop.get("type") or ""), "value": _prop_value(prop)}
    return out


def _prop_value(prop: dict) -> Any:
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return _rich_text(value)
    if kind in ("select", "status"):
        return str(value.get("name") or "") if isinstance(value, dict) else ""
    if kind == "multi_select":
        return [str(o.get("name") or "") for o in value if isinstance(o, dict)] \
            if isinstance(value, list) else []
    if kind == "number":
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if kind == "checkbox":
        return bool(value)
    if kind == "date":
        return str(value.get("start") or "") if isinstance(value, dict) else ""
    if kind in ("url", "email", "phone_number"):
        return str(value or "")
    if kind == "people":
        return len(value) if isinstance(value, list) else 0
    return ""
