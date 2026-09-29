"""The daemon's read side of credential design v2, against a fake server.

The fake seals with the real wire format, so these cells are about the rules:
what is cached, what is dropped, and what a race may not undo.
"""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from puffo_agent.crypto.credential_keys import (
    CredentialKeyError,
    compute_credential_wrap_aad,
    credential_id,
    derive_credential_kem_keypair,
    seal_credential,
    verify_credential_key_cert,
)
from puffo_agent.crypto.encoding import base64url_decode, base64url_encode
from puffo_agent.crypto.http_client import HttpError
from puffo_agent.crypto.primitives import Ed25519KeyPair
from puffo_agent.portal.credentials import AgentCredentials

SLUG = "agt-daemon-0001"
OWNER = "alice"
ROOT = hashlib.sha256(b"agent credentials test root").digest()
TYPE = "CUSTOMIZED"
RT = "PUFFO_GOOGLE_OAUTH_v1"
RT_AT = "PUFFO_GOOGLE_OAUTH_AT_v1"


class FakeServer:
    """What the daemon can see of the server: GET one, GET list, PUT key, refresh."""

    def __init__(self):
        self.recipient = derive_credential_kem_keypair(ROOT, 1).public_key_bytes()
        self.rows = {}  # (type, index) -> dict(id, version, state, value)
        self.gets = 0
        self.put_body = None
        self.hold = None  # an Event that GET one waits on, to open a race
        self.refreshes = []  # (path, body) of every refresh call
        self.refresh_error = None  # an HttpError the next refresh raises
        self.rotate = False

    def row(self, index, value, *, version=1, state="ACTIVATED", type=TYPE, expire_at=None):
        self.rows[type, index] = dict(
            id=credential_id(OWNER, type, index), version=version, state=state, value=value,
            expire_at=expire_at,
        )

    async def put(self, path, body):
        assert path == "/v2/identities/me/credential-key"
        self.put_body = body

    async def post(self, path, body):
        """Share-based refresh, as puffo-server refresh.rs answers it."""
        self.refreshes.append((path, body))
        if self.refresh_error is not None:
            raise self.refresh_error
        _, _, _, rt_type, index, _ = path.split("/")
        rt, at = self.rows[rt_type, int(index)], self.rows[RT_AT, int(index)]
        assert (body["version"], base64url_decode(body["share"])) == (rt["version"], rt["value"])
        at["version"] += 1
        at["value"] = f"access-{at['version']}".encode()
        if self.rotate:
            rt["version"] += 1
            rt["value"] = f"share-{rt['version']}".encode()
        return {"access_token": at["value"].decode(), "expires_at": None, "scope": "gmail.send",
                "at_version": at["version"], "rt_version": rt["version"], "rotated": self.rotate}

    async def get(self, path):
        if path.startswith("/v2/credentials?type=") or path == "/v2/credentials":
            wanted = path.partition("?type=")[2]
            return {"credentials": [
                {"id": r["id"], "type": t, "index": i, "version": r["version"],
                 "state": r["state"], "alias": f"acct-{i}"}
                for (t, i), r in self.rows.items() if not wanted or t == wanted
            ]}
        _, _, _, credential_type, index = path.split("/")
        index = int(index)
        self.gets += 1
        # Read the row as the server would at request time, then let a test
        # change the world before the response arrives.
        row = self.rows.get((credential_type, index))
        if self.hold is not None:
            await self.hold.wait()
        if row is None:
            raise HttpError(404, "{}")
        if row["state"] != "ACTIVATED":
            raise HttpError(410, "{}")
        aad = compute_credential_wrap_aad(
            credential_id=row["id"], version=row["version"], recipient_slug=SLUG,
            credential_type=credential_type, key_version=1,
        )
        return {"id": row["id"], "type": credential_type, "index": index,
                "version": row["version"], "state": row["state"], "expire_at": row["expire_at"],
                "blob": base64url_encode(seal_credential(self.recipient, aad, row["value"]))}


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def agent(server):
    return AgentCredentials(server, SLUG, OWNER, ROOT)


