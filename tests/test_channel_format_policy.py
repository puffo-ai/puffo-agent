"""PUF-411: channel format policy drives the send endpoint, and a policy
flip mid-send is recovered from rather than surfaced as an error."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from puffo_agent.agent import disk_cache, send_mode
from puffo_agent.agent import puffo_core_client as pcc
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.http_client import HttpError
from puffo_agent.mcp import puffo_core_tools as pct

PLAINTEXT_400 = (
    '{"error": "channel ch_abc is plaintext; send via '
    'POST /v2/messages/plaintext instead"}'
)
ENCRYPTED_400 = (
    '{"error": "channel ch_abc is encrypted; send a sealed envelope '
    'to POST /messages instead"}'
)


# ── send_mode: policy beats the source-based rules ──────────────────

@pytest.mark.asyncio
async def test_no_policy_leaves_source_rules_untouched():
    """The option-(b) contract: every channel predating PUF-410 reports
    NULL, and must behave exactly as it did before this ticket."""
    send_mode.note_turn_bundle(["a"], True)
    try:
        assert await send_mode.encryption_required("a", None, None, None) is True
        assert await send_mode.encryption_required("b", None, None, None) is False
    finally:
        send_mode.clear_turn_bundle(["a"])


@pytest.mark.asyncio
async def test_policy_forces_encryption_over_plaintext_default():
    assert await send_mode.encryption_required("b", None, None, True) is True


@pytest.mark.asyncio
async def test_plaintext_policy_overrides_encrypted_turn_bundle():
    """The deliberate downgrade: an explicitly-plaintext channel wins
    even over the never-downgrade rule, because the server refuses a
    sealed write there — the alternative is a message that never sends."""
    send_mode.note_turn_bundle(["a"], True)
    try:
        assert await send_mode.encryption_required("a", None, None, False) is False
    finally:
        send_mode.clear_turn_bundle(["a"])


# ── cache: server value lands and round-trips ───────────────────────

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
    [(True, True), (False, False), (None, None)],
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
async def test_absent_field_reads_as_no_policy(monkeypatch):
    """A server too old to send the field must not read as 'encrypted'."""
    client = _client()
    client.http.get = AsyncMock(return_value={"channels": [
        {"channel_id": "ch_a", "name": "general"},
    ]})
    monkeypatch.setattr(pcc.disk_cache, "persist_channel", lambda *a, **k: None)

    await client._warm_channels_for_space("sp_1")

    assert client.channel_policy("ch_a") is None


@pytest.mark.asyncio
async def test_policy_overwrites_rather_than_setdefault(monkeypatch):
    """A flipped policy must land — a stale cached value is exactly what
    the server guard rejects."""
    client = _client()
    client._channel_encrypted["ch_a"] = True
    client.http.get = AsyncMock(return_value={"channels": [
        {"channel_id": "ch_a", "name": "general", "is_encrypted": False},
    ]})
    monkeypatch.setattr(pcc.disk_cache, "persist_channel", lambda *a, **k: None)

    await client._warm_channels_for_space("sp_1")

    assert client.channel_policy("ch_a") is False


def test_policy_round_trips_through_disk(tmp_path, monkeypatch):
    """Survives a daemon restart: an empty in-memory cache falls back to
    what was persisted."""
    monkeypatch.setattr(disk_cache, "_cache_root", lambda: tmp_path)
    disk_cache.persist_channel("ch_a", "general", "sp_1", False)

    assert disk_cache.load_channel("ch_a")["is_encrypted"] is False

    client = _client()
    monkeypatch.setattr(pcc.disk_cache, "_cache_root", lambda: tmp_path)
    assert client.channel_policy("ch_a") is False


def test_unknown_channel_has_no_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(pcc.disk_cache, "_cache_root", lambda: tmp_path)
    assert _client().channel_policy("ch_never_seen") is None
    assert _client().channel_policy("") is None


# ── the 400 → reshape → resend recovery ─────────────────────────────

def test_format_mismatch_reads_the_servers_verdict():
    assert pct._format_mismatch(HttpError(400, PLAINTEXT_400)) is False
    assert pct._format_mismatch(HttpError(400, ENCRYPTED_400)) is True


def test_unrelated_errors_are_not_treated_as_format_mismatch():
    """Retrying an unrelated 400 by flipping format would send the wrong
    shape and mask the real error."""
    assert pct._format_mismatch(HttpError(400, '{"error": "bad nonce"}')) is None
    assert pct._format_mismatch(HttpError(403, PLAINTEXT_400)) is None
    assert pct._format_mismatch(HttpError(500, PLAINTEXT_400)) is None
    assert pct._format_mismatch(RuntimeError("boom")) is None


class _Cfg:
    def __init__(self, fail_first: str | None = None):
        self.slug = "alice-0001"
        self.posts: list[tuple[str, dict]] = []
        self._fail_first = fail_first
        self.refreshed: list[str] = []
        self.http_client = MagicMock()
        self.http_client.post = AsyncMock(side_effect=self._post)
        self.http_client.get = AsyncMock(return_value={"certs": []})
        self.data_client = MagicMock()
        self.data_client.refresh_channel_policy = AsyncMock(
            side_effect=lambda cid: self.refreshed.append(cid)
        )

    async def _post(self, path, body):
        self.posts.append((path, body))
        if self._fail_first == path and len(self.posts) == 1:
            raise HttpError(
                400,
                PLAINTEXT_400 if path == "/messages" else ENCRYPTED_400,
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
    monkeypatch.setattr(
        pct, "_fetch_device_keys",
        AsyncMock(return_value=[MagicMock(device_id="d1")]),
    )


@pytest.mark.asyncio
async def test_sealed_send_to_flipped_plaintext_channel_recovers(monkeypatch):
    _patch_builders(monkeypatch)
    cfg = _Cfg(fail_first="/messages")
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
    cfg = _Cfg(fail_first="/v2/messages/plaintext")
    inp = MagicMock()

    env = await pct._post_respecting_channel_format(
        cfg, inp, MagicMock(), False, ["alice-0001"], "ch_abc",
    )

    assert [p for p, _ in cfg.posts] == ["/v2/messages/plaintext", "/messages"]
    assert env["type"] == "message_envelope"


@pytest.mark.asyncio
async def test_no_retry_when_the_server_keeps_rejecting(monkeypatch):
    """A second rejection of the shape the server itself asked for is a
    server bug; looping would amplify it."""
    _patch_builders(monkeypatch)
    cfg = _Cfg()
    cfg.http_client.post = AsyncMock(side_effect=HttpError(400, PLAINTEXT_400))

    with pytest.raises(HttpError):
        await pct._post_respecting_channel_format(
            cfg, MagicMock(), MagicMock(), True, ["alice-0001"], "ch_abc",
        )
    assert cfg.http_client.post.await_count == 2


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
    _patch_builders(monkeypatch)
    cfg = _Cfg()

    await pct._post_respecting_channel_format(
        cfg, MagicMock(), MagicMock(), False, ["alice-0001"], "ch_abc",
    )
    assert [p for p, _ in cfg.posts] == ["/v2/messages/plaintext"]
    assert cfg.refreshed == []


# ── the old-shim guard ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_old_two_arg_shim_still_answers():
    """A data-client pinned to the pre-PUF-411 signature must keep
    working rather than fail the send."""
    cfg = MagicMock()
    cfg.slug = "alice-0001"

    async def old_shim(slug, root):
        return True

    cfg.data_client.get_send_encryption = old_shim
    assert await pct._send_encryption_required(cfg, None, "ch_abc") is True


@pytest.mark.asyncio
async def test_missing_shim_fails_safe_to_encrypted():
    cfg = MagicMock(spec=["slug", "data_client"])
    cfg.slug = "alice-0001"
    cfg.data_client = MagicMock(spec=[])
    assert await pct._send_encryption_required(cfg, None, "ch_abc") is True
