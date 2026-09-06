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
    EXECUTOR_REASONS,
    PERSISTABLE_PAIRS,
    UNTRUSTED_STATUS_PAIR,
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
    fields = {
        "enabled": True,
        "executor_path": "/usr/bin/true",
        "data_root": "/var/lib/gw",
        "client_bundle_sha256": "a" * 64,
    }
    fields.update(overrides)
    cfg = DaemonConfig()
    cfg.gmail_connect = GmailConnectConfig(**fields)
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

    Jeff 188618 §1 moved the persistence boundary: an explicit local Yes
    is where a connect may change durable state, so none of the three
    non-confirming facts persist any more. The REPLY still tells them
    apart — that is the half that carries the information.

      person clicks Cancel   -> (disconnected, refused)         NOT persisted
      nobody answers dialog  -> (failed, confirm_timeout)       NOT persisted
      no dialog backend      -> (failed, confirm_unavailable)   NOT persisted
      backend failed to run  -> (failed, confirm_unavailable)   NOT persisted
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
            # nothing ran, so nothing about the machine changed
            assert (after.state, after.reason) == ("disconnected", ""), outcome

    await check(ConfirmOutcome.CANCELLED, "disconnected", "refused", persisted=False)
    await check(ConfirmOutcome.TIMEOUT, "failed", "confirm_timeout", persisted=False)
    await check(
        ConfirmOutcome.UNAVAILABLE, "failed", "confirm_unavailable", persisted=False
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
        executor_mod._diagnose(cause="client_secret=GOCSPX-leaked-through-the-cause")

    assert "GOCSPX" not in caplog.text
    assert "client_secret" not in caplog.text
    assert "outside the closed set" in caplog.text


def test_every_diagnose_call_site_names_a_known_static_cause():
    """Jeff 188561: the guard covers this PR's own diagnostic sites, so it
    lives with the implementation and cannot drift away from it.

    Enumerates every ``_diagnose`` call in the module source: each must
    pass exactly one keyword ``cause=`` whose value is a static string in
    ``DIAGNOSTIC_CAUSES``. Positional args, computed values and unknown
    names are all rejected — a computed cause is how free text would get
    back into the sink after we closed the direct door.
    """
    import ast
    import pathlib

    package = pathlib.Path(executor_mod.__file__).parent
    sources = sorted(package.glob("*.py"))
    assert sources, f"no package sources under {package} — guard would be blind"

    sites = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        sites += [
            (path.name, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "_diagnose")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_diagnose"
                )
            )
        ]

    assert sites, "no _diagnose call sites found — guard would vacuously pass"
    for name, call in sites:
        where = f"{name}:{call.lineno}"
        assert not call.args, f"{where}: cause must be keyword-only, not positional"
        assert [kw.arg for kw in call.keywords] == ["cause"], where
        value = call.keywords[0].value
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
            f"{where}: cause must be a static string, never computed"
        )
        assert value.value in executor_mod.DIAGNOSTIC_CAUSES, f"{where}: {value.value}"


@pytest.mark.asyncio
async def test_non_loopback_ready_records_its_local_cause(tmp_path, home, caplog):
    """Jeff 188561 #1: outward stays non_loopback_ready, and the local
    cause is present too — the previous test only checked the outward
    half."""
    script = _fake_executor_script(
        tmp_path,
        """
        import json, sys
        json.loads(sys.stdin.readline())
        print(json.dumps({"event": "ready",
            "redirect_uri": "http://10.0.0.9:8080/oauth2/callback"}))
        sys.stdout.flush()
        """,
    )
    with caplog.at_level(logging.WARNING, logger=executor_mod.__name__):
        outcome = await run_gmail_executor(
            script,
            {"data_root": "/d", "expected_sha256": "a" * 64, "timeout": 2.0},
            flow_timeout_s=2.0,
        )

    assert (outcome.status, outcome.reason) == ("failed", "non_loopback_ready")
    assert "cause=non_loopback_ready" in caplog.text


@pytest.mark.parametrize("label, body", [
    ("malformed json", 'print("{not json")'),
    ("unknown event type", 'print(json.dumps({"event": "hello"}))'),
    ("unknown terminal status",
     'print(json.dumps({"event":"ready",'
     '"redirect_uri":"http://127.0.0.1:1/oauth2/callback"}));'
     'sys.stdout.flush();'
     'print(json.dumps({"event":"result","status":"weird"}))'),
    ("connected without ready",
     'print(json.dumps({"event":"result","status":"connected"}))'),
])
@pytest.mark.asyncio
async def test_protocol_subclasses_through_ops_response_and_persistence(
    label, body, tmp_path, home, monkeypatch
):
    """Jeff 188561 #2: the merge test drove the executor directly, so it
    proved the outcome but not what the control plane says or what lands
    on disk. This drives the whole op."""
    script = _fake_executor_script(
        tmp_path,
        "import json, sys\njson.loads(sys.stdin.readline())\n" + body
        + "\nsys.stdout.flush()\n",
    )
    _configured(monkeypatch, executor_path=script)

    async def allow(prompt, *, timeout_s):
        return ConfirmOutcome.CONFIRMED

    monkeypatch.setattr(ops, "request_native_confirm", allow)
    store_status(GmailConnectStatus(state="disconnected"))

    reply = await ops.gmail_connect_initiate({})

    assert reply == {"ok": False, "state": "failed", "reason": "protocol"}, label
    after = load_status()
    assert (after.state, after.reason) == ("failed", "protocol"), label