@pytest.mark.asyncio
async def test_register_publishes_a_cert_the_agents_own_root_signed(server, agent):
    await agent.register()
    root_pk = Ed25519KeyPair.from_secret_bytes(ROOT).public_key_bytes()
    assert verify_credential_key_cert(
        server.put_body["cert_json"], root_public_key=root_pk, slug=SLUG, key_version=1
    ) == server.recipient
    assert set(server.put_body) == {"cert_json"}


@pytest.mark.asyncio
async def test_a_fetched_value_opens_and_is_served_from_memory_after(server, agent):
    server.row(0, b"secret-0")
    assert (await agent.get(TYPE, 0)).value == b"secret-0"
    assert (await agent.get(TYPE, 0)).value == b"secret-0"
    assert server.gets == 1


@pytest.mark.asyncio
async def test_a_blob_sealed_for_another_version_is_refused_and_not_cached(server, agent):
    """Wrap sealed for v1, response claiming v2: the AAD disagrees."""
    server.row(0, b"secret-0", version=1)
    real_get = server.get

    async def lying_get(path):
        data = await real_get(path)
        if path != "/v2/credentials":
            data["version"] = 2
        return data

    server.get = lying_get
    with pytest.raises(CredentialKeyError):
        await agent.get(TYPE, 0)
    server.get = real_get
    await agent.get(TYPE, 0)
    assert server.gets == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "INACTIVATED"])
async def test_a_credential_not_held_or_switched_off_is_none_not_an_error(server, agent, state):
    """404 (not held) and 410 (INACTIVATED) are answers, not failures."""
    if state is not None:
        server.row(0, b"secret-0", state=state)
    assert await agent.get(TYPE, 0) is None


@pytest.mark.asyncio
async def test_reconcile_drops_the_absent_the_stale_and_the_inactivated(server, agent):
    for i in range(4):
        server.row(i, f"secret-{i}".encode())
        await agent.get(TYPE, i)
    del server.rows[TYPE, 0]                      # revoked
    server.rows[TYPE, 1]["version"] = 2           # value updated
    server.rows[TYPE, 2]["state"] = "INACTIVATED" # kill switch
    await agent.reconcile()
    gets = server.gets
    assert (await agent.get(TYPE, 3)).value == b"secret-3"
    assert server.gets == gets                    # 3 was kept, served from memory
    assert await agent.get(TYPE, 0) is None
    assert await agent.get(TYPE, 2) is None


@pytest.mark.asyncio
async def test_offline_across_delete_and_recreate_the_old_secret_is_not_served(server, agent):
    """Boris 226189 / 測試姬 226205: a recreated credential gets a new id."""
    server.row(0, b"old-secret")
    await agent.get(TYPE, 0)
    del server.rows[TYPE, 0]
    server.row(1, b"new-secret")
    await agent.reconcile()
    assert await agent.get(TYPE, 0) is None
    assert (await agent.get(TYPE, 1)).value == b"new-secret"


@pytest.mark.asyncio
async def test_a_failed_list_call_deletes_nothing(server, agent):
    server.row(0, b"secret-0")
    await agent.get(TYPE, 0)

    async def down(path):
        raise HttpError(503, "{}")

    real_get, server.get = server.get, down
    with pytest.raises(HttpError):
        await agent.reconcile()
    server.get = real_get
    gets = server.gets
    assert (await agent.get(TYPE, 0)).value == b"secret-0"
    assert server.gets == gets


@pytest.mark.asyncio
async def test_a_fetch_in_flight_across_a_revocation_is_not_cached(server, agent):
    """Jeff 226108: revoking does not bump the version, so only the
    generation fence can tell the response that crossed it is stale."""
    server.row(0, b"secret-0")
    server.hold = asyncio.Event()
    fetch = asyncio.create_task(agent.get(TYPE, 0))
    await asyncio.sleep(0)                        # the GET has read the row
    del server.rows[TYPE, 0]
    await agent.reconcile()
    server.hold.set()
    assert await fetch is None
    server.hold = None
    assert await agent.get(TYPE, 0) is None       # and nothing was left behind


