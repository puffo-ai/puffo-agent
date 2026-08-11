from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from puffo_agent.agent import disk_cache
from puffo_agent.agent.channel_format import is_channel_format_mismatch
from puffo_agent.agent import puffo_core_client as pcc
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.keystore import encode_secret
from puffo_agent.crypto.http_client import HttpError
from puffo_agent.crypto.message import RecipientDevice
from puffo_agent.crypto.primitives import Ed25519KeyPair
from puffo_agent.mcp import puffo_core_tools as pct

FORMAT_MISMATCH = '{"error": "CHANNEL_FORMAT_MISMATCH", "message": "wrong"}'


def _client() -> PuffoCoreMessageClient:
    c = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    c._channel_space = {}
    c._channel_name_cache = {}
    c._channel_encrypted = {}
    c.http = MagicMock()
    c.store = MagicMock()
    c.store.mark_channel_space = AsyncMock()
    c._log = MagicMock()
    return c


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "served,expected",
    [(True, True), (False, False), (None, True)],
)
async def test_warm_records_server_policy(monkeypatch, served, expected):
    client = _client()
    client.http.get = AsyncMock(return_value={"channels": [
        {"channel_id": "ch_a", "name": "general", "is_encrypted": served},
    ]})
    monkeypatch.setattr(pcc.disk_cache, "persist_channel", lambda *a, **k: None)

    await client._warm_channels_for_space("sp_1")

    assert client._channel_encrypted["ch_a"] is expected
    assert client.channel_policy("ch_a") is expected


@pytest.mark.asyncio
async def test_absent_field_fails_safe_to_encrypted(monkeypatch):
    client = _client()
    client.http.get = AsyncMock(return_value={"channels": [
        {"channel_id": "ch_a", "name": "general"},
    ]})
    monkeypatch.setattr(pcc.disk_cache, "persist_channel", lambda *a, **k: None)

    await client._warm_channels_for_space("sp_1")

    assert client.channel_policy("ch_a") is True


@pytest.mark.asyncio
async def test_cache_ignores_invalid_channel_and_store_failure(monkeypatch):
    client = _client()
    assert await client._cache_channel({}, "sp_1") == ""

    client.store.mark_channel_space.side_effect = RuntimeError("db down")
    monkeypatch.setattr(pcc.disk_cache, "persist_channel", lambda *a, **k: None)

    assert await client._cache_channel(
        {"channel_id": "ch_a", "name": "general", "is_encrypted": False},
        "sp_1",
    ) == "ch_a"
    assert client.channel_policy("ch_a") is False

    assert await client._cache_channel(
        {"channel_id": "ch_without_name", "is_encrypted": False},
        "sp_1",
    ) == "ch_without_name"


@pytest.mark.asyncio
async def test_warm_ignores_malformed_channel_entries():
    client = _client()
    client.http.get = AsyncMock(return_value={"channels": [None]})

    await client._warm_channels_for_space("sp_1")

    assert client._channel_encrypted == {}


@pytest.mark.asyncio
async def test_policy_overwrites_rather_than_setdefault(monkeypatch):
    client = _client()
    client._channel_encrypted["ch_a"] = True
    client.http.get = AsyncMock(return_value={"channels": [
        {"channel_id": "ch_a", "name": "general", "is_encrypted": False},
    ]})
    monkeypatch.setattr(pcc.disk_cache, "persist_channel", lambda *a, **k: None)

    await client._warm_channels_for_space("sp_1")

    assert client.channel_policy("ch_a") is False


def test_policy_round_trips_through_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "_cache_root", lambda: tmp_path)
    disk_cache.persist_channel("ch_a", "general", "sp_1", False)

    assert disk_cache.load_channel("ch_a")["is_encrypted"] is False

    client = _client()
    monkeypatch.setattr(pcc.disk_cache, "_cache_root", lambda: tmp_path)
    assert client.channel_policy("ch_a") is False


def test_name_only_cache_update_preserves_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "_cache_root", lambda: tmp_path)
    disk_cache.persist_channel("ch_a", "general", "sp_1", False)

    disk_cache.persist_channel("ch_a", "renamed", "sp_1")

    assert disk_cache.load_channel("ch_a")["is_encrypted"] is False


