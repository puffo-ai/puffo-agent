"""The ``connector.refresh`` and ``connector.disconnect`` computer commands.

These two ends have never spoken. A handler exists on the other side now
(puffo-server #406 at ``127ee329``, Bob 221596) and nothing here has been sent
to it, so nothing here can check the daemon against a real one. What it can check is the
part that is decided: the agreed request and response bodies (Jeff 221524, Bob
221549), that the bytes signed are the bytes sent, and that every way this can
fail reports the segment it failed at without claiming the computer was
disconnected when it was not.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from puffo_agent.portal.connector import command as connector_command
from puffo_agent.portal.connector.claim import (
    STAGE_CLEAR,
    STAGE_EXCHANGE,
    STAGE_READ_LOCAL,
    STAGE_SAVE,
    STAGE_STALE,
)
from puffo_agent.portal.connector.command import RefreshRefused
from puffo_agent.portal.connector.store import SkeletonConnectionStore

# Distinct per generation, so "the new one is here" and "the old one is gone"
# are two questions one assertion cannot accidentally answer together.
FIRST = {"refresh_token": "SENTINEL5f1c2ab4-first"}
SECOND = {"refresh_token": "SENTINELc0de9917-second"}


def store_at(tmp_path):
    return SkeletonConnectionStore(tmp_path / "connection.json")


def connected(store, credential=FIRST):
    return store.save(request_ref="req-1", provider="google", credential=credential)


def wired(monkeypatch, store):
    """Point the command at ``store`` and at a machine identity that is not
    read from this computer's disk."""
    monkeypatch.setattr(connector_command, "load_or_create_machine", lambda: object())
    monkeypatch.setattr(connector_command, "connection_store", lambda machine: store)


def exchanging(monkeypatch, handler):
    """Replace the whole server leg. Used by every cell that is about what the
    command does with an answer rather than about how the answer is fetched."""
    seen = []

    async def fake(base, machine, reference, credential):
        seen.append({"base": base, "reference": reference, "credential": credential})
        return handler(credential)

    monkeypatch.setattr(connector_command, "_exchange_with_server", fake)
    return seen


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_a_refresh_sends_the_agreed_body_and_signs_the_bytes_it_sends(
    monkeypatch, tmp_path
):
    """The agreed shape, and the one thing a body adds over the claim.

    A claim signs zero body bytes because it has no body. This has one, so the
    bytes that were signed and the bytes that went out have to be the same —
    re-serialising between the two would sign one string and send another, and
    the server would reject every refresh for a reason nothing here could see.
    """
    posted = {}

    def sign(machine, method, path, body):
        assert method == "POST"
        posted["signed"] = body
        return {"x-puffo-signature": "fake"}

    def post(url, *, data, headers):
        posted["url"] = url
        posted["data"] = data
        assert headers["content-type"] == "application/json"
        response = type("Response", (), {"status": 200})()

        async def as_json():
            return {"credential": SECOND}

        response.json = as_json
        return _Context(response)

    session = type("Session", (), {"post": staticmethod(post)})()
    monkeypatch.setattr(
        connector_command, "create_remote_http_session", lambda *a, **k: _Context(session)
    )
    monkeypatch.setattr(connector_command.machine_auth, "signed_headers", sign)

    got = await connector_command._exchange_with_server(
        "https://test.invalid", object(), "conn-7", FIRST
    )

    assert got == SECOND
    # The agreed body: the reference, the credential whole, and no request_ref
    # (Jeff 221524 removed it).
    assert json.loads(posted["data"]) == {"connection_ref": "conn-7", "credential": FIRST}
    assert posted["signed"] == posted["data"]
    assert posted["url"].endswith(connector_command._REFRESH_PATH)


@pytest.mark.asyncio
async def test_a_server_that_says_no_is_reported_with_its_own_code(monkeypatch):
    """No reason table, unlike the claim — so the raw code has to survive.

    The claim can translate a refusal because interface v1 fixes the codes.
    Nothing fixes these; there is no endpoint yet. A sentence this file made
    up would be the same reason-loss the claim's table exists to prevent
    (Boris 219775), with less excuse.
    """
    response = type("Response", (), {"status": 409})()

    async def as_json():
        return {"reason": "connection_unknown"}

    response.json = as_json
    session = type(
        "Session", (), {"post": staticmethod(lambda *a, **k: _Context(response))}
    )()
    monkeypatch.setattr(
        connector_command, "create_remote_http_session", lambda *a, **k: _Context(session)
    )
    monkeypatch.setattr(
        connector_command.machine_auth, "signed_headers", lambda *a, **k: {}
    )

    with pytest.raises(RefreshRefused) as refused:
        await connector_command._exchange_with_server(
            "https://test.invalid", object(), "conn-7", FIRST
        )

    assert "409" in refused.value.reason
    assert "connection_unknown" in refused.value.reason


