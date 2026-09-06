"""Gmail connect: presence gate, loopback checks, Layer-B projection.

The negative controls are the contract: no confirmation → the executor
is never spawned; a non-loopback callback or ready line dies before/at
the seam; token-only disconnect never reads as ``revoked``; and no
Layer-A detail (token_db, bundle paths, scope) can reach the Layer-B
projection or its consumers (EXECUTOR_INVOKE_SCHEMA v1 §5.1).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import stat
import textwrap

import pytest

from puffo_agent.portal.gmail_connect import executor as executor_mod
from puffo_agent.portal.gmail_connect import ops
from puffo_agent.portal.gmail_connect.native_confirm import ConfirmOutcome
from puffo_agent.portal.gmail_connect.executor import (
    ExecutorOutcome,
    ExecutorRefused,
    run_gmail_executor,
)
from puffo_agent.portal.gmail_connect.status_store import (
    REASONS,
    STATES,
    GmailConnectStatus,
    load_status,
    status_path,
    store_status,
)
from puffo_agent.portal.state import DaemonConfig, GmailConnectConfig


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    return tmp_path


def _configured(monkeypatch, **overrides) -> None:
    cfg = DaemonConfig()
    cfg.gmail_connect = GmailConnectConfig(
        enabled=True,
        executor_path="/usr/bin/true",
        data_root="/var/lib/gw",
        client_bundle_sha256="a" * 64,
        **overrides,
    )
    monkeypatch.setattr(ops, "_config", lambda: cfg)


@pytest.mark.asyncio
async def test_declined_confirm_never_spawns_executor(home, monkeypatch):
    """The native confirm is the gate: decline → zero executor calls and
    no state transition. Removing the gate in ``gmail_connect_initiate``
    turns this red."""
    _configured(monkeypatch)
    calls: list[dict] = []

    async def deny(prompt, *, timeout_s):
        return ConfirmOutcome.CANCELLED

    async def spy_executor(*a, **k):  # pragma: no cover - must not run
        calls.append({})
        return ExecutorOutcome(status="connected")

    monkeypatch.setattr(ops, "request_native_confirm", deny)
    monkeypatch.setattr(ops, "run_gmail_executor", spy_executor)

    res = await ops.gmail_connect_initiate({})

    assert res == {"ok": False, "state": "disconnected", "reason": "refused"}
    assert calls == []
    assert load_status().state == "disconnected"


@pytest.mark.asyncio
async def test_non_loopback_callback_refused_before_spawn(monkeypatch):
    spawned: list[object] = []

    async def spy_spawn(*a, **k):  # pragma: no cover - must not run
        spawned.append(a)
        raise AssertionError("spawn must not happen")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_spawn)
    with pytest.raises(ExecutorRefused):
        await run_gmail_executor(
            "/usr/bin/true",
            {"data_root": "/d", "expected_sha256": "a" * 64,
             "callback_host": "0.0.0.0"},
            flow_timeout_s=5,
        )
    assert spawned == []


@pytest.mark.asyncio
async def test_confirmed_flow_persists_layer_b_projection_only(home, monkeypatch):
    _configured(monkeypatch)
    sent: list[dict] = []

    async def allow(prompt, *, timeout_s):
        return ConfirmOutcome.CONFIRMED

    async def fake_executor(entrypoint, request, *, flow_timeout_s):
        sent.append(request)
        return ExecutorOutcome(status="connected")

    monkeypatch.setattr(ops, "request_native_confirm", allow)
    monkeypatch.setattr(ops, "run_gmail_executor", fake_executor)

    res = await ops.gmail_connect_initiate({})

    assert res == {"ok": True, "state": "connected", "reason": ""}
    # Invoke config comes from local daemon config, never from params.
    assert sent == [{
        "data_root": "/var/lib/gw", "expected_sha256": "a" * 64, "timeout": 300.0,
    }]
    on_disk = json.loads(status_path().read_text(encoding="utf-8"))
    # Layer B whitelist exactly (design v1.6 §3): state + reason.
    assert set(on_disk) == {"state", "reason", "updated_at"}
    assert on_disk["state"] == "connected"
    mode = stat.S_IMODE(os.stat(status_path()).st_mode)
    assert mode == 0o600


@pytest.mark.asyncio
async def test_failed_reason_reaches_projection_coarse(home, monkeypatch):
    _configured(monkeypatch)

    async def allow(prompt, *, timeout_s):
        return ConfirmOutcome.CONFIRMED

    async def failing_executor(entrypoint, request, *, flow_timeout_s):
        return ExecutorOutcome(status="failed", reason="callback_timeout")

    monkeypatch.setattr(ops, "request_native_confirm", allow)
    monkeypatch.setattr(ops, "run_gmail_executor", failing_executor)

    res = await ops.gmail_connect_initiate({})

    assert res == {"ok": False, "state": "failed", "reason": "callback_timeout"}
    after = load_status()
    assert (after.state, after.reason) == ("failed", "callback_timeout")


@pytest.mark.asyncio
async def test_token_only_disconnect_never_yields_revoked(home, monkeypatch):
    """Runbook v6 §7 negative control: ``revoked`` belongs to the
    composite Disconnect (grant + token both revoked). The token-only
    op caps the projection at ``disconnected`` and records the remote
    revoke as unconfirmed (invoke schema v1 has no revoke axis yet)."""
    _configured(monkeypatch)
    store_status(GmailConnectStatus(state="connected"))

    res = await ops.gmail_disconnect_token({})

    assert res["ok"] is True
    # The token-only scope is carried by the op name, not a returned
    # discriminator: a `scope` key would exceed the §5.1 whitelist and
    # collide with Layer-A's `scope` (the OAuth grant scope). Jeff 188515.
    assert "scope" not in res
    after = load_status()
    assert after.state == "disconnected"
    assert after.state != "revoked"
    assert after.reason == "revoke_unconfirmed"


@pytest.mark.asyncio
async def test_control_dispatch_routes_machine_level_gmail_ops(monkeypatch):
    """``execute_command`` must reach the gmail ops with no agent_slug —
    the agent-existence guard would otherwise swallow machine ops."""
    from puffo_agent.portal.control.client import execute_command

    async def fake_initiate(params):
        return {"ok": True, "state": "marker"}

    monkeypatch.setattr(ops, "gmail_connect_initiate", fake_initiate)
    res = await execute_command("gmail.connect_initiate", None, {})
    assert res == {"ok": True, "state": "marker"}

    async def fake_disconnect(params):
        return {"ok": True, "state": "disconnected", "reason": ""}

    monkeypatch.setattr(ops, "gmail_disconnect_token", fake_disconnect)
    res = await execute_command("gmail.disconnect_token", None, {})
    assert res == {"ok": True, "state": "disconnected", "reason": ""}


def _fake_executor_script(tmp_path, body: str) -> str:
    path = tmp_path / "fake_executor.py"
    path.write_text(
        "#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8"
    )
    path.chmod(0o755)
    return str(path)


@pytest.mark.asyncio
async def test_executor_schema_v1_protocol_and_layer_a_drop(tmp_path, home):
    """Real subprocess against invoke schema v1: config travels via one
    stdin JSON line (never argv), the ready/result event lines parse,
    and the Layer-A ``summary`` (token_db, scope, …) is dropped by
    construction — the outcome type carries only status + reason and
    the persisted projection scans clean."""
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys
        req = json.loads(sys.stdin.readline())
        assert req["data_root"] == "/d"
        assert req["expected_sha256"] == "a" * 64
        assert len(sys.argv) == 1
        print(json.dumps({"event": "ready",
            "redirect_uri": "http://127.0.0.1:49152/oauth2/callback"}))
        sys.stdout.flush()
        print(json.dumps({"event": "result", "status": "connected",
            "summary": {"scope": "gmail.send", "expires_in": 3599,
                        "has_refresh_token": True,
                        "token_db": "/private/secrets/tokens.db"}}))
        sys.stdout.flush()
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64, "timeout": 5.0},
        flow_timeout_s=10,
    )
    assert dataclasses.asdict(outcome) == {"status": "connected", "reason": ""}
    store_status(GmailConnectStatus(state="connected"))
    raw = status_path().read_text(encoding="utf-8")
    for leaked in ("token_db", "scope", "expires_in", "/private/secrets"):
        assert leaked not in raw


@pytest.mark.asyncio
async def test_prebind_failure_reason_survives_missing_ready(tmp_path):
    """Schema v1.1 §3: a pre-bind failure is 0 ready + 1 failed result.
    The daemon must surface the executor's real reason — mapping a
    missing ready line to timeout/protocol wholesale turns this red."""
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys
        sys.stdin.readline()
        print(json.dumps({"event": "result", "status": "failed",
                          "reason": "bundle_verify_failed"}))
        sys.stdout.flush()
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64},
        flow_timeout_s=5,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "bundle_verify_failed"


@pytest.mark.asyncio
async def test_out_of_enum_reason_is_clamped_to_internal_error(tmp_path):
    """The reason field is a closed enum (schema §4, six values); a
    free-text reason could carry secrets into the Layer-B projection,
    so the daemon clamps anything unknown to internal_error."""
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys
        sys.stdin.readline()
        print(json.dumps({"event": "result", "status": "failed",
                          "reason": "Exception: token=SENTINEL_ya29_SECRET"}))
        sys.stdout.flush()
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64},
        flow_timeout_s=5,
    )
    assert outcome.reason == "internal_error"
    assert "SENTINEL" not in outcome.reason


@pytest.mark.asyncio
async def test_connected_without_ready_is_protocol_failure(tmp_path):
    """A connected result with no prior ready line is out of contract:
    a flow that never bound loopback cannot have run consent."""
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys
        sys.stdin.readline()
        print(json.dumps({"event": "result", "status": "connected"}))
        sys.stdout.flush()
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64},
        flow_timeout_s=5,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "protocol"


@pytest.mark.asyncio
async def test_non_loopback_ready_line_kills_the_flow(tmp_path):
    """An executor advertising a routable redirect_uri is not the
    contract's executor: the daemon must kill it at the ready line,
    before any browser or consent could be in flight."""
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys, time
        sys.stdin.readline()
        print(json.dumps({"event": "ready",
            "redirect_uri": "http://evil.example:80/oauth2/callback"}))
        sys.stdout.flush()
        time.sleep(600)
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64},
        flow_timeout_s=5,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "non_loopback_ready"


@pytest.mark.asyncio
async def test_executor_silent_after_ready_is_killed_as_timeout(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(executor_mod, "RESULT_DEADLINE_MARGIN_S", 0.2)
    script = _fake_executor_script(
        tmp_path,
        '''
        import json, sys, time
        sys.stdin.readline()
        print(json.dumps({"event": "ready",
            "redirect_uri": "http://127.0.0.1:49152/oauth2/callback"}))
        sys.stdout.flush()
        time.sleep(600)
        ''',
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64},
        flow_timeout_s=0.3,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "timeout"


@pytest.mark.asyncio
async def test_status_route_serves_projection_only(home):
    from aiohttp.test_utils import TestClient, TestServer

    from puffo_agent.portal import rpc_service
    from puffo_agent.portal.local_service_auth import (
        issue_local_service_token,
        local_service_headers,
    )

    store_status(GmailConnectStatus(state="connected"))
    cfg = rpc_service.RpcServiceConfig(enabled=True, port=0)
    app = rpc_service.build_app(cfg)
    headers = local_service_headers(issue_local_service_token("any-agent"))
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/v1/rpc/any-agent/gmail-connect-status",
            json={},
            headers=headers,
        )
        assert resp.status == 200
        body = await resp.json()
    # Layer B exactly: any extra key (a Layer-A path, an account, …)
    # turns this red — the assertion is a whitelist, not a keyword scan.
    assert body == {"ok": True, "state": "connected", "reason": ""}


LEAK = "token exchange failed db=/private/secrets/tokens.db client_secret=GOCSPX-abc"


@pytest.mark.asyncio
async def test_out_of_roster_reason_is_clamped_before_layer_b(tmp_path, home, monkeypatch):
    """End-to-end via a real subprocess: a hostile/buggy executor must not
    put free text into the on-disk file, the Layer-B projection, or the
    control-plane response body.

    Negative control authored by Boris (review of e926d42, patch
    85fece2b…); kept as-is because it covers an exit the unit-level
    clamp test does not — ``ops`` returns the reason in its own
    ``reason`` field without passing through ``projection()``.
    """
    script = _fake_executor_script(
        tmp_path,
        """
        import json, sys
        json.loads(sys.stdin.readline())
        print(json.dumps({"event": "ready",
            "redirect_uri": "http://127.0.0.1:49152/oauth2/callback"}))
        sys.stdout.flush()
        print(json.dumps({"event": "result", "status": "failed",
            "reason": %r}))
        sys.stdout.flush()
        """ % LEAK,
    )
    cfg = DaemonConfig()
    cfg.gmail_connect = GmailConnectConfig(
        enabled=True, executor_path=script, data_root="/d",
        client_bundle_sha256="a" * 64,
    )
    monkeypatch.setattr(ops, "_config", lambda: cfg)
    async def allow(prompt, timeout_s=0.0):
        return ConfirmOutcome.CONFIRMED

    monkeypatch.setattr(ops, "request_native_confirm", allow)

    res = await ops.gmail_connect_initiate({})
    on_disk = status_path().read_text(encoding="utf-8")
    projection = load_status().projection()

    assert "client_secret" not in on_disk and "/private/secrets" not in on_disk
    assert projection["reason"] == "internal_error"
    assert "client_secret" not in json.dumps(res)


def test_poisoned_status_file_cannot_leak_through_the_load_path(home):
    """The trust-boundary clamp does not cover bytes already on disk.

    A file written by an older build (or a rollback, or a hand edit)
    is untrusted input too: ``load_status`` must clamp it, or the
    projection faithfully serves whatever is in the file.
    """
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": "failed", "reason": LEAK}), encoding="utf-8")

    projection = load_status().projection()

    assert projection["reason"] == "internal_error"
    assert "client_secret" not in json.dumps(projection)


def test_revoked_is_structurally_absent_not_merely_unused(home):
    """``revoked`` belongs to the composite Disconnect, which does not
    exist yet. "No call site mints it" is an enumeration of today's code;
    this pins the structural form — it is not constructible and not
    loadable, so an on-disk ``revoked`` cannot be served to the UI as a
    revocation that never happened.
    """
    assert "revoked" not in STATES

    assert GmailConnectStatus(state="revoked").state == "disconnected"

    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": "revoked", "reason": ""}), encoding="utf-8")
    assert load_status().state == "disconnected"


ALLOWED_REPLY_KEYS = {"ok", "state", "reason"}


@pytest.mark.asyncio
async def test_every_ops_exit_is_exactly_ok_state_reason(tmp_path, home, monkeypatch):
    """Jeff 188515: one shape for every control-plane reply.

    Walks every branch of both ops and asserts the key set never
    exceeds {ok, state, reason} and the reason is always a roster
    member — no ``error`` key, no ``scope`` discriminator, no
    ``updated_at``, and no free text anywhere.
    """
    async def deny(prompt, timeout_s=0.0):
        return ConfirmOutcome.CANCELLED

    async def allow(prompt, timeout_s=0.0):
        return ConfirmOutcome.CONFIRMED

    def check(reply):
        assert set(reply) <= ALLOWED_REPLY_KEYS, reply
        assert reply["reason"] in REASONS, reply
        assert reply["state"] in STATES, reply
        return reply

    # not configured -> executor_unavailable
    cfg = DaemonConfig()
    cfg.gmail_connect = GmailConnectConfig(enabled=False)
    monkeypatch.setattr(ops, "_config", lambda: cfg)
    assert check(await ops.gmail_connect_initiate({}))["reason"] == "executor_unavailable"

    # configured but no data_root / pin -> executor_unavailable
    cfg.gmail_connect = GmailConnectConfig(enabled=True, executor_path="/bin/true")
    assert check(await ops.gmail_connect_initiate({}))["reason"] == "executor_unavailable"

    script = _fake_executor_script(
        tmp_path,
        """
        import json, sys
        json.loads(sys.stdin.readline())
        print(json.dumps({"event": "ready",
            "redirect_uri": "http://127.0.0.1:49152/oauth2/callback"}))
        sys.stdout.flush()
        print(json.dumps({"event": "result", "status": "failed",
            "reason": "leak client_secret=GOCSPX-abc db=/private/secrets/x.db"}))
        sys.stdout.flush()
        """,
    )
    cfg.gmail_connect = GmailConnectConfig(
        enabled=True, executor_path=script, data_root="/d",
        client_bundle_sha256="a" * 64,
    )

    # user declines -> refused
    monkeypatch.setattr(ops, "request_native_confirm", deny)
    assert check(await ops.gmail_connect_initiate({}))["reason"] == "refused"

    # executor free text -> clamped, never verbatim
    monkeypatch.setattr(ops, "request_native_confirm", allow)
    reply = check(await ops.gmail_connect_initiate({}))
    assert reply["reason"] == "internal_error"
    assert "client_secret" not in json.dumps(reply)

    # already pending -> protocol
    store_status(GmailConnectStatus(state="pending"))
    assert check(await ops.gmail_connect_initiate({}))["reason"] == "protocol"

    # disconnect: token-only, carried by the op name and no scope key
    store_status(GmailConnectStatus(state="connected"))
    reply = check(await ops.gmail_disconnect_token({}))
    assert reply["reason"] == "revoke_unconfirmed"
    assert "scope" not in reply
    assert check(await ops.gmail_disconnect_token({}))["reason"] == ""


def test_updated_at_stays_on_disk_and_never_leaves(home):
    """Jeff 188515 #1: the persisted record may carry updated_at; no
    outward-facing surface may. Guards against someone "simplifying"
    the store by serving the file straight through."""
    store_status(GmailConnectStatus(state="connected"))

    on_disk = json.loads(status_path().read_text(encoding="utf-8"))
    assert "updated_at" in on_disk

    assert set(load_status().projection()) == {"state", "reason"}


def test_reply_clamps_free_text_on_its_own_leg():
    """``_reply``'s roster check is defence in depth: today the boundary
    clamp means it never sees free text, so no end-to-end test can make
    it fail. Exercised directly, or the leg is unobservable and a later
    refactor could delete it silently.
    """
    reply = ops._reply(
        "exchange failed client_secret=GOCSPX-abc", ok=False, state="failed"
    )

    assert reply == {"ok": False, "state": "failed", "reason": "internal_error"}


@pytest.mark.asyncio
async def test_six_confirm_facts_each_pin_response_and_persistence(
    tmp_path, home, monkeypatch
):
    """Jeff 188539's final table — six facts, both legs each.

    ``refused`` is reserved for a person who provably declined; the other
    non-confirming facts are failures of this machine and must say so.
    Collapsing them reported "cancelled" on every non-macOS host, which
    is untrue and unactionable (Boris 188533). ``confirm_timeout`` is
    kept distinct from the executor's ``timeout`` for the same reason:
    nobody answering the dialog never contacted Google, while a silent
    executor may already have opened the consent page (Boris 188538).

      person clicks Cancel   -> (disconnected, refused)         NOT persisted
      nobody answers dialog  -> (failed, confirm_timeout)       persisted
      no dialog backend      -> (failed, confirm_unavailable)   persisted
      backend failed to run  -> (failed, confirm_unavailable)   persisted
      executor refuses       -> (failed, refused)               persisted
      executor read timeout  -> (failed, timeout)               persisted
    """
    _configured(monkeypatch)

    def confirm_as(outcome):
        async def stub(prompt, *, timeout_s):
            return outcome
        monkeypatch.setattr(ops, "request_native_confirm", stub)

    async def check(outcome, expect_state, expect_reason, *, persisted):
        store_status(GmailConnectStatus(state="disconnected"))
        confirm_as(outcome)
        reply = await ops.gmail_connect_initiate({})
        assert reply == {
            "ok": False, "state": expect_state, "reason": expect_reason
        }, outcome
        after = load_status()
        if persisted:
            assert (after.state, after.reason) == (expect_state, expect_reason), outcome
        else:
            # a transient user choice must never be recorded as a failure
            assert (after.state, after.reason) == ("disconnected", ""), outcome

    await check(ConfirmOutcome.CANCELLED, "disconnected", "refused", persisted=False)
    await check(ConfirmOutcome.TIMEOUT, "failed", "confirm_timeout", persisted=True)
    await check(
        ConfirmOutcome.UNAVAILABLE, "failed", "confirm_unavailable", persisted=True
    )

    # fifth fact: the executor refuses before spawn — same reason as a
    # cancel, told apart by state and by being recorded.
    async def refuse(*a, **k):
        raise ExecutorRefused("entrypoint is not executable")

    store_status(GmailConnectStatus(state="disconnected"))
    confirm_as(ConfirmOutcome.CONFIRMED)
    monkeypatch.setattr(ops, "run_gmail_executor", refuse)
    reply = await ops.gmail_connect_initiate({})
    assert reply == {"ok": False, "state": "failed", "reason": "refused"}
    assert (load_status().state, load_status().reason) == ("failed", "refused")

    # sixth fact: the executor started and then went silent. Distinct
    # from nobody answering the dialog — this one may already have
    # opened the consent page, so the user needs a different hint.
    async def read_timeout(*a, **k):
        return ExecutorOutcome(status="failed", reason="timeout")

    store_status(GmailConnectStatus(state="disconnected"))
    monkeypatch.setattr(ops, "run_gmail_executor", read_timeout)
    reply = await ops.gmail_connect_initiate({})
    assert reply == {"ok": False, "state": "failed", "reason": "timeout"}
    assert (load_status().state, load_status().reason) == ("failed", "timeout")

    # the two timeouts are different facts, not one value wearing two hats
    assert ops._CONFIRM_REFUSALS[ConfirmOutcome.TIMEOUT][1] == "confirm_timeout"


@pytest.mark.parametrize(
    "returncode, stderr, expected",
    [
        (0, b"", "CONFIRMED"),
        (1, b"0:17: execution error: User canceled. (-128)", "CANCELLED"),
        # anything else non-zero: we do NOT know a person declined
        (1, b"0:4: execution error: The variable x is not defined. (-2753)",
         "UNAVAILABLE"),
        (127, b"", "UNAVAILABLE"),
        (1, b"", "UNAVAILABLE"),
    ],
)
@pytest.mark.asyncio
async def test_confirm_classification_fails_closed(
    returncode, stderr, expected, monkeypatch
):
    """Fail closed: only the documented cancel signature (-128) may be
    read as a person declining. Every other non-zero exit is UNAVAILABLE.

    Drives the classification itself with a fake subprocess — the mapping
    table test cannot see this, so flipping the fallback to CANCELLED
    (claiming the user declined when we do not know) would otherwise stay
    green on every platform.
    """
    from puffo_agent.portal.gmail_connect import native_confirm as nc

    class FakeProc:
        def __init__(self):
            self.returncode = returncode

        async def communicate(self):
            return b"", stderr

    async def fake_exec(*a, **k):
        return FakeProc()

    monkeypatch.setattr(nc.sys, "platform", "darwin")
    monkeypatch.setattr(nc.asyncio, "create_subprocess_exec", fake_exec)

    outcome = await nc.request_native_confirm("prompt")

    assert outcome.name == expected


def test_only_a_provable_cancel_escapes_being_recorded_as_failure():
    from puffo_agent.portal.gmail_connect import native_confirm as nc

    assert ops._CONFIRM_REFUSALS[nc.ConfirmOutcome.CANCELLED][0] == "disconnected"
    assert ops._CONFIRM_REFUSALS[nc.ConfirmOutcome.TIMEOUT][0] == "failed"
    assert ops._CONFIRM_REFUSALS[nc.ConfirmOutcome.UNAVAILABLE][0] == "failed"


@pytest.mark.parametrize(
    "label, body",
    [
        ("malformed json", 'print("{not json")'),
        ("unknown event type",
         'print(json.dumps({"event": "hello"}))'),
        ("terminal status is neither connected nor failed",
         'print(json.dumps({"event":"ready",'
         '"redirect_uri":"http://127.0.0.1:1/oauth2/callback"}));'
         'sys.stdout.flush();'
         'print(json.dumps({"event":"result","status":"weird"}))'),
        ("connected without a ready line",
         'print(json.dumps({"event":"result","status":"connected"}))'),
    ],
)
@pytest.mark.asyncio
async def test_protocol_is_a_deliberate_merge_not_an_accident(
    label, body, tmp_path, home
):
    """Jeff 188542: ``protocol`` intentionally covers several low-level
    faults, because they mean one actionable thing to a user — the daemon
    cannot understand or trust this executor session — and the recovery is
    identical (retry; if it persists, report the connector).

    The rule is NOT "every low-level path needs its own reason"; it is
    that facts with different recovery actions must not collapse, while
    paths sharing a recovery action may be merged on purpose. So this
    pins the merge in both directions: every representative sub-class
    must land on exactly ``protocol``, and none may be projected into a
    narrower, unproven explanation.
    """
    script = _fake_executor_script(
        tmp_path,
        "import json, sys\njson.loads(sys.stdin.readline())\n"
        + body
        + "\nsys.stdout.flush()\n",
    )
    outcome = await run_gmail_executor(
        script,
        {"data_root": "/d", "expected_sha256": "a" * 64, "timeout": 2.0},
        flow_timeout_s=2.0,
    )

    assert outcome.status == "failed", label
    assert outcome.reason == "protocol", label


# Jeff 188550 / Boris 188551: the deliberate merge keeps an operator
# signal, without giving the UI a narrower story and without opening a
# new text sink.
_DIAGNOSED = [
    ("parse_error", 'print("{not json")'),
    ("unknown_terminal_status",
     'print(json.dumps({"event":"ready",'
     '"redirect_uri":"http://127.0.0.1:1/oauth2/callback"}));'
     'sys.stdout.flush();'
     'print(json.dumps({"event":"result","status":"weird"}))'),
    ("connected_without_ready",
     'print(json.dumps({"event":"result","status":"connected"}))'),
]


@pytest.mark.parametrize("cause, body", _DIAGNOSED)
@pytest.mark.asyncio
async def test_protocol_subpath_records_its_local_cause(
    cause, body, tmp_path, home, caplog
):
    """Leg 2: the right local cause is present — not merely 'some
    diagnostic exists'. Leg 1 (outward + persistence unchanged) is
    pinned by the merge test above."""
    script = _fake_executor_script(
        tmp_path,
        "import json, sys\njson.loads(sys.stdin.readline())\n" + body
        + "\nsys.stdout.flush()\n",
    )
    with caplog.at_level(logging.WARNING, logger=executor_mod.__name__):
        outcome = await run_gmail_executor(
            script,
            {"data_root": "/d", "expected_sha256": "a" * 64, "timeout": 2.0},
            flow_timeout_s=2.0,
        )

    assert (outcome.status, outcome.reason) == ("failed", "protocol")
    assert f"cause={cause}" in caplog.text


@pytest.mark.asyncio
async def test_diagnostic_carries_the_cause_and_nothing_else(
    tmp_path, home, caplog
):
    """Leg 3: the diagnostic is a NEW text sink, and this whole round
    began with free text reaching a sink it should not have. Nothing
    from the executor's own output may ride along."""
    sentinel = "SENTINEL-client_secret=GOCSPX-abc-/private/secrets/tok.db"
    # Valid JSON with an unknown event type, on purpose: that path raises
    # a ValueError the daemon builds itself, so a mutation that folds the
    # offending line into the message WOULD leak. Malformed JSON cannot
    # discriminate here — json's own error never quotes the input, so a
    # sentinel test built on it passes no matter what the code does.
    script = _fake_executor_script(
        tmp_path,
        "import json, sys\njson.loads(sys.stdin.readline())\n"
        f'print(json.dumps({{"event": "hello", "note": "{sentinel}"}}))\n'
        "sys.stdout.flush()\n",
    )
    with caplog.at_level(logging.WARNING, logger=executor_mod.__name__):
        outcome = await run_gmail_executor(
            script,
            {"data_root": "/d", "expected_sha256": "a" * 64, "timeout": 2.0},
            flow_timeout_s=2.0,
        )

    assert (outcome.status, outcome.reason) == ("failed", "protocol")
    assert "cause=parse_error" in caplog.text
    # neither the offending line nor any credential-shaped text
    assert "SENTINEL" not in caplog.text
    assert "client_secret" not in caplog.text
    assert "/private/secrets" not in caplog.text


def test_diagnostic_causes_are_a_closed_set_defined_once(caplog):
    """Boris 188551: three call sites typing their own literal would
    drift silently. A non-member must be dropped, not written through —
    otherwise the sink accepts free text again by another door."""
    assert executor_mod.DIAGNOSTIC_CAUSES == (
        "parse_error",
        "connected_without_ready",
        "unknown_terminal_status",
        "non_loopback_ready",
    )
    with caplog.at_level(logging.WARNING, logger=executor_mod.__name__):
        executor_mod._diagnose("client_secret=GOCSPX-leaked-through-the-cause")

    assert "GOCSPX" not in caplog.text
    assert "client_secret" not in caplog.text
    assert "outside the closed set" in caplog.text