@pytest.mark.asyncio
async def test_channel_update_refreshes_policy_and_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(pcc.disk_cache, "_cache_root", lambda: tmp_path)
    client = _client()

    await client._handle_channel_update({
        "type": "channel_update",
        "channel_id": "ch_a",
        "space_id": "sp_1",
        "name": "general",
        "is_encrypted": False,
    })

    assert client._channel_space["ch_a"] == "sp_1"
    assert client._channel_name_cache["ch_a"] == "general"
    assert client.channel_policy("ch_a") is False
    assert disk_cache.load_channel("ch_a")["is_encrypted"] is False
    client.store.mark_channel_space.assert_awaited_once_with("ch_a", "sp_1")


@pytest.mark.asyncio
async def test_name_only_channel_update_preserves_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(pcc.disk_cache, "_cache_root", lambda: tmp_path)
    disk_cache.persist_channel("ch_a", "general", "sp_1", False)
    client = _client()
    client._channel_encrypted["ch_a"] = False

    await client._handle_channel_update({
        "channel_id": "ch_a",
        "space_id": "sp_1",
        "name": "renamed",
        "is_encrypted": None,
    })

    assert client.channel_policy("ch_a") is False
    assert disk_cache.load_channel("ch_a")["name"] == "renamed"
    assert disk_cache.load_channel("ch_a")["is_encrypted"] is False


