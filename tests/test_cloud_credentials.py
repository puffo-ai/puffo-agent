"""``CloudAgentCredentials``: the server hands over the KEY, the unwrap stays here.

The server is faked at the HTTP boundary with a REAL seal, so an open proves the
AAD and the key agree. The signed ``get``/``post`` raise: a keyless agent that
reached for them would be trying to sign with a key it does not hold."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from puffo_agent.crypto.credential_keys import (
    CredentialKeyError,
    compute_credential_wrap_aad,
    credential_id,
    seal_credential,
)
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.http_client import HttpError
from puffo_agent.crypto.primitives import KemKeyPair
from puffo_agent.portal.cloud_credentials import CloudAgentCredentials

SLUG, OWNER, TYPE = "bot-cloud", "op-test", "PUFFO_CLAUDE_CODE_TOKEN_v1"
SECRET_V1 = bytes(range(32))
SECRET_V2 = bytes(range(32, 64))


class FakeKeylessServer:
    def __init__(self):
        self.key_version = 1
        self.rows = {}
        self.kem_posts = 0
        self.gets = []
        self.hold = None  # an Event that GET one waits on, to open a race

    def row(self, index, value, *, version=1, state="ACTIVATED", sealed_under=None, expire_at=None):
        self.rows[TYPE, index] = dict(value=value, version=version, state=state,
                                      sealed_under=sealed_under or self.key_version, expire_at=expire_at)

    def _secret(self, version):
        return {1: SECRET_V1, 2: SECRET_V2}[version]

    # the keyless surface
    async def post_unsigned(self, path, body=None):
        assert path == "/v2/cloud-agents/credential-kem-secret" and body == {}
        self.kem_posts += 1
        return {"slug": SLUG, "key_version": self.key_version,
                "kem_secret_key": base64url_encode(self._secret(self.key_version))}

    async def get_unsigned(self, path):
        self.gets.append(path)
        if path.startswith("/v2/cloud-agents/credentials?type="):
            wanted = path.partition("?type=")[2]
            return {"credentials": [
                {"id": credential_id(OWNER, t, i), "type": t, "index": i, "version": r["version"],
                 "state": r["state"], "alias": f"acct-{i}"}
                for (t, i), r in self.rows.items() if t == wanted
            ] + [{"id": "x", "type": "OTHER_v1", "index": 0, "version": 1, "state": "ACTIVATED"}]}
        _, _, _, _, ctype, index = path.split("/")
        # Read the row as the server would at request time, then let a test
        # change the world before the response arrives.
        row = self.rows.get((ctype, int(index)))
        if self.hold is not None:
            await self.hold.wait()
        if row is None:
            raise HttpError(404, "{}")
        cid = credential_id(OWNER, ctype, int(index))
        aad = compute_credential_wrap_aad(credential_id=cid, version=row["version"], recipient_slug=SLUG,
                                          credential_type=ctype, key_version=row["sealed_under"])
        pub = KemKeyPair.from_secret_bytes(self._secret(row["sealed_under"])).public_key_bytes()
        return {"id": cid, "type": ctype, "index": int(index), "version": row["version"],
                "expire_at": row["expire_at"], "blob": base64url_encode(seal_credential(pub, aad, row["value"]))}

    # the signed surface — forbidden for a keyless agent
    async def get(self, path):
        raise AssertionError(f"signed GET used on a keyless agent: {path}")

    async def post(self, path, body=None):
        raise AssertionError(f"signed POST used on a keyless agent: {path}")


@pytest.mark.asyncio
async def test_opens_a_real_wrap_with_the_key_the_server_hands_over():
    srv = FakeKeylessServer(); srv.row(0, b"plan-token")
    creds = CloudAgentCredentials(srv, SLUG, OWNER)
    held = await creds.get(TYPE, 0)
    assert held.value == b"plan-token" and held.type == TYPE and held.index == 0
    assert "plan-token" not in repr(held)
    assert srv.kem_posts == 1


@pytest.mark.asyncio
async def test_the_kem_secret_is_fetched_once_and_cached():
    srv = FakeKeylessServer(); srv.row(0, b"a"); srv.row(1, b"b")
    creds = CloudAgentCredentials(srv, SLUG, OWNER)
    assert (await creds.get(TYPE, 0)).value == b"a"
    assert (await creds.get(TYPE, 1)).value == b"b"
    assert (await creds.get(TYPE, 0)).value == b"a"  # from memory
    assert srv.kem_posts == 1
    assert len([g for g in srv.gets if "?type=" not in g]) == 2


@pytest.mark.asyncio
async def test_held_lists_only_activated_rows_of_that_type():
    srv = FakeKeylessServer(); srv.row(1, b"x"); srv.row(0, b"y"); srv.row(2, b"z", state="INACTIVATED")
    creds = CloudAgentCredentials(srv, SLUG, OWNER)
    assert await creds.held(TYPE) == [(0, "acct-0"), (1, "acct-1")]


@pytest.mark.asyncio
async def test_not_held_is_none_not_an_error():
    creds = CloudAgentCredentials(FakeKeylessServer(), SLUG, OWNER)
    assert await creds.get(TYPE, 0) is None


@pytest.mark.asyncio
async def test_a_rotated_key_is_refetched_once_and_the_wrap_opens():
    """The one legitimate reason a genuine wrap refuses: our cached key is at
    an older version than the one the wrap was sealed under."""
    srv = FakeKeylessServer(); srv.row(0, b"old")
    creds = CloudAgentCredentials(srv, SLUG, OWNER)
    assert (await creds.get(TYPE, 0)).value == b"old"
    srv.key_version = 2
    srv.row(0, b"new", version=2, sealed_under=2)
    # still served from memory until told otherwise — the handler calls forget()
    assert (await creds.get(TYPE, 0)).value == b"old"
    creds.forget(TYPE, 0)
    assert (await creds.get(TYPE, 0)).value == b"new"
    assert srv.kem_posts == 2


@pytest.mark.asyncio
async def test_a_wrap_for_a_different_credential_is_refused():
    srv = FakeKeylessServer(); srv.row(0, b"a")
    creds = CloudAgentCredentials(srv, SLUG, OWNER)
    real = srv.get_unsigned

    async def swapped(path):
        data = await real(path)
        if isinstance(data, dict) and "blob" in data:
            data["index"] = 9  # B's row answering for A
        return data

    srv.get_unsigned = swapped
    with pytest.raises(CredentialKeyError):
        await creds.get(TYPE, 0)


@pytest.mark.asyncio
async def test_a_kem_answer_with_no_usable_key_is_refused():
    srv = FakeKeylessServer(); srv.row(0, b"a")

    async def bad(path, body=None):
        return {"slug": SLUG}

    srv.post_unsigned = bad
    with pytest.raises(CredentialKeyError):
        await CloudAgentCredentials(srv, SLUG, OWNER).get(TYPE, 0)


@pytest.mark.asyncio
async def test_an_expired_credential_is_none():
    srv = FakeKeylessServer()
    srv.row(0, b"a", expire_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    assert await CloudAgentCredentials(srv, SLUG, OWNER).get(TYPE, 0) is None


@pytest.mark.asyncio
async def test_an_older_version_landing_late_does_not_replace_a_newer_one():
    """Ported from the native reader: nothing was invalidated, so the fence
    cannot tell the two responses apart; the version has to."""
    import asyncio

    srv = FakeKeylessServer(); srv.row(0, b"secret-v1")
    creds = CloudAgentCredentials(srv, SLUG, OWNER)
    gate = srv.hold = asyncio.Event()
    old = asyncio.create_task(creds.get(TYPE, 0))
    await asyncio.sleep(0)                        # the old GET has read v1
    srv.hold = None
    srv.row(0, b"secret-v2", version=2)           # a new row; the old GET keeps v1
    assert (await creds.get(TYPE, 0)).value == b"secret-v2"
    gate.set()                                    # now v1 lands
    assert (await old).value == b"secret-v2"      # the floor hands back the newer
    assert (await creds.get(TYPE, 0)).value == b"secret-v2"


@pytest.mark.asyncio
async def test_a_fetch_racing_a_forget_does_not_land():
    """The generation fence: forget() during an in-flight GET means the value
    that GET returns is one we have been told to stop trusting."""
    import asyncio

    srv = FakeKeylessServer(); srv.row(0, b"stale")
    creds = CloudAgentCredentials(srv, SLUG, OWNER)
    gate = srv.hold = asyncio.Event()
    inflight = asyncio.create_task(creds.get(TYPE, 0))
    await asyncio.sleep(0)
    creds.forget(TYPE, 0)                         # fence goes up with nothing cached
    gate.set()
    assert await inflight is None
    assert creds._find(TYPE, 0) is None


def test_owner_is_required():
    with pytest.raises(ValueError):
        CloudAgentCredentials(FakeKeylessServer(), SLUG, "")