@pytest.mark.asyncio
async def test_a_genuine_row_for_another_credential_is_not_served_as_the_one_asked_for(
    server, agent
):
    """Boris 226286: B's row opens under its own AAD; only the id says it is not A."""
    server.row(0, b"secret-A")
    server.row(1, b"secret-B")
    real_get = server.get

    async def relabelling_get(path):
        return await real_get(path.replace("/0", "/1") if path.endswith("/0") else path)

    server.get = relabelling_get
    with pytest.raises(CredentialKeyError, match="different credential"):
        await agent.get(TYPE, 0)
    server.get = real_get
    assert (await agent.get(TYPE, 0)).value == b"secret-A"


@pytest.mark.asyncio
async def test_a_held_credential_does_not_print_its_value(server, agent):
    server.row(0, b"do-not-log-me")
    held = await agent.get(TYPE, 0)
    assert "do-not-log-me" not in repr(held)
    assert "do-not-log-me" not in repr({held.id: held})


def _oauth_pair(server, index=0):
    server.row(index, b"share-1", type=RT)
    server.row(index, b"access-1", type=RT_AT)


@pytest.mark.asyncio
async def test_refresh_spends_the_held_share_and_drops_only_the_access_token(server, agent):
    _oauth_pair(server)
    assert (await agent.get(RT_AT, 0)).value == b"access-1"
    token = await agent.refresh(RT, 0)
    assert (token.type, token.version, token.value) == (RT_AT, 2, b"access-2")
    gets = server.gets
    assert (await agent.get(RT_AT, 0)).value == b"access-2"   # re-fetched, not the old one
    await agent.get(RT, 0)
    assert server.gets == gets + 1                            # the share was kept


@pytest.mark.asyncio
async def test_after_a_rotation_the_next_refresh_uses_the_new_share(server, agent):
    """A daemon that kept the rotated-away share fails the second refresh."""
    _oauth_pair(server)
    server.rotate = True
    await agent.refresh(RT, 0)
    assert (await agent.refresh(RT, 0)).value == b"access-3"
    assert base64url_decode(server.refreshes[1][1]["share"]) == b"share-2"


@pytest.mark.asyncio
async def test_a_stale_share_refusal_drops_it_so_asking_again_can_succeed(server, agent):
    """Another holder rotated first: this share is dead, and the 409 says so."""
    _oauth_pair(server)
    await agent.get(RT, 0)
    server.rows[RT, 0].update(version=2, value=b"share-2")
    server.refresh_error = HttpError(409, "{}")
    with pytest.raises(HttpError):
        await agent.refresh(RT, 0)
    server.refresh_error = None
    assert (await agent.refresh(RT, 0)).value == b"access-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 410])
async def test_a_refresh_refused_as_not_held_drops_the_pair(server, agent, status):
    _oauth_pair(server)
    await agent.get(RT_AT, 0)
    server.refresh_error = HttpError(status, "{}")
    assert await agent.refresh(RT, 0) is None
    gets = server.gets
    await agent.get(RT, 0)
    await agent.get(RT_AT, 0)
    assert server.gets == gets + 2


def _run_with(server, *, owner=OWNER, keyless=False):
    from types import SimpleNamespace as NS

    from puffo_agent.crypto.keystore import encode_secret
    from puffo_agent.portal.worker_run import StandardWorkerRun

    server.keyless = keyless
    identity = NS(root_secret_key=encode_secret(ROOT))
    client = NS(http=server, slug=SLUG, keystore=NS(load_identity=lambda slug: identity))
    worker = NS(agent_cfg=NS(puffo_core=NS(operator_slug=owner)))
    return StandardWorkerRun(worker), NS(client=client, paths=NS(agent_id="agent-1"))


@pytest.mark.asyncio
async def test_the_worker_registers_the_agents_own_key_at_start(server):
    from puffo_agent.portal.credentials import keep_registering

    run, context = _run_with(server)
    assert await keep_registering(run._build_credentials(context))
    root_pk = Ed25519KeyPair.from_secret_bytes(ROOT).public_key_bytes()
    assert verify_credential_key_cert(
        server.put_body["cert_json"], root_public_key=root_pk, slug=SLUG, key_version=1
    ) == server.recipient


