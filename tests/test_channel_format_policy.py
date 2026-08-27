"""channels.is_encrypted drives the outbound channel format."""
from __future__ import annotations

import json

import pytest

from puffo_agent.agent import disk_cache
from puffo_agent.agent.channel_format import is_channel_format_mismatch
from puffo_agent.agent.send_coordinator import (
    CHANNEL_SEND_PATH,
    SemanticSendRequest,
    SendCoordinator,
)
from puffo_agent.crypto.http_client import HttpError
from puffo_agent.mcp.data_client import DataNotFound

from .test_puffo_core_tools import _setup
from .test_send_coordinator import Freshness

MISMATCH_BODY = json.dumps(
    {"error": "CHANNEL_FORMAT_MISMATCH", "message": "channel is plaintext"}
)


def test_is_channel_format_mismatch_matches_only_the_contract():
    assert is_channel_format_mismatch(HttpError(400, MISMATCH_BODY))
    assert not is_channel_format_mismatch(HttpError(400, '{"error": "OTHER"}'))
    assert not is_channel_format_mismatch(HttpError(409, MISMATCH_BODY))
    assert not is_channel_format_mismatch(HttpError(400, "not json"))
    assert not is_channel_format_mismatch(RuntimeError("nope"))


def test_disk_cache_round_trips_and_preserves_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "_cache_root", lambda: tmp_path)
    disk_cache.persist_channel("ch_1", "general", "sp_1", False)
    assert disk_cache.load_channel("ch_1")["is_encrypted"] is False
    # A policy-less rewrite preserves the persisted value.
    disk_cache.persist_channel("ch_1", "general-renamed", "sp_1")
    assert disk_cache.load_channel("ch_1")["is_encrypted"] is False
    # Legacy rows without the field fail safe (no key present).
    disk_cache.persist_channel("ch_2", "general", "sp_1")
    assert "is_encrypted" not in disk_cache.load_channel("ch_2")


class _Policy:
    def __init__(self, initial: bool, refreshed: bool | None = None):
        self.value = initial
        self.refreshed = refreshed
        self.ensures: list[tuple[str, str]] = []
        self.refreshes: list[str] = []

    async def ensure_channel_policy(self, channel_id, space_id=""):
        self.ensures.append((channel_id, space_id))
        return self.value

    async def refresh_channel_policy(self, channel_id):
        self.refreshes.append(channel_id)
        if self.refreshed is not None:
            self.value = self.refreshed
        return self.value


async def _policy_fixture(policy):
    cfg, http, _store = _setup()

    class Data:
        async def lookup_channel_space(self, channel_id):
            return {"ch_a": "sp_1"}.get(channel_id)

        async def get_message_by_envelope(self, _envelope_id):
            raise DataNotFound("not found")

    from puffo_agent.crypto.encoding import base64url_encode
    from puffo_agent.crypto.primitives import KemKeyPair

    device = KemKeyPair.generate()
    http.responses["/spaces/sp_1/channels/ch_a/members"] = {
        "members": [{"slug": "alice-1"}],
    }
    http.responses["/certs/sync?slugs=alice-1"] = {
        "entries": [{
            "seq": 1,
            "kind": "device_cert",
            "cert": {
                "device_id": "dev_a",
                "kem_public_key": base64url_encode(device.public_key_bytes()),
            },
        }],
        "has_more": False,
    }
    freshness = Freshness(0, None)
    coordinator = SendCoordinator(
        slug=cfg.slug,
        keystore=cfg.keystore,
        http_client=http,
        data_client=Data(),
        baseline_source=freshness,
        active_turn_source=freshness,
        channel_policy_source=policy,
    )
    return coordinator, http


def _channel_posts(http):
    return [c for c in http.calls if c[0] == "POST" and c[1] == CHANNEL_SEND_PATH]


@pytest.mark.asyncio
async def test_encrypted_channel_sends_a_sealed_envelope():
    coordinator, http = await _policy_fixture(_Policy(True))
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="hello")
    )
    assert result["state"] == "sent"
    (post,) = _channel_posts(http)
    envelope = post[2]["envelope"]
    assert envelope["type"] == "message_envelope"
    assert envelope["recipients"]


@pytest.mark.asyncio
async def test_plaintext_channel_sends_a_plaintext_envelope():
    coordinator, http = await _policy_fixture(_Policy(False))
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="hello")
    )
    assert result["state"] == "sent"
    (post,) = _channel_posts(http)
    envelope = post[2]["envelope"]
    assert envelope["type"] == "plaintext_message_envelope"
    assert "signed_payload" in envelope
    # No device or member fetch on the plaintext path.
    assert not any("/certs/sync" in c[1] for c in http.calls if c[0] == "GET")
    assert not any("/members" in c[1] for c in http.calls if c[0] == "GET")