@pytest.mark.asyncio
async def test_channel_update_without_name_uses_id_and_ignores_bad_updates(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(pcc.disk_cache, "_cache_root", lambda: tmp_path)
    client = _client()
    client.store.mark_channel_space.side_effect = RuntimeError("db down")

    await client._handle_channel_update({
        "channel_id": "ch_created",
        "space_id": "sp_1",
        "is_encrypted": False,
    })
    await client._handle_channel_update({"space_id": "sp_1"})
    await client._handle_channel_update({"channel_id": "ch_bad"})

    cached = disk_cache.load_channel("ch_created")
    assert cached["name"] == "ch_created"
    assert cached["is_encrypted"] is False
    assert "ch_bad" not in client._channel_space


@pytest.mark.asyncio
async def test_listen_wires_channel_update_handler(monkeypatch):
    client = _client()
    client.slug = "alice-0001"
    client.keystore = MagicMock()
    client.keystore.load_identity.return_value = MagicMock(
        kem_secret_key=encode_secret(Ed25519KeyPair.generate().secret_bytes()),
        identity_cert_json="",
        server_url="http://localhost:3000",
    )
    client.store.open = AsyncMock()
    client._consume_queue = AsyncMock()
    client._invite_poll_loop = AsyncMock()
    client._warm_task = None
    ws = MagicMock()
    ws.run = AsyncMock()
    monkeypatch.setattr(pcc, "PuffoCoreWsClient", MagicMock(return_value=ws))

    await client.listen(AsyncMock())

    assert ws.on_channel_update == client._handle_channel_update


def test_legacy_null_cache_entry_fails_safe_to_encrypted(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_cache, "_cache_root", lambda: tmp_path)
    disk_cache._atomic_write_json(
        tmp_path / "channels" / "ch_a.json",
        {"channel_id": "ch_a", "name": "general", "is_encrypted": None},
    )

    assert _client().channel_policy("ch_a") is True


def test_unknown_channel_fails_safe_to_encrypted(tmp_path, monkeypatch):
    monkeypatch.setattr(pcc.disk_cache, "_cache_root", lambda: tmp_path)
    assert _client().channel_policy("ch_never_seen") is True
    assert _client().channel_policy("") is True


@pytest.mark.asyncio
async def test_ensure_policy_uses_disk_then_warms_server(tmp_path, monkeypatch):
    monkeypatch.setattr(pcc.disk_cache, "_cache_root", lambda: tmp_path)
    disk_cache.persist_channel("ch_disk", "disk", "sp_1", False)
    client = _client()

    assert await client.ensure_channel_policy("ch_disk", "sp_1") is False

    client.http.get = AsyncMock(return_value={"channels": [
        {"channel_id": "ch_server", "name": "server", "is_encrypted": False},
    ]})
    assert await client.ensure_channel_policy("ch_server", "sp_1") is False
    assert await client.ensure_channel_policy("ch_unknown", "") is True


@pytest.mark.asyncio
async def test_refresh_policy_resolves_space_and_keeps_value_on_fetch_failure():
    client = _client()
    client._channel_encrypted["ch_a"] = False
    client.store.lookup_channel_space = AsyncMock(return_value="sp_1")
    client.http.get = AsyncMock(side_effect=RuntimeError("offline"))

    assert await client.refresh_channel_policy("ch_a") is False
    client.store.lookup_channel_space.assert_awaited_once_with("ch_a")

    client._channel_space.clear()
    client.store.lookup_channel_space.side_effect = RuntimeError("db down")
    assert await client.refresh_channel_policy("ch_a") is False

    client._channel_space["ch_a"] = "sp_1"
    client.store.lookup_channel_space.reset_mock()
    assert await client.refresh_channel_policy("ch_a") is False
    client.store.lookup_channel_space.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_warm_ignores_fetch_failure():
    client = _client()
    client.http.get = AsyncMock(side_effect=RuntimeError("offline"))

    await client._bulk_fetch_profiles(["alice"])


@pytest.mark.asyncio
async def test_general_lookup_ignores_malformed_channel(monkeypatch):
    client = _client()
    client.http.get = AsyncMock(return_value={"channels": [None]})
    monkeypatch.setattr(pcc.asyncio, "sleep", AsyncMock())

    assert await client._find_public_general_channel("sp_1") == ""


def test_format_mismatch_reads_the_server_error_code():
    assert is_channel_format_mismatch(HttpError(400, FORMAT_MISMATCH)) is True


def test_unrelated_errors_are_not_treated_as_format_mismatch():
    assert is_channel_format_mismatch(HttpError(400, '{"error": "bad nonce"}')) is False
    assert is_channel_format_mismatch(HttpError(403, FORMAT_MISMATCH)) is False
    assert is_channel_format_mismatch(HttpError(400, "not json")) is False
    assert is_channel_format_mismatch(RuntimeError("boom")) is False


class _Cfg:
    def __init__(
        self,
        fail_first: str | None = None,
        refreshed_policy: bool | None = None,
    ):
        self.slug = "alice-0001"
        self.posts: list[tuple[str, dict]] = []
        self._fail_first = fail_first
        self.refreshed: list[str] = []
        self.http_client = MagicMock()
        self.http_client.post = AsyncMock(side_effect=self._post)
        self.http_client.get = AsyncMock(return_value={"certs": []})
        self.data_client = MagicMock()
        self.data_client.refresh_channel_policy = AsyncMock(
            side_effect=lambda cid: (
                self.refreshed.append(cid) or refreshed_policy
            )
        )

    async def _post(self, path, body):
        self.posts.append((path, body))
        if self._fail_first == path and len(self.posts) == 1:
            raise HttpError(
                400,
                FORMAT_MISMATCH,
            )
        return {}


def _patch_builders(monkeypatch):
    monkeypatch.setattr(
        pct, "build_plaintext_message",
        lambda inp, key: {"envelope_id": "e1", "type": "plaintext_message_envelope"},
    )
    monkeypatch.setattr(
        pct, "encrypt_message_with_content_key",
        lambda inp, key: ({"envelope_id": "e1", "type": "message_envelope"}, b"k"),
    )
    fetch = AsyncMock(return_value=[MagicMock(device_id="d1")])
    monkeypatch.setattr(
        pct, "_fetch_device_keys",
        fetch,
    )
    return fetch


@pytest.mark.asyncio
async def test_sealed_send_to_flipped_plaintext_channel_recovers(monkeypatch):
    _patch_builders(monkeypatch)
    cfg = _Cfg(fail_first="/messages", refreshed_policy=False)
    inp = MagicMock()

    env = await pct._post_respecting_channel_format(
        cfg, inp, MagicMock(), True, ["alice-0001"], "ch_abc",
    )

    assert [p for p, _ in cfg.posts] == ["/messages", "/v2/messages/plaintext"]
    assert env["type"] == "plaintext_message_envelope"
    assert cfg.refreshed == ["ch_abc"]


@pytest.mark.asyncio
async def test_plaintext_send_to_flipped_encrypted_channel_recovers(monkeypatch):
    _patch_builders(monkeypatch)
    cfg = _Cfg(fail_first="/v2/messages/plaintext", refreshed_policy=True)
    inp = MagicMock()
    recipients = AsyncMock(return_value=["alice-0001"])
    monkeypatch.setattr(pct, "_resolve_channel_recipient_slugs", recipients)

    env = await pct._post_respecting_channel_format(
        cfg, inp, MagicMock(), False, [], "ch_abc", "sp_1",
    )

    assert [p for p, _ in cfg.posts] == ["/v2/messages/plaintext", "/messages"]
    assert env["type"] == "message_envelope"
    assert cfg.refreshed == ["ch_abc"]
    recipients.assert_awaited_once_with(cfg, "sp_1", "ch_abc")


@pytest.mark.asyncio
async def test_no_retry_when_the_server_keeps_rejecting(monkeypatch):
    _patch_builders(monkeypatch)
    cfg = _Cfg(refreshed_policy=False)
    cfg.http_client.post = AsyncMock(side_effect=HttpError(400, FORMAT_MISMATCH))

    with pytest.raises(HttpError):
        await pct._post_respecting_channel_format(
            cfg, MagicMock(), MagicMock(), True, ["alice-0001"], "ch_abc",
        )
    assert cfg.http_client.post.await_count == 2


@pytest.mark.asyncio
async def test_no_retry_when_refresh_does_not_change_policy(monkeypatch):
    _patch_builders(monkeypatch)
    cfg = _Cfg(fail_first="/messages", refreshed_policy=True)

    with pytest.raises(HttpError):
        await pct._post_respecting_channel_format(
            cfg, MagicMock(), MagicMock(), True, ["alice-0001"], "ch_abc",
        )

    assert [p for p, _ in cfg.posts] == ["/messages"]
    assert cfg.refreshed == ["ch_abc"]


@pytest.mark.asyncio
async def test_refresh_failure_does_not_guess_the_new_format(monkeypatch):
    _patch_builders(monkeypatch)
    cfg = _Cfg(fail_first="/messages")
    cfg.data_client.refresh_channel_policy.side_effect = RuntimeError("offline")

    with pytest.raises(HttpError):
        await pct._post_respecting_channel_format(
            cfg, MagicMock(), MagicMock(), True, ["alice-0001"], "ch_abc",
        )

    assert [p for p, _ in cfg.posts] == ["/messages"]


@pytest.mark.asyncio
async def test_refresh_without_channel_or_client_returns_none():
    cfg = MagicMock()
    cfg.data_client = MagicMock(spec=[])
    assert await pct._refresh_channel_policy(cfg, "ch_abc") is None
    assert await pct._refresh_channel_policy(cfg, None) is None


@pytest.mark.asyncio
async def test_unrelated_error_is_surfaced_not_retried(monkeypatch):
    _patch_builders(monkeypatch)
    cfg = _Cfg()
    cfg.http_client.post = AsyncMock(
        side_effect=HttpError(400, '{"error": "bad nonce"}')
    )

    with pytest.raises(HttpError):
        await pct._post_respecting_channel_format(
            cfg, MagicMock(), MagicMock(), False, ["alice-0001"], "ch_abc",
        )
    assert cfg.http_client.post.await_count == 1
    assert cfg.refreshed == []


@pytest.mark.asyncio
async def test_happy_path_sends_once(monkeypatch):
    fetch = _patch_builders(monkeypatch)
    cfg = _Cfg()

    await pct._post_respecting_channel_format(
        cfg, MagicMock(), MagicMock(), False, ["alice-0001"], "ch_abc",
    )
    assert [p for p, _ in cfg.posts] == ["/v2/messages/plaintext"]
    assert cfg.refreshed == []
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_encrypted_send_schedules_missing_device_supplement(monkeypatch):
    _patch_builders(monkeypatch)
    cfg = _Cfg()
    cfg.http_client.post = AsyncMock(return_value={"missing_devices": ["dev_2"]})
    scheduled = []

    def schedule(coro):
        scheduled.append(coro)
        coro.close()
        return MagicMock()

    monkeypatch.setattr(pct.asyncio, "create_task", schedule)

    await pct._post_respecting_channel_format(
        cfg, MagicMock(), MagicMock(), True, ["alice-0001"], "ch_abc",
    )

    assert len(scheduled) == 1


@pytest.mark.asyncio
async def test_encrypted_send_requires_recipient_devices(monkeypatch):
    fetch = _patch_builders(monkeypatch)
    fetch.return_value = []

    with pytest.raises(RuntimeError, match="no recipient devices"):
        await pct._post_respecting_channel_format(
            _Cfg(), MagicMock(), MagicMock(), True,
            ["alice-0001"], "ch_abc",
        )


@pytest.mark.asyncio
async def test_encrypted_channel_requires_resolvable_members():
    cfg = _Cfg()
    cfg.http_client.get = AsyncMock(return_value={"members": [{"slug": ""}]})

    with pytest.raises(RuntimeError, match="no resolvable members"):
        await pct._resolve_channel_recipient_slugs(cfg, "sp_1", "ch_abc")


@pytest.mark.asyncio
async def test_missing_shim_fails_safe_to_encrypted():
    cfg = MagicMock(spec=["slug", "data_client"])
    cfg.slug = "alice-0001"
    cfg.data_client = MagicMock(spec=[])
    assert await pct._send_encryption_required(cfg, "ch_abc") is True


def _fallback_client(initial_policy: bool, refreshed_policy: bool | None = None):
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "alice-0001"
    client._channel_space = {"ch_abc": "sp_1"}
    client._channel_encrypted = {"ch_abc": initial_policy}
    client._log = MagicMock()
    client.store = MagicMock()

    key = Ed25519KeyPair.generate()

    class _Session:
        subkey_id = "sk_1"
        subkey_secret_key = encode_secret(key.secret_bytes())

    client.keystore = MagicMock()
    client.keystore.load_session.return_value = _Session()

    posts = []

    async def post(path, body):
        posts.append(path)
        if refreshed_policy is not None and len(posts) == 1:
            raise HttpError(400, FORMAT_MISMATCH)
        return {"devices_queued": 1}

    client.http = MagicMock()
    client.http.post = AsyncMock(side_effect=post)
    client.http.get = AsyncMock(return_value={
        "members": [{"slug": "alice-0001"}],
    })
    client._fetch_device_keys = AsyncMock(return_value=[
        RecipientDevice(device_id="dev_1", kem_public_key=b"k" * 32),
    ])

    async def refresh(channel_id):
        assert channel_id == "ch_abc"
        assert refreshed_policy is not None
        client._channel_encrypted[channel_id] = refreshed_policy
        return refreshed_policy

    client.refresh_channel_policy = AsyncMock(side_effect=refresh)
    return client, posts


@pytest.mark.asyncio
async def test_plaintext_fallback_send_needs_no_recipients():
    client, posts = _fallback_client(False)

    await client.send_fallback_message("ch_abc", "hello")

    assert posts == ["/v2/messages/plaintext"]
    client.http.get.assert_not_awaited()
    client._fetch_device_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_send_refreshes_policy_before_format_retry():
    client, posts = _fallback_client(False, refreshed_policy=True)

    await client.send_fallback_message("ch_abc", "hello")

    assert posts == ["/v2/messages/plaintext", "/messages"]
    client.refresh_channel_policy.assert_awaited_once_with("ch_abc")
    client._fetch_device_keys.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallback_send_stops_when_encrypted_channel_has_no_members():
    client, posts = _fallback_client(True)
    client.http.get = AsyncMock(return_value={"members": []})

    await client.send_fallback_message("ch_abc", "hello")

    assert posts == []
    client._fetch_device_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_send_stops_when_encrypted_channel_has_no_devices():
    client, posts = _fallback_client(True)
    client._fetch_device_keys = AsyncMock(return_value=[])

    await client.send_fallback_message("ch_abc", "hello")

    assert posts == []


@pytest.mark.asyncio
async def test_fallback_send_surfaces_unrelated_post_error():
    client, _ = _fallback_client(False)
    client.http.post = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await client.send_fallback_message("ch_abc", "hello")


@pytest.mark.asyncio
async def test_fallback_send_does_not_retry_when_refreshed_policy_is_unchanged():
    client, _ = _fallback_client(False, refreshed_policy=False)

    with pytest.raises(HttpError):
        await client.send_fallback_message("ch_abc", "hello")

    client.refresh_channel_policy.assert_awaited_once_with("ch_abc")


@pytest.mark.asyncio
async def test_fallback_dm_is_always_encrypted():
    client, posts = _fallback_client(False)
    client._last_dm_sender = "operator-0001"

    await client.send_fallback_message("", "hello")

    assert posts == ["/messages"]
    client._fetch_device_keys.assert_awaited_once_with(
        ["alice-0001", "operator-0001"],
    )
