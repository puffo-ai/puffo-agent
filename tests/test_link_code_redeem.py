"""``machine link --code`` — redeem a user-minted link code.

Covers the new code paths to 100%: ``normalize_link_code``,
``redeem_link_code`` (HTTP happy + every error branch), the ``run_link``
``--code`` branch (happy, redeem error, expired/timeout/incomplete
approval), plus a poll-race where approval lands on a later iteration.
The aiohttp session is faked so nothing touches the network.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _bridge_support import isolated_home  # noqa: E402

from puffo_agent.portal.control import link as link_mod  # noqa: E402
from puffo_agent.portal.control.link import (  # noqa: E402
    ControlError,
    LinkError,
    fetch_operator_display_name,
    mint_link_code,
    normalize_link_code,
    redeem_link_code,
    run_link,
)

_REAL_SLEEP = asyncio.sleep


# ── fake aiohttp ────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int, *, text: str = "", json_body: dict | None = None):
        self.status = status
        self._text = text
        self._json = json_body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._text

    async def json(self):
        return self._json


class _FakeSession:
    """Dispatches post/get to queued responses keyed by (method, path-suffix).

    Each key maps to a list consumed in order, so a poll loop can be
    handed a pending→approved sequence.
    """

    def __init__(self, routes: dict[tuple[str, str], list[_FakeResp]]):
        self._routes = routes
        self.calls: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _resp(self, method: str, url: str) -> _FakeResp:
        for (m, suffix), queue in self._routes.items():
            if m == method and url.endswith(suffix) and queue:
                self.calls.append((method, suffix))
                return queue.pop(0)
        raise AssertionError(f"no fake response for {method} {url}")

    def post(self, url, **_kw):
        return self._resp("POST", url)

    def get(self, url, **_kw):
        return self._resp("GET", url)


def _patch_session(monkeypatch, routes):
    session = _FakeSession(routes)
    monkeypatch.setattr(
        link_mod, "create_remote_http_session", lambda *a, **k: session
    )
    return session


@pytest.fixture(autouse=True)
def _home():
    old = {k: os.environ.get(k) for k in ("PUFFO_AGENT_HOME", "PUFFO_HOME")}
    isolated_home()
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ── normalize_link_code ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,want",
    [
        (" abcd-2345 ", "ABCD2345"),
        ("ABCD-2345", "ABCD2345"),
        ("abcd2345", "ABCD2345"),
        ("AB-CD-23-45", "ABCD2345"),
    ],
)
def test_normalize_link_code(raw, want):
    assert normalize_link_code(raw) == want


# ── redeem_link_code (HTTP) ─────────────────────────────────────────


def test_redeem_success_returns_base(monkeypatch):
    _patch_session(
        monkeypatch,
        {
            ("POST", "/v2/machines"): [_FakeResp(200)],
            ("POST", "/redeem"): [_FakeResp(200, json_body={"status": "claimed"})],
        },
    )
    base = asyncio.run(redeem_link_code("https://relay/", "Box", "ABCD2345"))
    assert base == "https://relay"


def test_redeem_registration_rejected(monkeypatch):
    _patch_session(
        monkeypatch,
        {("POST", "/v2/machines"): [_FakeResp(500, text="boom")]},
    )
    with pytest.raises(LinkError, match="registration rejected"):
        asyncio.run(redeem_link_code("https://relay", "Box", "ABCD2345"))


@pytest.mark.parametrize(
    "status,match",
    [
        (404, "unknown link code"),
        (410, "expired"),
        (409, "could not redeem"),
    ],
)
def test_redeem_error_statuses(monkeypatch, status, match):
    _patch_session(
        monkeypatch,
        {
            ("POST", "/v2/machines"): [_FakeResp(200)],
            ("POST", "/redeem"): [_FakeResp(status, text="nope")],
        },
    )
    with pytest.raises(LinkError, match=match):
        asyncio.run(redeem_link_code("https://relay", "Box", "ABCD2345"))


# ── run_link --code branch ──────────────────────────────────────────


def test_run_link_code_happy(monkeypatch):
    calls = []

    async def _redeem(server_url, hostname, code):
        calls.append(("redeem", code))
        return "https://relay"

    async def _await(base, code, hostname):
        calls.append(("await", code, base))
        return "approved", "op-0001"

    monkeypatch.setattr(link_mod, "redeem_link_code", _redeem)
    monkeypatch.setattr(link_mod, "await_link_approval", _await)
    rc = asyncio.run(run_link("https://relay", "Box", open_browser=False, code="abcd-2345"))
    assert rc == 0
    assert calls == [("redeem", "ABCD2345"), ("await", "ABCD2345", "https://relay")]


def test_run_link_code_redeem_error(monkeypatch):
    async def _redeem(server_url, hostname, code):
        raise LinkError("unknown link code")

    monkeypatch.setattr(link_mod, "redeem_link_code", _redeem)
    rc = asyncio.run(run_link("https://relay", "Box", open_browser=False, code="NOPE1234"))
    assert rc == 1


@pytest.mark.parametrize("status", ["expired", "timeout"])
def test_run_link_code_await_non_approved(monkeypatch, status):
    async def _redeem(server_url, hostname, code):
        return "https://relay"

    async def _await(base, code, hostname):
        return status, None

    monkeypatch.setattr(link_mod, "redeem_link_code", _redeem)
    monkeypatch.setattr(link_mod, "await_link_approval", _await)
    rc = asyncio.run(run_link("https://relay", "Box", open_browser=False, code="ABCD2345"))
    assert rc == 1


def test_run_link_code_await_control_error(monkeypatch):
    async def _redeem(server_url, hostname, code):
        return "https://relay"

    async def _await(base, code, hostname):
        raise ControlError("bad cert")

    monkeypatch.setattr(link_mod, "redeem_link_code", _redeem)
    monkeypatch.setattr(link_mod, "await_link_approval", _await)
    rc = asyncio.run(run_link("https://relay", "Box", open_browser=False, code="ABCD2345"))
    assert rc == 1


def test_run_link_code_await_link_error(monkeypatch):
    async def _redeem(server_url, hostname, code):
        return "https://relay"

    async def _await(base, code, hostname):
        raise LinkError("approval response incomplete")

    monkeypatch.setattr(link_mod, "redeem_link_code", _redeem)
    monkeypatch.setattr(link_mod, "await_link_approval", _await)
    rc = asyncio.run(run_link("https://relay", "Box", open_browser=False, code="ABCD2345"))
    assert rc == 1


# ── await_link_approval — race: approval lands on a later poll ───────


def test_await_approval_race_lands_on_second_poll(monkeypatch):
    monkeypatch.setattr(link_mod.asyncio, "sleep", lambda _s: _REAL_SLEEP(0))
    monkeypatch.setattr(link_mod, "verify_control_cert", lambda *a: "op_root_pk")
    saved = []
    monkeypatch.setattr(link_mod, "save_pairing", lambda p: saved.append(p))

    async def _noop_migrate(_root):
        return 0

    monkeypatch.setattr(link_mod, "migrate_owned_agents", _noop_migrate)

    _patch_session(
        monkeypatch,
        {
            ("GET", "/ABCD2345"): [
                _FakeResp(200, json_body={"status": "claimed"}),
                _FakeResp(500),  # transient blip — skipped
                _FakeResp(
                    200,
                    json_body={
                        "status": "approved",
                        "operator_control_cert": {"kind": "control_cert"},
                        "operator_slug": "op-0001",
                    },
                ),
            ],
        },
    )
    status, slug = asyncio.run(
        link_mod.await_link_approval("https://relay", "ABCD2345", "Box", timeout=100)
    )
    assert status == "approved"
    assert slug == "op-0001"
    assert len(saved) == 1  # pairing saved exactly once despite the race


def test_await_approval_expired_returns_early(monkeypatch):
    monkeypatch.setattr(link_mod.asyncio, "sleep", lambda _s: _REAL_SLEEP(0))
    _patch_session(
        monkeypatch,
        {("GET", "/ABCD2345"): [_FakeResp(200, json_body={"status": "expired"})]},
    )
    status, slug = asyncio.run(
        link_mod.await_link_approval("https://relay", "ABCD2345", "Box", timeout=100)
    )
    assert status == "expired"
    assert slug is None


def test_await_approval_incomplete_cert_raises(monkeypatch):
    monkeypatch.setattr(link_mod.asyncio, "sleep", lambda _s: _REAL_SLEEP(0))
    _patch_session(
        monkeypatch,
        {
            ("GET", "/ABCD2345"): [
                _FakeResp(200, json_body={"status": "approved", "operator_slug": "op-0001"}),
            ],
        },
    )
    with pytest.raises(LinkError, match="incomplete"):
        asyncio.run(link_mod.await_link_approval("https://relay", "ABCD2345", "Box", timeout=100))


def test_await_approval_times_out(monkeypatch):
    monkeypatch.setattr(link_mod.asyncio, "sleep", lambda _s: _REAL_SLEEP(0))
    _patch_session(
        monkeypatch,
        {("GET", "/ABCD2345"): [_FakeResp(200, json_body={"status": "pending"})]},
    )
    # timeout below one poll interval → loop exits immediately with timeout.
    status, slug = asyncio.run(
        link_mod.await_link_approval("https://relay", "ABCD2345", "Box", timeout=0)
    )
    assert status == "timeout"
    assert slug is None


# ── run_link WITHOUT code (mint branch — existing flow, re-covered) ──


def test_run_link_no_code_mints_and_opens_browser(monkeypatch):
    opened = []

    async def _mint(server_url, hostname):
        return "ABCD2345", "https://relay"

    async def _await(base, code, hostname):
        return "approved", "op-0001"

    monkeypatch.setattr(link_mod, "mint_link_code", _mint)
    monkeypatch.setattr(link_mod, "await_link_approval", _await)
    monkeypatch.setattr(link_mod.webbrowser, "open", lambda url: opened.append(url))
    rc = asyncio.run(run_link("https://relay", "Box", open_browser=True))
    assert rc == 0
    assert opened and "code=ABCD2345" in opened[0]


def test_run_link_no_code_mint_error(monkeypatch):
    async def _mint(server_url, hostname):
        raise LinkError("could not create code")

    monkeypatch.setattr(link_mod, "mint_link_code", _mint)
    rc = asyncio.run(run_link("https://relay", "Box", open_browser=False))
    assert rc == 1


def test_run_link_no_code_browser_open_failure_is_nonfatal(monkeypatch):
    async def _mint(server_url, hostname):
        return "ABCD2345", "https://relay"

    async def _await(base, code, hostname):
        return "approved", "op-0001"

    def _boom(_url):
        raise RuntimeError("no browser")

    monkeypatch.setattr(link_mod, "mint_link_code", _mint)
    monkeypatch.setattr(link_mod, "await_link_approval", _await)
    monkeypatch.setattr(link_mod.webbrowser, "open", _boom)
    rc = asyncio.run(run_link("https://relay", "Box", open_browser=True))
    assert rc == 0



# ── mint_link_code (sibling of redeem — machine mints its own) ──────


def test_mint_success_returns_code_and_base(monkeypatch):
    _patch_session(
        monkeypatch,
        {
            ("POST", "/v2/machines"): [_FakeResp(200)],
            ("POST", "/v2/machines/links"): [_FakeResp(200, json_body={"code": "WXYZ7788"})],
        },
    )
    code, base = asyncio.run(mint_link_code("https://relay/", "Box"))
    assert code == "WXYZ7788"
    assert base == "https://relay"


def test_mint_registration_rejected(monkeypatch):
    _patch_session(monkeypatch, {("POST", "/v2/machines"): [_FakeResp(403, text="no")]})
    with pytest.raises(LinkError, match="registration rejected"):
        asyncio.run(mint_link_code("https://relay", "Box"))


def test_mint_create_code_rejected(monkeypatch):
    _patch_session(
        monkeypatch,
        {
            ("POST", "/v2/machines"): [_FakeResp(200)],
            ("POST", "/v2/machines/links"): [_FakeResp(500, text="boom")],
        },
    )
    with pytest.raises(LinkError, match="could not create code"):
        asyncio.run(mint_link_code("https://relay", "Box"))


# ── PUF-393: fetch_operator_display_name ─────────────────────────────


def test_fetch_operator_display_name_ok(monkeypatch):
    _patch_session(
        monkeypatch,
        {
            ("GET", "/operators/alice-1"): [
                _FakeResp(200, json_body={"operator_slug": "alice-1", "display_name": "Alice"})
            ]
        },
    )
    name = asyncio.run(fetch_operator_display_name("https://x.example", "alice-1"))
    assert name == "Alice"


def test_fetch_operator_display_name_non_200_returns_empty(monkeypatch):
    _patch_session(
        monkeypatch,
        {("GET", "/operators/alice-1"): [_FakeResp(403, text="not linked")]},
    )
    assert asyncio.run(fetch_operator_display_name("https://x.example", "alice-1")) == ""


def test_fetch_operator_display_name_missing_field_returns_empty(monkeypatch):
    _patch_session(
        monkeypatch,
        {("GET", "/operators/alice-1"): [_FakeResp(200, json_body={"operator_slug": "alice-1"})]},
    )
    assert asyncio.run(fetch_operator_display_name("https://x.example", "alice-1")) == ""


def test_fetch_operator_display_name_network_error_returns_empty(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(link_mod, "create_remote_http_session", boom)
    assert asyncio.run(fetch_operator_display_name("https://x.example", "alice-1")) == ""