@pytest.mark.parametrize("owner, keyless", [("", False), (OWNER, True)])
def test_an_agent_without_an_owner_or_keys_holds_no_credentials(server, owner, keyless):
    run, context = _run_with(server, owner=owner, keyless=keyless)
    assert run._build_credentials(context) is None


@pytest.mark.asyncio
async def test_registration_retries_failures_and_waits_out_a_server_without_v2(server, agent):
    """Boris 227783: 404 is polled, so a running agent picks v2 up on deploy."""
    from puffo_agent.portal.credentials import keep_registering

    answers = [HttpError(404, "{}"), HttpError(404, "{}"), HttpError(503, "{}"),
               OSError("reset"), None]
    naps = []

    async def put(path, body):
        answer = answers.pop(0)
        if answer is not None:
            raise answer

    async def sleep(seconds):
        naps.append(seconds)

    server.put = put
    assert await keep_registering(agent, sleep=sleep)
    assert (answers, naps) == ([], [3600.0, 3600.0, 5.0, 20.0])


@pytest.mark.asyncio
async def test_a_rejected_cert_is_not_retried(server, agent):
    from puffo_agent.portal.credentials import keep_registering

    naps = []

    async def rejected(path, body):
        naps.append("put")
        raise HttpError(400, "{}")

    async def sleep(seconds):
        raise AssertionError("a rejected cert was retried")

    server.put = rejected
    assert not await keep_registering(agent, sleep=sleep)
    assert naps == ["put"]


@pytest.mark.asyncio
async def test_a_value_already_expired_on_arrival_is_not_handed_out(server, agent):
    """Codex review on #426: the expiry check ran on cache hits only."""
    server.row(0, b"access-old", type=RT_AT, expire_at="2000-01-01T00:00:00+00:00")
    assert await agent.get(RT_AT, 0) is None


@pytest.mark.asyncio
async def test_an_older_version_landing_late_does_not_replace_a_newer_one(server, agent):
    """Codex review on #426: nothing was invalidated, so the fence cannot
    tell the two responses apart; the version has to."""
    server.row(0, b"secret-v1")
    gate = server.hold = asyncio.Event()
    old = asyncio.create_task(agent.get(TYPE, 0))
    await asyncio.sleep(0)                        # the old GET has read v1
    server.hold = None
    server.row(0, b"secret-v2", version=2)       # a new row; the old GET keeps v1
    assert (await agent.get(TYPE, 0)).value == b"secret-v2"
    gate.set()                                    # now v1 lands
    assert (await old).value == b"secret-v2"
    assert (await agent.get(TYPE, 0)).value == b"secret-v2"


@pytest.mark.asyncio
async def test_a_credential_that_expired_while_cached_is_not_handed_back_by_a_late_older_get(
    server, agent
):
    """Expired cache plus a late older version: keeping the newer one must
    not hand back an expired one."""
    soon = (datetime.now(timezone.utc) + timedelta(milliseconds=50)).isoformat()
    server.row(0, b"secret-v5", version=5, expire_at=soon)
    assert (await agent.get(TYPE, 0)).version == 5
    await asyncio.sleep(0.06)
    server.row(0, b"secret-v4", version=4)
    assert await agent.get(TYPE, 0) is None


@pytest.mark.asyncio
async def test_a_transient_failure_is_raised_rather_than_read_as_a_revoke(server, agent):
    """A 5xx says nothing about who holds what, unlike 403/404/410."""
    server.row(0, b"secret-0")
    await agent.get(TYPE, 0)

    async def down(path):
        raise HttpError(503, "{}")

    real_get, server.get = server.get, down
    agent._invalidate(lambda c: True)             # force the next call to fetch
    with pytest.raises(HttpError):
        await agent.get(TYPE, 0)
    server.get = real_get
    assert (await agent.get(TYPE, 0)).value == b"secret-0"


@pytest.mark.asyncio
async def test_refreshing_a_share_this_agent_does_not_hold_is_none_not_an_error(server, agent):
    server.row(0, b"access-1", type=RT_AT)        # the AT only; no share
    assert await agent.refresh(RT, 0) is None
    assert server.refreshes == []


