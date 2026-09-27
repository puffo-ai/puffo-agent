"""The daemon's read side of credential design v2, against a fake server.

The fake seals blobs with the same wire format the Rust server uses (pinned by
credential_vectors.json), so these cells are about the daemon's rules, not
the bytes: what is cached, what is dropped, and what a race may not undo.
"""

import asyncio
import hashlib
import uuid

import pytest

from puffo_agent.crypto.credential_keys import (
    CredentialKeyError,
    compute_credential_wrap_aad,
    derive_credential_kem_keypair,
    seal_credential,
    verify_credential_key_cert,
)
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.http_client import HttpError
from puffo_agent.crypto.primitives import Ed25519KeyPair
from puffo_agent.portal.credentials import AgentCredentials

SLUG = "agt-daemon-0001"
ROOT = hashlib.sha256(b"agent credentials test root").digest()
TYPE = "CUSTOMIZED"


def _id(index: int, generation: int = 0) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cred/{index}/{generation}"))


class FakeServer:
    """What the daemon can see of the server: GET one, GET list, PUT key."""

    def __init__(self):
        self.recipient = derive_credential_kem_keypair(ROOT, 1).public_key_bytes()
        self.rows = {}  # index -> dict(id, version, state, value)
        self.gets = 0
        self.put_body = None
        self.hold = None  # an Event that GET one waits on, to open a race

    def row(self, index, value, *, version=1, state="ACTIVATED", generation=0):
        self.rows[index] = dict(
            id=_id(index, generation), version=version, state=state, value=value
        )

    async def put(self, path, body):
        assert path == "/v2/identities/me/credential-key"
        self.put_body = body

    async def get(self, path):
        if path == "/v2/credentials":
            return {"credentials": [
                {"id": r["id"], "type": TYPE, "index": i, "version": r["version"],
                 "state": r["state"]}
                for i, r in self.rows.items()
            ]}
        index = int(path.rsplit("/", 1)[1])
        self.gets += 1
        # Read the row as the server would at request time, then let a test
        # change the world before the response arrives.
        row = self.rows.get(index)
        if self.hold is not None:
            await self.hold.wait()
        if row is None:
            raise HttpError(404, "{}")
        if row["state"] != "ACTIVATED":
            raise HttpError(410, "{}")
        aad = compute_credential_wrap_aad(
            credential_id=row["id"], version=row["version"], recipient_slug=SLUG,
            credential_type=TYPE, key_version=1,
        )
        return {"id": row["id"], "type": TYPE, "index": index, "version": row["version"],
                "state": row["state"], "expire_at": None,
                "blob": base64url_encode(seal_credential(self.recipient, aad, row["value"]))}


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def agent(server):
    return AgentCredentials(server, SLUG, ROOT)


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
    """The server says v2 but the wrap was sealed for v1: the AAD disagrees,
    so the fetch fails rather than handing back a value of unknown standing."""
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
    del server.rows[0]                            # revoked
    server.rows[1]["version"] = 2                 # value updated
    server.rows[2]["state"] = "INACTIVATED"       # kill switch
    await agent.reconcile()
    gets = server.gets
    assert (await agent.get(TYPE, 3)).value == b"secret-3"
    assert server.gets == gets                    # 3 was kept, served from memory
    assert await agent.get(TYPE, 0) is None
    assert await agent.get(TYPE, 2) is None


@pytest.mark.asyncio
async def test_offline_across_delete_and_recreate_the_old_secret_is_not_served(server, agent):
    """Boris 226189 / 測試姬 226205: with the index never reused, a recreated
    credential has a new id, and the old one is simply absent from the list."""
    server.row(0, b"old-secret")
    await agent.get(TYPE, 0)
    del server.rows[0]
    server.row(1, b"new-secret", generation=1)
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
    """Jeff 226108: GET(v1) leaves, the credential is revoked and reconcile
    sees it gone, then the old response lands. Revoking does not bump the
    version, so only the generation fence can tell this response is stale."""
    server.row(0, b"secret-0")
    server.hold = asyncio.Event()
    fetch = asyncio.create_task(agent.get(TYPE, 0))
    await asyncio.sleep(0)                        # the GET has read the row
    del server.rows[0]
    await agent.reconcile()
    server.hold.set()
    assert await fetch is None
    server.hold = None
    assert await agent.get(TYPE, 0) is None       # and nothing was left behind