@pytest.mark.asyncio
async def test_no_backend_and_backend_failure_are_separate_producers(monkeypatch):
    """Jeff 188561 #3: the mapping test stubs a single UNAVAILABLE, which
    cannot show that BOTH production paths reach it. Drive each one."""
    from puffo_agent.portal.gmail_connect import native_confirm as nc

    # (a) no dialog backend at all — any non-macOS host
    monkeypatch.setattr(nc.sys, "platform", "linux")
    assert await nc.request_native_confirm("p") is nc.ConfirmOutcome.UNAVAILABLE

    # (b) backend present but fails to launch
    monkeypatch.setattr(nc.sys, "platform", "darwin")

    async def boom(*a, **k):
        raise OSError("osascript missing")

    monkeypatch.setattr(nc.asyncio, "create_subprocess_exec", boom)
    assert await nc.request_native_confirm("p") is nc.ConfirmOutcome.UNAVAILABLE


@pytest.mark.asyncio
async def test_dialog_timeout_is_its_own_producer(monkeypatch):
    """Jeff 188561 #3: the 120s no-answer path, driven for real rather
    than stubbed as an outcome."""
    from puffo_agent.portal.gmail_connect import native_confirm as nc

    killed: list[bool] = []

    class HangingProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            killed.append(True)

        async def wait(self):
            return -9

    async def hang(*a, **k):
        return HangingProc()

    monkeypatch.setattr(nc.sys, "platform", "darwin")
    monkeypatch.setattr(nc.asyncio, "create_subprocess_exec", hang)

    outcome = await nc.request_native_confirm("p", timeout_s=0.05)

    assert outcome is nc.ConfirmOutcome.TIMEOUT
    assert killed, "a dialog that timed out must be killed, not left running"


# Jeff 188586 / 188590: an ill-formed pin is a build/config defect, not a
# security event. It is treated as "not configured" so the refusal happens
# before the user is ever shown a dialog, and `bundle_verify_failed` keeps
# its single meaning: a VALID pin that did not match the bundle's bytes.
#
# The trailing-newline case is the discriminating one — it is exactly what
# `re.match(r"[0-9a-f]{64}$", …)` lets through and `fullmatch` rejects
# (Boris 188589), and `sha256sum > pin` is how a build would produce it.
_ILL_FORMED_PINS = [
    pytest.param("A" * 64, id="uppercase"),
    pytest.param("a" * 63 + "F", id="mixed-case"),
    pytest.param("hello", id="not-hex"),
    pytest.param("a" * 40, id="too-short"),
    pytest.param("a" * 65, id="too-long"),
    pytest.param("g" * 64, id="non-hex-letters"),
    pytest.param("a" * 64 + "\n", id="trailing-newline"),
    pytest.param(" " + "a" * 64, id="leading-space"),
]


@pytest.mark.parametrize("pin", _ILL_FORMED_PINS)
def test_ill_formed_pin_is_dropped_at_the_config_boundary(pin):
    """After construction there is no invalid pin left for any consumer
    to pick up — the value is gone, not merely unused."""
    assert GmailConnectConfig(client_bundle_sha256=pin).client_bundle_sha256 == ""


@pytest.mark.parametrize("pin", _ILL_FORMED_PINS)
@pytest.mark.asyncio
async def test_ill_formed_pin_refuses_before_any_dialog_or_spawn(
    home, monkeypatch, pin
):
    """The hard acceptance shape (Jeff 188590): `(failed,
    executor_unavailable)`, zero native confirmations, zero spawns —
    so a build defect never reaches Google and never interrupts the
    user with a dialog it is going to fail after anyway."""
    _configured(monkeypatch, client_bundle_sha256=pin)
    confirms: list[str] = []
    spawns: list[object] = []

    async def spy_confirm(prompt, *, timeout_s):  # pragma: no cover - must not run
        confirms.append(prompt)
        return ConfirmOutcome.CONFIRMED

    async def spy_executor(*a, **k):  # pragma: no cover - must not run
        spawns.append(a)
        return ExecutorOutcome(status="connected")

    monkeypatch.setattr(ops, "request_native_confirm", spy_confirm)
    monkeypatch.setattr(ops, "run_gmail_executor", spy_executor)

    res = await ops.gmail_connect_initiate({})

    # Jeff 188607 §5: this used to assert only ``reason`` while the
    # docstring claimed the whole pair — the green was real but did not
    # cover the half it was quoted for (Boris 188604).
    assert res == {"ok": False, "state": "failed", "reason": "executor_unavailable"}
    assert confirms == [], "the user must not be prompted for a config defect"
    assert spawns == [], "no spawn, hence no Google call and no token write"