@pytest.mark.asyncio
async def test_plaintext_channel_send_survives_an_empty_member_list():
    coordinator, http = await _policy_fixture(_Policy(False))
    http.responses["/spaces/sp_1/channels/ch_a/members"] = {"members": []}
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="hello")
    )
    assert result["state"] == "sent"


@pytest.mark.asyncio
async def test_encrypted_channel_send_still_requires_members():
    coordinator, http = await _policy_fixture(_Policy(True))
    http.responses["/spaces/sp_1/channels/ch_a/members"] = {"members": []}
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="hello")
    )
    assert result["state"] == "failed"
    assert "no resolvable members" in result["error"]


@pytest.mark.asyncio
async def test_format_mismatch_refreshes_and_resends_in_the_other_format():
    policy = _Policy(False, refreshed=True)
    coordinator, http = await _policy_fixture(policy)

    original_post = http.post
    state = {"failed": False}

    async def post(path, body=None):
        if path == CHANNEL_SEND_PATH and not state["failed"]:
            state["failed"] = True
            http.calls.append(("POST", path, body))
            raise HttpError(400, MISMATCH_BODY)
        return await original_post(path, body)

    http.post = post
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="hello")
    )
    assert result["state"] == "sent"
    assert policy.refreshes == ["ch_a"]
    first, second = _channel_posts(http)
    assert first[2]["envelope"]["type"] == "plaintext_message_envelope"
    assert second[2]["envelope"]["type"] == "message_envelope"


@pytest.mark.asyncio
async def test_format_mismatch_retry_uploads_attachments_once(tmp_path):
    policy = _Policy(False, refreshed=True)
    coordinator, http = await _policy_fixture(policy)
    coordinator.workspace = str(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"hello file")

    original_post = http.post
    state = {"failed": False}

    async def post(path, body=None):
        if path == CHANNEL_SEND_PATH and not state["failed"]:
            state["failed"] = True
            http.calls.append(("POST", path, body))
            raise HttpError(400, MISMATCH_BODY)
        return await original_post(path, body)

    http.post = post
    result = await coordinator.send(
        SemanticSendRequest(
            destination="ch_a",
            caption="see file",
            attachment_paths=("a.txt",),
        )
    )
    assert result["state"] == "sent"
    uploads = [
        c for c in http.calls if c[0] == "POST_BYTES" and c[1] == "/blobs/upload"
    ]
    assert len(uploads) == 1
    # Both attempts reference the same uploaded blob.
    first, second = _channel_posts(http)
    assert first[2]["envelope"]["type"] == "plaintext_message_envelope"
    assert second[2]["envelope"]["type"] == "message_envelope"


@pytest.mark.asyncio
async def test_format_mismatch_with_unchanged_policy_fails_once():
    policy = _Policy(False, refreshed=False)
    coordinator, http = await _policy_fixture(policy)

    original_post = http.post

    async def post(path, body=None):
        if path == CHANNEL_SEND_PATH:
            http.calls.append(("POST", path, body))
            raise HttpError(400, MISMATCH_BODY)
        return await original_post(path, body)

    http.post = post
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="hello")
    )
    assert result["state"] == "failed"
    assert result["error_kind"] == "channel_format_mismatch"
    assert policy.refreshes == ["ch_a"]
    assert len(_channel_posts(http)) == 1


@pytest.mark.asyncio
async def test_missing_policy_source_stays_encrypted():
    coordinator, http = await _policy_fixture(None)
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="hello")
    )
    assert result["state"] == "sent"
    (post,) = _channel_posts(http)
    assert post[2]["envelope"]["type"] == "message_envelope"


# ── client-side policy cache ─────────────────────────────────────────

from unittest.mock import AsyncMock, MagicMock

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient


def _bare_client():
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client._channel_space = {}
    client._channel_name_cache = {}
    client._channel_encrypted = {}
    client.http = MagicMock()
    client.store = MagicMock()
    client.store.mark_channel_space = AsyncMock()
    client.store.lookup_channel_space = AsyncMock(return_value=None)
    client._log = MagicMock()
    return client