@pytest.mark.asyncio
async def test_a_refresh_that_failed_for_an_unrelated_reason_keeps_the_share(server, agent):
    """Only 409 means the share is dead; a 500 must leave it cached."""
    _oauth_pair(server)
    await agent.get(RT, 0)
    server.refresh_error = HttpError(500, "{}")
    with pytest.raises(HttpError):
        await agent.refresh(RT, 0)
    server.refresh_error = None
    gets = server.gets
    assert (await agent.refresh(RT, 0)).value == b"access-2"
    assert server.gets == gets                    # the share was served from memory


def test_only_an_oauth_refresh_token_type_can_be_refreshed(server, agent):
    from puffo_agent.portal.credentials import _access_token_type

    assert _access_token_type(RT) == RT_AT
    for rejected in (TYPE, RT_AT):
        with pytest.raises(ValueError, match="not a refreshable credential type"):
            _access_token_type(rejected)


def test_an_owner_is_required_to_derive_the_ids_a_response_is_checked_against(server):
    with pytest.raises(ValueError, match="owner_slug is required"):
        AgentCredentials(server, SLUG, "", ROOT)


def _services_run(server, *, owner=OWNER):
    """``_start_services`` with every other service stubbed: each needs a live runtime."""
    from types import SimpleNamespace as NS

    run, context = _run_with(server, owner=owner)

    async def forever():
        await asyncio.Event().wait()

    reporter = NS(run_heartbeat_loop=forever, stop=lambda: None)
    global_runtime = NS(run=forever, stop=lambda: None)
    run._build_runtime_event_uploader = lambda ctx: None
    run._build_reporter = lambda client: reporter
    run._build_global_runtime = lambda ctx, **kw: global_runtime
    run._prepare_reminder_sync = lambda ctx, runtime: _none()
    run._heartbeat = lambda agent_id: forever()
    run._upload_runtime_events = lambda uploader: forever()
    run.worker._refresh_watcher_loop = lambda flags, apply: forever()
    run.worker.runtime = NS(status="running", activity="x", save=lambda agent_id: None)
    run.worker._status_reporter = reporter
    context.paths.refresh_flags = None
    context.runtime_event_outbox = None
    return run, context


async def _none():
    return None


@pytest.mark.asyncio
async def test_worker_startup_publishes_the_agents_key(server):
    run, context = _services_run(server)
    services = await run._start_services(context)
    assert run.worker._credentials is not None
    assert await services.credential_key_task is True
    assert server.put_body is not None
    await run._cleanup(context, services)


@pytest.mark.asyncio
async def test_shutdown_does_not_wait_for_a_registration_that_is_still_retrying(server):
    """``keep_registering`` retries for as long as the agent runs, so shutdown
    must cancel it rather than await it."""
    run, context = _services_run(server)

    async def never(path, body):
        await asyncio.Event().wait()

    server.put = never
    services = await run._start_services(context)
    await asyncio.sleep(0)
    assert not services.credential_key_task.done()
    await asyncio.wait_for(run._cleanup(context, services), timeout=5)
    assert services.credential_key_task.cancelled()


@pytest.mark.asyncio
async def test_an_agent_that_holds_no_credentials_starts_and_stops_without_the_task(server):
    run, context = _services_run(server, owner="")
    services = await run._start_services(context)
    assert (run.worker._credentials, services.credential_key_task) == (None, None)
    await run._cleanup(context, services)
    assert server.put_body is None


@pytest.mark.asyncio
async def test_an_unreadable_identity_leaves_the_agent_without_credentials(server):
    run, context = _run_with(server)

    def unreadable(slug):
        raise OSError("keystore is unreadable")

    context.client.keystore.load_identity = unreadable
    assert run._build_credentials(context) is None


@pytest.mark.asyncio
async def test_held_lists_only_usable_credentials_of_the_type_asked(server, agent):
    server.row(2, b"s", type=RT)
    server.row(0, b"s", type=RT)
    server.row(1, b"s", type=RT, state="INACTIVATED")
    server.row(5, b"s")                           # another type
    assert await agent.held(RT) == [(0, "acct-0"), (2, "acct-2")]