def test_valid_lowercase_pin_is_kept_verbatim():
    """The positive control: without it, a validator that blanked every
    pin would pass every test above."""
    pin = "0123456789abcdef" * 4
    assert GmailConnectConfig(client_bundle_sha256=pin).client_bundle_sha256 == pin


def test_loader_applies_the_pin_gate_to_config_on_disk(tmp_path, monkeypatch):
    """The path an installer actually exercises: a bad pin in the YAML
    is gone by the time anything reads the config."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    good = "0123456789abcdef" * 4
    for pin, expected in (("AB" * 32, ""), (good, good)):
        (tmp_path / "daemon.yml").write_text(
            "gmail_connect:\n"
            "  enabled: true\n"
            "  executor_path: /usr/bin/true\n"
            "  data_root: /var/lib/gw\n"
            f"  client_bundle_sha256: {pin}\n",
            encoding="utf-8",
        )
        assert DaemonConfig.load().gmail_connect.client_bundle_sha256 == expected


def test_invalid_pin_diagnostic_never_carries_the_value(caplog):
    """Jeff 188586: a fixed cause only. A pin is not itself a secret,
    but this field is one hop from bundle paths and credentials, and
    the rule is that daemon diagnostics carry causes, not values."""
    pin = "SECRETLOOKING" + "Z" * 51
    with caplog.at_level(logging.WARNING):
        GmailConnectConfig(client_bundle_sha256=pin)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "invalid_client_bundle_sha256" in logged
    assert pin not in logged
    assert "SECRETLOOKING" not in logged


# ---------------------------------------------------------------------------
# Windows native confirm (Jeff 188597 / 188599, Jeremy 188600: acceptance
# runs on Windows). This host is macOS, so what is provable here is the
# producer path — the shipped dialog script and the classification — not
# that a real dialog appears. That last cell needs Jeremy's machine.
# ---------------------------------------------------------------------------


def test_backend_dispatch_covers_exactly_the_two_supported_hosts(monkeypatch):
    from puffo_agent.portal.gmail_connect import native_confirm as nc

    monkeypatch.setattr(nc.sys, "platform", "darwin")
    argv, classify = nc._backend("hello")
    assert argv[0] == "osascript" and classify is nc._classify_osascript

    monkeypatch.setattr(nc.sys, "platform", "win32")
    argv, classify = nc._backend("hello")
    assert argv[0] == nc.sys.executable and classify is nc._classify_windows
    # The prompt travels as an argument, never spliced into the source.
    assert argv[2] == nc._WINDOWS_DIALOG_SOURCE
    assert argv[3:] == ["hello", nc.DIALOG_TITLE]

    for platform in ("linux", "freebsd", "cygwin"):
        monkeypatch.setattr(nc.sys, "platform", platform)
        assert nc._backend("hello") is None, platform


@pytest.mark.parametrize(
    "returncode,expected",
    [
        (0, ConfirmOutcome.CONFIRMED),
        (7, ConfirmOutcome.CANCELLED),
        (9, ConfirmOutcome.UNAVAILABLE),
        # 1 and 2 are what a Python that failed to start exits with. If
        # either were read as a decline we would tell the user they said
        # no on a machine where the dialog never ran — the same failure
        # the osascript branch avoids by matching -128 exactly.
        (1, ConfirmOutcome.UNAVAILABLE),
        (2, ConfirmOutcome.UNAVAILABLE),
        (-9, ConfirmOutcome.UNAVAILABLE),
        (127, ConfirmOutcome.UNAVAILABLE),
    ],
)
def test_windows_classification_fails_closed(returncode, expected):
    from puffo_agent.portal.gmail_connect import native_confirm as nc

    assert nc._classify_windows(returncode, b"") is expected


def _run_dialog_script(message_box_returns: int):
    """Execute the SHIPPED dialog source with a stubbed ``ctypes``.

    Running the real string rather than a paraphrase is the point: a
    test that re-implements the exit-code logic would stay green while
    the shipped script drifted.
    """
    import subprocess
    import sys as _sys

    from puffo_agent.portal.gmail_connect import native_confirm as nc

    harness = textwrap.dedent(
        f"""
        import sys, types
        calls = []
        user32 = types.SimpleNamespace(
            MessageBoxW=lambda *a: (calls.append(a), {message_box_returns})[1]
        )
        fake = types.ModuleType("ctypes")
        fake.windll = types.SimpleNamespace(user32=user32)
        sys.modules["ctypes"] = fake
        try:
            exec(compile({nc._WINDOWS_DIALOG_SOURCE!r}, "<dialog>", "exec"))
        finally:
            sys.stderr.write(repr(calls))
        """
    )
    return subprocess.run(
        [_sys.executable, "-c", harness, "THE PROMPT", "THE TITLE"],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "message_box_returns,exit_code",
    [
        (6, 0),    # IDYES  -> confirmed
        (7, 7),    # IDNO   -> declined
        (0, 9),    # MessageBoxW itself failed -> NOT a decline
        (2, 9),    # IDCANCEL cannot occur on a Yes/No box; still not a decline
        (32000, 9),
    ],
)
def test_shipped_dialog_script_maps_return_codes(message_box_returns, exit_code):
    done = _run_dialog_script(message_box_returns)
    assert done.returncode == exit_code, done.stderr


def test_shipped_dialog_script_passes_text_as_data_with_the_safe_flags():
    """The prompt and title reach MessageBoxW as arguments, and the box
    is Yes/No defaulted to No — so a dismissal that is not a deliberate
    Yes cannot read as consent."""
    from puffo_agent.portal.gmail_connect import native_confirm as nc

    done = _run_dialog_script(6)
    assert "THE PROMPT" in done.stderr and "THE TITLE" in done.stderr
    assert str(nc._WIN_DIALOG_FLAGS) in done.stderr
    # Default button is No, and the box is Yes/No rather than OK/Cancel.
    assert nc._WIN_DIALOG_FLAGS & 0x00000100  # MB_DEFBUTTON2
    assert nc._WIN_DIALOG_FLAGS & 0x00000004  # MB_YESNO

def test_hostile_prompt_stays_in_argv_and_out_of_the_source(monkeypatch):
    """A prompt is data. It reaches the child as an argument, and the
    executed source is a fixed constant — so there is no quoting to get
    wrong, unlike the osascript branch which must escape its string."""
    from puffo_agent.portal.gmail_connect import native_confirm as nc

    hostile = 'x"; import os; os.system("touch /tmp/pwned"); "'
    monkeypatch.setattr(nc.sys, "platform", "win32")
    argv, _ = nc._backend(hostile)

    source = argv[2]
    assert source == nc._WINDOWS_DIALOG_SOURCE
    assert "pwned" not in source
    assert hostile == argv[3]
    assert "sys.argv[1]" in source


# Jeff 188607 §4. Two facts per cell, and they pull in opposite
# directions: the REPLY must be fixed regardless of what is on disk,
# and the DISK must be untouched regardless of what the reply says.
# Before this ruling the reply inherited the stored state, so a
# pre-flight refusal on a connected machine answered "connected".
_PRIOR_STATES = ("disconnected", "connected", "failed", "pending")

_PREFLIGHT_DEFECTS = [
    pytest.param({"enabled": False}, id="disabled"),
    pytest.param({"executor_path": ""}, id="no-executor-path"),
    pytest.param({"data_root": ""}, id="no-data-root"),
    pytest.param({"client_bundle_sha256": ""}, id="no-pin"),
    pytest.param({"client_bundle_sha256": "A" * 64}, id="ill-formed-pin"),
]


@pytest.mark.parametrize("prior", _PRIOR_STATES)
@pytest.mark.parametrize("defect", _PREFLIGHT_DEFECTS)
@pytest.mark.asyncio
async def test_preflight_refusal_is_fixed_and_never_rewrites_the_projection(
    home, monkeypatch, prior, defect
):
    _configured(monkeypatch, **defect)
    store_status(GmailConnectStatus(state=prior))
    before = status_path().read_bytes()

    confirms: list[str] = []
    spawns: list[object] = []

    async def spy_confirm(prompt, *, timeout_s):  # pragma: no cover - must not run
        confirms.append(prompt)
        return ConfirmOutcome.CONFIRMED

    async def spy_executor(*a, **k):  # pragma: no cover - must not run
        spawns.append(a)
        return ExecutorOutcome(status="connected")

    monkeypatch.setattr(ops, "request_native_confirm", spy_confirm)
    monkeypatch.setattr(ops, "run_gmail_executor", spy_executor)

    res = await ops.gmail_connect_initiate({})

    # The config checks run before the re-entry guard, so a machine
    # that is misconfigured answers for the config no matter what was
    # in flight. `protocol` is reachable only with a valid config —
    # the test below.
    assert res == {"ok": False, "state": "failed", "reason": "executor_unavailable"}
    # Byte-for-byte, not field-by-field: `updated_at` moving would mean
    # something wrote, even if the values happened to match.
    assert status_path().read_bytes() == before
    assert confirms == []
    assert spawns == []


@pytest.mark.asyncio
async def test_pending_reentry_reports_failed_and_stays_pending(home, monkeypatch):
    """The connect already running is untouched: the second caller is
    told its own call failed, and the in-flight one keeps the machine
    in ``pending``."""
    _configured(monkeypatch)
    store_status(GmailConnectStatus(state="pending"))
    before = status_path().read_bytes()

    res = await ops.gmail_connect_initiate({})

    assert res == {"ok": False, "state": "failed", "reason": "protocol"}
    assert load_status().state == "pending"
    assert status_path().read_bytes() == before


def test_reply_state_is_required_so_no_branch_can_inherit_the_stored_state():
    """The structural half of Jeff 188607 §1: it is not that the three
    branches were fixed, it is that a fourth one cannot repeat them
    silently. Restoring a default would make this the only red."""
    import inspect

    state = inspect.signature(ops._reply).parameters["state"]
    assert state.default is inspect.Parameter.empty
    # Names actually referenced by the compiled body, so the docstring
    # is free to explain what it no longer does.
    assert "load_status" not in ops._reply.__code__.co_names


# Jeff 188618: an explicit local Yes is the boundary at which a connect
# may change durable state. The tests below hold both sides of it — the
# three non-confirming outcomes must leave the projection alone, and the
# confirmed path must still record, so "fixing" one side cannot silently
# delete the other.
@pytest.mark.parametrize(
    "outcome,expected",
    [
        (ConfirmOutcome.CANCELLED, ("disconnected", "refused")),
        (ConfirmOutcome.TIMEOUT, ("failed", "confirm_timeout")),
        (ConfirmOutcome.UNAVAILABLE, ("failed", "confirm_unavailable")),
    ],
)
@pytest.mark.parametrize("prior", ("disconnected", "failed"))
@pytest.mark.asyncio
async def test_no_yes_means_no_write(home, monkeypatch, outcome, expected, prior):
    """`connected` is not in `prior` here: an already-connected machine
    never reaches the dialog at all (see the idempotent no-op below), so
    asking for it in this matrix would test the guard, not the write."""
    _configured(monkeypatch)
    store_status(GmailConnectStatus(state=prior))
    before = status_path().read_bytes()
    spawns: list[object] = []

    async def stub(prompt, *, timeout_s):
        return outcome

    async def spy_executor(*a, **k):  # pragma: no cover - must not run
        spawns.append(a)
        return ExecutorOutcome(status="connected")

    monkeypatch.setattr(ops, "request_native_confirm", stub)
    monkeypatch.setattr(ops, "run_gmail_executor", spy_executor)

    res = await ops.gmail_connect_initiate({})

    state, reason = expected
    assert res == {"ok": False, "state": state, "reason": reason}
    assert status_path().read_bytes() == before
    assert spawns == []


@pytest.mark.asyncio
async def test_already_connected_is_an_idempotent_no_op(home, monkeypatch):
    """Jeff 188618 §2. Frozen design v1.6 §4 has no `connected ->
    pending` edge, so a second Connect on a connected machine is a
    success that changes nothing — rather than a dialog the user did not
    ask for and a transition the state machine does not define."""
    _configured(monkeypatch)
    store_status(GmailConnectStatus(state="connected"))
    before = status_path().read_bytes()
    confirms: list[str] = []
    spawns: list[object] = []

    async def spy_confirm(prompt, *, timeout_s):  # pragma: no cover - must not run
        confirms.append(prompt)
        return ConfirmOutcome.CONFIRMED

    async def spy_executor(*a, **k):  # pragma: no cover - must not run
        spawns.append(a)
        return ExecutorOutcome(status="connected")

    monkeypatch.setattr(ops, "request_native_confirm", spy_confirm)
    monkeypatch.setattr(ops, "run_gmail_executor", spy_executor)

    res = await ops.gmail_connect_initiate({})

    assert res == {"ok": True, "state": "connected", "reason": ""}
    assert status_path().read_bytes() == before
    assert confirms == [], "a connected machine must not be prompted again"
    assert spawns == []


@pytest.mark.asyncio
async def test_the_no_op_guard_does_not_mask_a_config_defect(home, monkeypatch):
    """The guard sits AFTER the config pre-flight, so a connected machine
    whose build is broken still reports the defect instead of a cheerful
    no-op. Moving the guard earlier turns this red."""
    _configured(monkeypatch, client_bundle_sha256="A" * 64)
    store_status(GmailConnectStatus(state="connected"))
    before = status_path().read_bytes()

    res = await ops.gmail_connect_initiate({})

    assert res == {"ok": False, "state": "failed", "reason": "executor_unavailable"}
    assert status_path().read_bytes() == before


@pytest.mark.asyncio
async def test_the_confirmed_side_of_the_boundary_still_persists(home, monkeypatch):
    """The other half of Jeff 188618: past an explicit Yes, real work
    happened and the outcome is recorded. Without this, deleting every
    `store_status` would pass the tests above."""
    _configured(monkeypatch)

    async def allow(prompt, *, timeout_s):
        return ConfirmOutcome.CONFIRMED

    async def failing(entrypoint, request, *, flow_timeout_s):
        return ExecutorOutcome(status="failed", reason="exchange_failed")

    monkeypatch.setattr(ops, "request_native_confirm", allow)
    monkeypatch.setattr(ops, "run_gmail_executor", failing)

    store_status(GmailConnectStatus(state="disconnected"))
    res = await ops.gmail_connect_initiate({})

    assert res == {"ok": False, "state": "failed", "reason": "exchange_failed"}
    after = load_status()
    assert (after.state, after.reason) == ("failed", "exchange_failed")


# ── (ok, state, reason) roster drift alarm ────────────────────────────────
#
# Jeff 188669: a CHANGE ALARM, not a runtime source of truth. The Web
# classifier holds a hand-copied mirror of this set in another repo with no
# automatic sync (Boris 188667). A new FAILURE pair is harmless there — it
# falls back to `error`, which is the right display. The one thing that
# silently hurts is a new NEUTRAL/SUCCESS pair: it would render as a red
# error while both repos' tests stay green. Re-auth, left to a later design
# by Jeff 188618, is exactly that kind of change.
#
# Derivation handed over by Boris (shared/gmail-connect-pair-roster,
# derive_reply_pairs.py sha256 30de070e…7a66). Two of his properties are
# load-bearing and kept verbatim in spirit:
#   1. dynamic arguments are EXPANDED through a declared table keyed by the
#      unparsed expression source — never by line number, which silently
#      re-points when code is inserted above (the LingTai #1624 lesson),
#      and never skipped;
#   2. finding no call sites is an error, not a pass.
#
# One deliberate widening (Linus): Boris's version parsed `ops.py` only. A
# `_reply` site added in a SIBLING module of the package was invisible to
# it — measured, not assumed: a mutant `ops_extra.py` emitting the success
# pair `(True, "connected_readonly", "")` left the count at 15 and the
# alarm green, which is precisely the failure mode above. This walks the
# package, the same shape as the `_diagnose` guard, so a new module is
# covered by default instead of being exempt until someone remembers it.

_EXPECTED_REPLY_PAIRS = {
    # ok=True — the three shapes a caller may read as success.
    (True, "connected", ""),
    (True, "disconnected", ""),
    (True, "disconnected", "revoke_unconfirmed"),
    # ok=False but NOT a machine failure: the user declined the dialog.
    (False, "disconnected", "refused"),
    # ok=False, daemon-minted terminal failures of this call.
    (False, "failed", "confirm_timeout"),
    (False, "failed", "confirm_unavailable"),
    (False, "failed", "executor_unavailable"),
    (False, "failed", "protocol"),
    (False, "failed", "refused"),
    # ok=False, minted by the executor WRAPPER itself, not reported by the
    # child. Bob 188676 / Jeff 188677: `_reply` does not clamp these away —
    # they are in `REASONS`, so they reach the caller verbatim and persist.
    # An earlier version of this roster expanded `outcome.reason` to
    # `EXECUTOR_REASONS` alone and silently lost exactly these two.
    (False, "failed", "non_loopback_ready"),
    (False, "failed", "timeout"),
} | {
    # ok=False, reason reported by the child: invoke schema v1 §4's closed
    # set, clamped at the trust boundary in `run_gmail_executor`.
    (False, "failed", reason)
    for reason in (
        "bundle_verify_failed",
        "callback_timeout",
        "exchange_failed",
        "scope_mismatch",
        "keychain_error",
        "internal_error",
    )
}


def _called_name(node):
    """Final callable name of a Call, bare or module-qualified.

    `store_status(...)` and `status_store.store_status(...)` are the same
    producer. Matching only `ast.Name` let a package sibling bypass these
    alarms using ordinary qualified syntax — measured, not assumed: a probe
    module calling both producers that way left every roster test green
    (Jeff 188717). The reply scanner already matched both; the other two
    did not, so the gap was in the shape of the matcher, not in one site.
    """
    import ast

    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _ast_const(node):
    """The literal value of an AST node, or None if it is not a literal."""
    import ast

    return node.value if isinstance(node, ast.Constant) else None


def _confirm_refusal_table(tree):
    """`_CONFIRM_REFUSALS` read as (state, reason) tuples, from source."""
    import ast

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and getattr(node.targets[0], "id", "") == "_CONFIRM_REFUSALS"
        ):
            return {
                ast.unparse(k): tuple(e.value for e in v.elts)
                for k, v in zip(node.value.keys, node.value.values)
            }
    raise AssertionError("_CONFIRM_REFUSALS not found — the table moved or was renamed")


def _executor_failure_reasons(trees) -> set[str]:
    """Every reason a failed ``ExecutorOutcome`` can carry, from source.

    The one non-literal site is the child-reported reason, which
    ``run_gmail_executor`` clamps to ``EXECUTOR_REASONS`` immediately
    above it; that is declared here by expression source. Anything else
    raises rather than being skipped — an alarm must never pass over what
    it could not read.
    """
    import ast

    reasons: set[str] = set()
    sites = 0
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if _called_name(node) != "ExecutorOutcome":
                continue
            keywords = {k.arg: k.value for k in node.keywords}
            if _ast_const(keywords.get("status")) != "failed":
                continue
            sites += 1
            literal = _ast_const(keywords.get("reason"))
            if literal is not None:
                reasons.add(literal)
                continue
            expression = (
                ast.unparse(keywords["reason"]) if "reason" in keywords else None
            )
            assert expression == "reason", (
                f"{path.name}:{node.lineno}: failed ExecutorOutcome has an "
                f"undeclared dynamic reason {expression!r} — expand it beside "
                f"the change that introduced it."
            )
            reasons |= set(EXECUTOR_REASONS)
    assert sites, "no failed ExecutorOutcome sites found — the scan is blind"
    return reasons


def _derive_reply_pairs():
    """Every `(ok, state, reason)` the package can emit, from source."""
    import ast
    import pathlib

    package = pathlib.Path(ops.__file__).parent
    sources = sorted(package.glob("*.py"))
    assert sources, f"no package sources under {package} — the alarm would be blind"

    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in sources}
    ops_tree = trees[pathlib.Path(ops.__file__)]

    # Declared expansions for non-literal arguments, keyed by expression
    # source. Extend this ONLY together with the code introducing the new
    # dynamic — that pairing is the whole point.
    expansions = {
        # ops._confirm_refusal: both values come from the table.
        ("reason", "state"): {
            (False, state, reason)
            for state, reason in _confirm_refusal_table(ops_tree).values()
        },
        # ops.gmail_connect_initiate: executor terminal failure. DERIVED
        # from the producer's own `ExecutorOutcome` sites — NOT assumed to
        # be `EXECUTOR_REASONS`, which was wrong: the wrapper mints
        # `non_loopback_ready`/`timeout`/`protocol`/`executor_unavailable`
        # itself and `_reply` passes them through (Jeff 188677). Deriving
        # it means a new wrapper-minted failure is picked up automatically
        # instead of needing someone to remember this list.
        ("outcome.reason", "failed"): {
            (False, "failed", reason)
            for reason in _executor_failure_reasons(trees)
        },
    }

    pairs, sites = set(), []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if _called_name(node) != "_reply":
                continue
            sites.append(f"{path.name}:{node.lineno}")
            where = sites[-1]
            reason_node = node.args[0] if node.args else None
            state_node = next(
                (k.value for k in node.keywords if k.arg == "state"), None
            )
            ok_node = next((k.value for k in node.keywords if k.arg == "ok"), None)
            ok, reason, state = (
                _ast_const(ok_node),
                _ast_const(reason_node),
                _ast_const(state_node),
            )
            if reason is not None and state is not None and isinstance(ok, bool):
                pairs.add((ok, state, reason))
                continue

            key = (
                ast.unparse(reason_node) if reason_node is not None else None,
                state if state is not None else ast.unparse(state_node),
            )
            assert key in expansions, (
                f"{where}: `_reply` has a dynamic argument this alarm cannot "
                f"expand: {key!r}. Declare its expansion beside the change that "
                f"introduced it — the alarm must never pass over what it could "
                f"not read."
            )
            pairs |= expansions[key]

    assert sites, "no _reply call sites found — the alarm would vacuously pass"
    return pairs


def test_reply_pair_roster_has_not_drifted():
    """The Web classifier's hand-copied table must not silently fall behind.

    Adding a reply shape here turns this red BEFORE a new neutral/success
    pair can reach the other repo's `error` fallback and be shown to a user
    as a red failure. When it goes red, update the expected roster AND the
    Web classifier together — updating only this one restores green while
    leaving the real defect in place.
    """
    assert _derive_reply_pairs() == _EXPECTED_REPLY_PAIRS


# ── load path: the projection value space is its own surface ──────────────
#
# The reply roster is derived from `_reply` call sites, so it describes
# what a COMMAND can answer. The projection has a second producer nobody
# had enumerated: `load_status` feeds disk bytes straight into
# `GmailConnectStatus`, whose per-field clamping fixed an out-of-roster
# reason but kept the state (Boris 188696). Both roster derivations
# reasoned from reply branches, so both missed it — the failure was in the
# choice of enumeration surface, not in the derivation, which is exactly
# why two independent implementations agreeing on 17 could not catch it.

_PRODUCERLESS_PAIRS = [
    # reason retired in a later version (Boris 188696's version-narrowing)
    ("connected", "reauthorized"),
    ("disconnected", "reauthorized"),
    # unknown state
    ("bogus", ""),
    ("BOGUS", "internal_error"),
    # both fields legal, no producer emits the combination (测试姬 188700)
    ("connected", "revoke_unconfirmed"),
    ("disconnected", "refused"),
    ("pending", "revoke_unconfirmed"),
    ("connected", "confirm_timeout"),
]


@pytest.mark.parametrize(("state", "reason"), _PRODUCERLESS_PAIRS)
def test_a_pair_no_producer_can_emit_fails_closed(home, state, reason):
    """Never keep claiming `connected`/`pending` from bytes we cannot place.

    Fails closed to ONE sentinel rather than to a family of new pairs, so
    the projection roster does not grow a member per defect.
    """
    status_path().parent.mkdir(parents=True, exist_ok=True)
    status_path().write_text(
        json.dumps({"state": state, "reason": reason, "updated_at": 1.0}),
        encoding="utf-8",
    )

    loaded = load_status()

    assert (loaded.state, loaded.reason) == UNTRUSTED_STATUS_PAIR
    assert loaded.projection() == {
        "state": "disconnected", "reason": "internal_error",
    }


@pytest.mark.parametrize(("state", "reason"), sorted(PERSISTABLE_PAIRS))
def test_every_persistable_pair_survives_a_round_trip(home, state, reason):
    """The negative control for the test above.

    A fail-closed rule that also swallowed legitimate pairs would pass
    every producerless case while breaking the feature.
    """
    store_status(GmailConnectStatus(state=state, reason=reason))

    loaded = load_status()

    assert (loaded.state, loaded.reason) == (state, reason)


def _derive_persisted_pairs():
    """Every `(state, reason)` a producer can persist, from source."""
    import ast
    import pathlib

    package = pathlib.Path(ops.__file__).parent
    sources = sorted(package.glob("*.py"))
    assert sources, "no package sources — the alarm would be blind"

    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in sources}
    written, sites = set(), []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if _called_name(node) != "GmailConnectStatus":
                continue
            parent_calls = [
                outer
                for outer in ast.walk(tree)
                if _called_name(outer) == "store_status"
                and node in ast.walk(outer)
            ]
            if not parent_calls:
                continue
            sites.append(f"{path.name}:{node.lineno}")
            keywords = {k.arg: k.value for k in node.keywords}
            state = _ast_const(keywords.get("state"))
            assert isinstance(state, str), f"{sites[-1]}: state must be a literal"
            if "reason" not in keywords:
                written.add((state, ""))
                continue
            reason = _ast_const(keywords["reason"])
            if reason is not None:
                written.add((state, reason))
                continue
            expression = ast.unparse(keywords["reason"])
            assert expression == "outcome.reason", (
                f"{sites[-1]}: undeclared dynamic reason {expression!r}"
            )
            # DERIVED, not the `EXECUTOR_FAILURE_REASONS` constant.
            # `PERSISTABLE_PAIRS` is itself built from that constant, so
            # using it here made the assertion `X | C == Y | C` — C cancels
            # and the alarm goes blind to exactly the change it exists to
            # catch. Measured on 914ed30 (Boris 188728): mint a new reason
            # at an `ExecutorOutcome` site and the REPLY alarm goes red
            # while this one stays green, even though the value round-trips
            # `('failed', <new>)` -> `('disconnected', 'internal_error')`.
            written |= {(state, r) for r in _executor_failure_reasons(trees)}

    assert sites, "no store_status(GmailConnectStatus(...)) sites found"
    # The missing-file default is a producer too, just not a written one.
    return written | {("disconnected", "")}


def test_the_persistable_roster_matches_what_producers_actually_write(home):
    """`PERSISTABLE_PAIRS` must track `store_status` call sites.

    The drift alarm for the SECOND surface. Without it, adding a producer
    without extending the roster would make the new state fail closed —
    silently, and in the safe-looking direction.
    """
    assert _derive_persisted_pairs() == set(PERSISTABLE_PAIRS)


def test_a_qualified_producer_in_a_sibling_module_is_still_seen():
    """The alarm must not be dodgeable by ordinary import style.

    `status_store.store_status(status_store.GmailConnectStatus(...))` is
    how a new sibling module would normally be written, and the scanner
    used to miss it entirely — the roster test stayed green with a live
    out-of-roster producer present (Jeff 188717).

    Written into the real package because that is the surface the alarm
    scans; anything less would be testing a copy of the problem.
    """
    import pathlib

    package = pathlib.Path(ops.__file__).parent
    sibling = package / "_roster_control_sibling.py"
    sibling.write_text(
        "from . import status_store\n"
        "\n"
        "\n"
        "def new_persisted_producer():\n"
        "    status_store.store_status(\n"
        '        status_store.GmailConnectStatus(state="failed", reason="confirm_timeout")\n'
        "    )\n",
        encoding="utf-8",
    )
    try:
        derived = _derive_persisted_pairs()
    finally:
        sibling.unlink()

    # `(failed, confirm_timeout)` is a REPLY pair that no producer
    # persists, so its appearance here is unambiguous evidence the
    # sibling was read — and it makes the roster comparison fail.
    assert ("failed", "confirm_timeout") in derived
    assert derived != set(PERSISTABLE_PAIRS)