@pytest.mark.asyncio
async def test_warm_caches_channel_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(disk_cache, "_cache_root", lambda: tmp_path)
    client = _bare_client()
    client.http.get = AsyncMock(return_value={"channels": [
        {"channel_id": "ch_p", "name": "open", "is_encrypted": False},
        {"channel_id": "ch_e", "name": "sealed", "is_encrypted": True},
        {"channel_id": "ch_legacy", "name": "old"},
    ]})
    await client._warm_channels_for_space("sp_1")
    assert client._channel_encrypted == {
        "ch_p": False,
        "ch_e": True,
        "ch_legacy": True,  # missing field fails safe
    }
    assert disk_cache.load_channel("ch_p")["is_encrypted"] is False
    assert client.channel_policy("ch_p") is False
    assert client.channel_policy("ch_unknown") is True


@pytest.mark.asyncio
async def test_channel_update_frame_updates_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(disk_cache, "_cache_root", lambda: tmp_path)
    client = _bare_client()
    await client._handle_channel_update({
        "type": "channel_update",
        "channel_id": "ch_p",
        "space_id": "sp_1",
        "is_encrypted": False,
    })
    assert client._channel_encrypted["ch_p"] is False
    assert disk_cache.load_channel("ch_p")["is_encrypted"] is False
    # A policy-less update (e.g. rename) leaves the policy alone.
    await client._handle_channel_update({
        "type": "channel_update",
        "channel_id": "ch_p",
        "space_id": "sp_1",
        "name": "renamed",
    })
    assert client._channel_encrypted["ch_p"] is False
    assert client._channel_name_cache["ch_p"] == "renamed"


@pytest.mark.asyncio
async def test_ensure_channel_policy_reads_disk_then_warms(monkeypatch, tmp_path):
    monkeypatch.setattr(disk_cache, "_cache_root", lambda: tmp_path)
    client = _bare_client()
    disk_cache.persist_channel("ch_disk", "general", "sp_1", False)
    assert await client.ensure_channel_policy("ch_disk") is False
    assert client._channel_encrypted["ch_disk"] is False

    # Unknown everywhere -> warm the space, then adopt the fetched policy.
    client.http.get = AsyncMock(return_value={"channels": [
        {"channel_id": "ch_new", "name": "fresh", "is_encrypted": False},
    ]})
    assert await client.ensure_channel_policy("ch_new", "sp_1") is False


@pytest.mark.asyncio
async def test_refresh_channel_policy_rewarnms_from_the_server(monkeypatch, tmp_path):
    monkeypatch.setattr(disk_cache, "_cache_root", lambda: tmp_path)
    client = _bare_client()
    client._channel_space["ch_p"] = "sp_1"
    client._channel_encrypted["ch_p"] = False
    client.http.get = AsyncMock(return_value={"channels": [
        {"channel_id": "ch_p", "name": "open", "is_encrypted": True},
    ]})
    assert await client.refresh_channel_policy("ch_p") is True
    assert client._channel_encrypted["ch_p"] is True


@pytest.mark.asyncio
async def test_eviction_clears_the_policy_cache():
    from puffo_agent.agent.membership_events import (
        evict_channel_caches,
        evict_space_caches,
    )

    client = _bare_client()
    client._channel_space.update({"ch_a": "sp_1", "ch_b": "sp_1"})
    client._channel_encrypted.update({"ch_a": False, "ch_b": True})
    client.store.unmark_channel_space = AsyncMock()
    await evict_channel_caches(
        channel_id="ch_a",
        channel_spaces=client._channel_space,
        channel_names=client._channel_name_cache,
        store=client.store,
        log=client._log,
        channel_policies=client._channel_encrypted,
    )
    assert "ch_a" not in client._channel_encrypted
    await evict_space_caches(
        space_id="sp_1",
        channel_spaces=client._channel_space,
        channel_names=client._channel_name_cache,
        space_names={},
        space_members={},
        store=client.store,
        log=client._log,
        channel_policies=client._channel_encrypted,
    )
    assert client._channel_encrypted == {}


@pytest.mark.asyncio
async def test_ws_channel_update_frame_dispatches():
    from puffo_agent.crypto.ws_client import PuffoCoreWsClient

    client = PuffoCoreWsClient.__new__(PuffoCoreWsClient)
    client.on_message = None
    client.on_event = None
    client.on_cert_update = None
    client.on_space_membership_changed = None
    client.on_connect = None
    seen: list[dict] = []

    async def handler(update):
        seen.append(update)

    client.on_channel_update = handler
    await client._handle_frame(json.dumps({
        "type": "channel_update",
        "channel_id": "ch_p",
        "space_id": "sp_1",
        "is_encrypted": False,
    }))
    assert seen and seen[0]["channel_id"] == "ch_p"