@pytest.mark.asyncio
async def test_the_transport_never_lets_an_error_out_under_another_name(monkeypatch):
    """Everything that goes wrong out there arrives as one type.

    Not tidiness. The entry point below tells a store that would not read from
    a replacement it could not save by whether the server had answered yet, and
    an ``OSError`` escaping this function would land in that same handler and
    be reported as an unreadable local store — a network fault described as a
    disk one. The wrapping here is what makes that unreachable, so it is pinned
    here rather than assumed there.
    """
    def explodes(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(connector_command, "create_remote_http_session", explodes)
    monkeypatch.setattr(
        connector_command.machine_auth, "signed_headers", lambda *a, **k: {}
    )

    with pytest.raises(RefreshRefused) as refused:
        await connector_command._exchange_with_server(
            "https://test.invalid", object(), "conn-7", FIRST
        )

    # The type name survives, for the same reason the claim carries one.
    assert "OSError" in refused.value.reason


def test_the_stages_a_refresh_can_end_at_are_distinguishable():
    """Positive control for the stage names themselves.

    Every other cell here compares a result against the imported constant, so
    two stages collapsing onto one string would satisfy all of them and leave a
    caller unable to tell a stale connection from an unreadable store. An
    assertion that reads the same name it is checking is a sentinel that never
    reds; this one reads them as values.
    """
    stages = [STAGE_READ_LOCAL, STAGE_STALE, STAGE_EXCHANGE, STAGE_SAVE, STAGE_CLEAR]

    assert len(set(stages)) == len(stages)
    assert "command" not in stages


@pytest.mark.asyncio
async def test_a_refresh_stores_the_replacement_whole(monkeypatch, tmp_path):
    """Whole in, whole out. The daemon is not told the credential's shape
    (v0.4 §4), so neither direction may take it apart."""
    store = store_at(tmp_path)
    original = connected(store)
    wired(monkeypatch, store)
    seen = exchanging(monkeypatch, lambda credential: SECOND)

    result = await connector_command.run_refresh_command(
        {"connection_ref": original.reference}, "https://test.invalid/"
    )

    assert result == {"ok": True, "connection_ref": original.reference}
    assert seen == [
        {
            "base": "https://test.invalid",
            "reference": original.reference,
            "credential": FIRST,
        }
    ]
    # Read back off the store, not taken from the return value.
    reloaded = store_at(tmp_path).load()
    assert reloaded.credential == SECOND
    # A refresh replaces a credential, not a connection.
    assert reloaded.reference == original.reference
    assert reloaded.provider == "google"
    assert reloaded.request_ref == "req-1"


@pytest.mark.asyncio
async def test_a_refresh_for_a_connection_that_is_not_here_never_reaches_the_server(
    monkeypatch, tmp_path
):
    store = store_at(tmp_path)
    connected(store)
    wired(monkeypatch, store)
    seen = exchanging(monkeypatch, lambda credential: SECOND)

    result = await connector_command.run_refresh_command(
        {"connection_ref": "some-other-connection"}, "https://test.invalid"
    )

    assert result["ok"] is False
    assert result["stage"] == STAGE_STALE
    assert seen == []
    assert store_at(tmp_path).load().credential == FIRST


@pytest.mark.asyncio
async def test_a_refused_refresh_leaves_the_credential_alone(monkeypatch, tmp_path):
    store = store_at(tmp_path)
    original = connected(store)
    wired(monkeypatch, store)

    def refuses(credential):
        raise RefreshRefused("server refused the refresh (503, reason=None)")

    exchanging(monkeypatch, refuses)

    result = await connector_command.run_refresh_command(
        {"connection_ref": original.reference}, "https://test.invalid"
    )

    assert result["ok"] is False
    assert result["stage"] == STAGE_EXCHANGE
    assert "503" in result["reason"]
    assert store_at(tmp_path).load().credential == FIRST


@pytest.mark.asyncio
async def test_no_failed_refresh_reports_this_computer_as_disconnected(
    monkeypatch, tmp_path
):
    """A refresh that fails is not an outage, and must not be reported as one.

    Every stage but the last leaves the connection exactly as it was. A
    ``connected: False`` in any of these results would put "未连接" on a page
    for a computer still holding a working credential — so the key is absent
    from all of them rather than set correctly in each, which is one place to
    get it wrong instead of four.
    """
    store = store_at(tmp_path)
    original = connected(store)
    wired(monkeypatch, store)

    def refuses(credential):
        raise RefreshRefused("nope")

    exchanging(monkeypatch, refuses)
    results = [
        await connector_command.run_refresh_command({}, "https://test.invalid"),
        await connector_command.run_refresh_command(
            {"connection_ref": "gone"}, "https://test.invalid"
        ),
        await connector_command.run_refresh_command(
            {"connection_ref": original.reference}, "https://test.invalid"
        ),
    ]

    assert [r["ok"] for r in results] == [False, False, False]
    # Three genuinely different failures, so this is not one case checked
    # three times: a malformed command, a connection that is not here, and a
    # server that said no.
    assert [r["stage"] for r in results] == ["command", STAGE_STALE, STAGE_EXCHANGE]
    for result in results:
        assert "connected" not in result


@pytest.mark.asyncio
async def test_a_response_the_daemon_cannot_read_is_refused_rather_than_stored(
    monkeypatch, tmp_path
):
    """What happens when the response is not the agreed shape.

    The shape is agreed — ``{"credential": ...}`` (Bob 221549) — and this is
    not doubting it. It is which way to be wrong if it ever moves. Refusing
    fails here and changes nothing that is stored. The alternative reading,
    taking whatever arrived as the credential, would store an envelope as a
    credential and nothing would notice until the connection was next used.
    """
    for payload in (SECOND, {"access_token": "bare"}, ["not", "an", "object"], None):
        with pytest.raises(RefreshRefused):
            connector_command._read_refreshed(payload)

    assert connector_command._read_refreshed({"credential": SECOND}) == SECOND
    # An empty credential is still an answer the server gave; it is not this
    # function's business to decide the credential is too small (v0.4 §4).
    assert connector_command._read_refreshed({"credential": {}}) == {}


@pytest.mark.asyncio
async def test_an_unreadable_store_stops_the_refresh_before_the_server(
    monkeypatch, tmp_path
):
    store = store_at(tmp_path)
    original = connected(store)
    (tmp_path / "connection.json").write_bytes(b"{ this is not json")
    wired(monkeypatch, store)
    seen = exchanging(monkeypatch, lambda credential: SECOND)

    result = await connector_command.run_refresh_command(
        {"connection_ref": original.reference}, "https://test.invalid"
    )

    assert result["ok"] is False
    assert result["stage"] == STAGE_READ_LOCAL
    # The class name is what survives to the caller, so it has to be in there
    # (Jeff 221379): this stage cannot otherwise tell a flaky read from a
    # computer whose two copies disagree.
    assert "JSONDecodeError" in result["reason"] or "ValueError" in result["reason"]
    assert seen == []


@pytest.mark.asyncio
async def test_a_replacement_that_could_not_be_stored_says_the_server_already_answered(
    monkeypatch, tmp_path
):
    """The distinction the stage names exist for.

    A store that would not read and a store that would not write both raise
    OSError. They are not the same news: the first leaves the credential
    untouched, the second means the server has already been asked for a
    replacement that is now nowhere — and whether that cost the connection
    depends on whether the exchange rotated anything, which this daemon cannot
    know because it never looks inside the credential (v0.4 §4).
    """
    store = store_at(tmp_path)
    original = connected(store)
    wired(monkeypatch, store)
    exchanging(monkeypatch, lambda credential: SECOND)

    def cannot_write(self, body):
        raise OSError("read-only file system")

    monkeypatch.setattr(SkeletonConnectionStore, "_put", cannot_write)

    result = await connector_command.run_refresh_command(
        {"connection_ref": original.reference}, "https://test.invalid"
    )

    assert result["ok"] is False
    assert result["stage"] == STAGE_SAVE
    assert "the server answered" in result["reason"]
    assert "OSError" in result["reason"]
    # And it is a different stage from the same exception raised before the
    # server was reached — the cell above pins that side.
    assert result["stage"] != STAGE_READ_LOCAL


@pytest.mark.asyncio
async def test_a_server_leg_failure_is_never_reported_as_a_broken_local_store(
    monkeypatch, tmp_path
):
    """The classification must not depend on a wrapper two functions away.

    ``_exchange_with_server`` turns every transport error into
    ``RefreshRefused``, and while it does, nothing else can happen. But an
    ``OSError`` that got past it would land in the handler for the store and be
    reported as "local store unreadable" — a network fault described as a disk
    one. That was measured, not imagined: with the guard removed this cell
    reports ``read_local``.

    So the entry point classifies at the boundary of the leg rather than
    trusting the leg's insides. Unreachable-because-something-distant-is-
    careful is the kind of guarantee that stops holding with nobody editing
    the file that relies on it.
    """
    store = store_at(tmp_path)
    original = connected(store)
    wired(monkeypatch, store)

    async def leaks(base, machine, reference, credential):
        raise OSError("connection refused")

    monkeypatch.setattr(connector_command, "_exchange_with_server", leaks)

    result = await connector_command.run_refresh_command(
        {"connection_ref": original.reference}, "https://test.invalid"
    )

    assert result["stage"] == STAGE_EXCHANGE
    assert result["stage"] != STAGE_READ_LOCAL
    assert "OSError" in result["reason"]
    assert store_at(tmp_path).load().credential == FIRST


@pytest.mark.asyncio
async def test_a_cancelled_refresh_is_not_answered_with_a_result(monkeypatch, tmp_path):
    """Cancellation gets out, and the docstring says "always returns" anyway.

    ``asyncio.CancelledError`` is a ``BaseException``: it is not caught here
    and must not be, because a tidy result dict would keep a dying errand
    alive. This cell exists so that "always returns a command result" cannot
    quietly grow to cover a case nobody checked — it is pinned as the
    exception it is.
    """
    store = store_at(tmp_path)
    original = connected(store)
    wired(monkeypatch, store)

    async def cancelled(base, machine, reference, credential):
        raise asyncio.CancelledError()

    monkeypatch.setattr(connector_command, "_exchange_with_server", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await connector_command.run_refresh_command(
            {"connection_ref": original.reference}, "https://test.invalid"
        )

    assert store_at(tmp_path).load().credential == FIRST


@pytest.mark.asyncio
async def test_a_disconnect_clears_this_computer_and_says_so(monkeypatch, tmp_path):
    store = store_at(tmp_path)
    connected(store)
    wired(monkeypatch, store)

    result = await connector_command.run_disconnect_command({})

    assert result == {"ok": True, "connected": False}
    assert store_at(tmp_path).load() is None
    assert not (tmp_path / "connection.json").exists()


@pytest.mark.asyncio
async def test_a_disconnect_that_could_not_clear_is_not_reported_as_done(
    monkeypatch, tmp_path
):
    store = store_at(tmp_path)
    connected(store)
    wired(monkeypatch, store)

    # Patched at the file system rather than at the store: the file store
    # overrides ``clear`` outright, so a stand-in ``_erase`` is never called
    # and the cell would pass on a disconnect that worked.
    def cannot_unlink(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "unlink", cannot_unlink)

    result = await connector_command.run_disconnect_command({})

    assert result["ok"] is False
    assert result["stage"] == STAGE_CLEAR
    # Still connected, because the credential is still readable here. Saying
    # otherwise would tell the user a secret was destroyed that was not.
    assert result["connected"] is True


@pytest.mark.asyncio
async def test_a_disconnect_works_on_a_computer_whose_store_will_not_read(
    monkeypatch, tmp_path
):
    """Why the disconnect command takes no connection reference.

    A disconnect is the documented way out of a computer whose local copies
    disagree — the keychain store's own error messages name it as the way out.
    On such a computer ``load`` raises and there is no reference to check
    against, so a command that insisted on one would remove the only exit from
    the state it exists to exit.
    """
    store = store_at(tmp_path)
    connected(store)
    (tmp_path / "connection.json").write_bytes(b"not a record at all")
    wired(monkeypatch, store)

    result = await connector_command.run_disconnect_command({"connection_ref": "anything"})

    assert result == {"ok": True, "connected": False}
    assert not (tmp_path / "connection.json").exists()


@pytest.mark.asyncio
async def test_the_two_new_ops_reach_the_connector(monkeypatch):
    """Neither carries an agent_slug; prove the dispatcher routes them instead
    of falling through to "unsupported op"."""
    from puffo_agent.portal.control.client import execute_command

    seen = {}

    async def fake_refresh(params, server_url):
        seen["refresh"] = (params, server_url)
        return {"ok": True, "connection_ref": "conn-7"}

    async def fake_disconnect(params):
        seen["disconnect"] = params
        return {"ok": True, "connected": False}

    monkeypatch.setattr(connector_command, "run_refresh_command", fake_refresh)
    monkeypatch.setattr(connector_command, "run_disconnect_command", fake_disconnect)

    assert await execute_command(
        "connector.refresh", None, {"connection_ref": "conn-7"},
        server_url="https://example.test/",
    ) == {"ok": True, "connection_ref": "conn-7"}
    assert seen["refresh"] == ({"connection_ref": "conn-7"}, "https://example.test/")

    # A disconnect calls nobody, so it must not need to know where the server
    # is: the local credential's removal cannot depend on that.
    assert await execute_command("connector.disconnect", None, {}) == {
        "ok": True,
        "connected": False,
    }
    assert seen["disconnect"] == {}


@pytest.mark.asyncio
async def test_the_refresh_command_without_a_server_url_does_not_reach_the_connector():
    from puffo_agent.portal.control.client import execute_command

    result = await execute_command("connector.refresh", None, {"connection_ref": "c"})

    assert result["ok"] is False
