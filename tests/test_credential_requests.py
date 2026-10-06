"""Agentic credential setup: the DM contract, the ledger, placement, and the
untrusted filed reply (``portal/credential_requests``).

The property every test here guards: a fetched value reaches its destination
and NOTHING else — not a tool result, not a message, not a log line."""

from __future__ import annotations

import json
import logging
import os
import stat
import time
import uuid
from types import SimpleNamespace

import pytest

from puffo_agent.portal import credential_requests as cr
from puffo_agent.portal.credentials import HeldCredential

OWNER = "op-test"
SECRET = b"sk-live-THE-SECRET-VALUE-0123456789"


def _dm(sender: str, text: str, kind: str = "dm"):
    return SimpleNamespace(envelope_kind=kind, sender_slug=sender, content=text)


def _filed(request_id: str, ctype: str, index: int = 0, version: int = 1) -> str:
    block = json.dumps({"request_id": request_id, "type": ctype, "index": index, "version": version})
    return f"Filed it for you.\n\n```{cr.FILED_FENCE}\n{block}\n```"


class FakeCredentials:
    """What handle_filed_replies needs of AgentCredentials / CloudAgentCredentials."""

    def __init__(self, *, listed=(), held=None, get_error=None, list_error=None):
        self.listed = list(listed)
        self.held_value = held
        self.get_error = get_error
        self.list_error = list_error
        self.get_calls: list[tuple[str, int]] = []
        self.held_calls: list[str] = []

    async def held(self, ctype):
        self.held_calls.append(ctype)
        if self.list_error:
            raise self.list_error
        return [(i, "") for i in self.listed]

    async def get(self, ctype, index):
        self.get_calls.append((ctype, index))
        if self.get_error:
            raise self.get_error
        return self.held_value


def _held(ctype=cr.TYPE_CLAUDE_TOKEN, index=0, version=1, value=SECRET):
    return HeldCredential(id="cid", type=ctype, index=index, version=version, expire_at=None, value=value)


# ── the pinned message contract ──────────────────────────────────────────────


def test_request_message_is_a_sentence_then_the_fenced_block():
    rid = str(uuid.uuid4())
    body = cr.build_request_message(request_id=rid, credential_type=cr.TYPE_CHATGPT, reason="to run Codex")
    sentence, _, rest = body.partition("\n\n")
    assert "{" not in sentence and "secure form" in sentence
    assert rest.startswith(f"```{cr.REQUEST_FENCE}\n") and rest.endswith("\n```")
    data = json.loads(rest.split("\n")[1])
    assert data == {"request_id": rid, "type": cr.TYPE_CHATGPT, "reason": "to run Codex"}
    with_alias = cr.build_request_message(
        request_id=rid, credential_type=cr.TYPE_CUSTOMIZED, reason="r", alias="GITHUB_TOKEN"
    )
    assert json.loads(with_alias.split("\n")[-2])["alias"] == "GITHUB_TOKEN"


def test_parse_filed_reply_accepts_exactly_the_pinned_shape():
    rid = str(uuid.uuid4())
    assert cr.parse_filed_reply(_filed(rid, cr.TYPE_CHATGPT, 2, 3)) == {
        "request_id": rid, "type": cr.TYPE_CHATGPT, "index": 2, "version": 3,
    }


@pytest.mark.parametrize(
    "text",
    [
        "no block here",
        # the REQUEST fence must never be mistaken for a filed reply
        cr.build_request_message(request_id=str(uuid.uuid4()), credential_type=cr.TYPE_CHATGPT, reason="r"),
        _filed("not-a-uuid", cr.TYPE_CHATGPT),
        _filed(str(uuid.uuid4()), "PUFFO_SOMETHING_ELSE_v1"),
        _filed(str(uuid.uuid4()), cr.TYPE_CHATGPT, index=-1),
        _filed(str(uuid.uuid4()), cr.TYPE_CHATGPT, version=0),
        f"```{cr.FILED_FENCE}\n[1,2,3]\n```",
        f"```{cr.FILED_FENCE}\nnot json\n```",
        f"```{cr.FILED_FENCE}\n" + json.dumps({"request_id": str(uuid.uuid4()), "type": cr.TYPE_CHATGPT, "index": True, "version": 1}) + "\n```",
    ],
)
def test_parse_filed_reply_rejects_anything_else_silently(text):
    assert cr.parse_filed_reply(text) is None


def test_message_text_reads_str_and_dict_content():
    assert cr.message_text("x") == "x"
    assert cr.message_text({"text": "y"}) == "y"
    assert cr.message_text({"other": 1}) == ""
    assert cr.message_text(None) == ""


# ── ledger ───────────────────────────────────────────────────────────────────


def test_ledger_persists_at_0600_and_settles_once(tmp_path):
    path = tmp_path / "credential_requests.json"
    ledger = cr.RequestLedger(path)
    req = ledger.issue(credential_type=cr.TYPE_CHATGPT, reason="r")
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # survives a restart
    again = cr.RequestLedger(path)
    assert again.get(req.request_id).state == "pending"
    assert [r.request_id for r in again.pending()] == [req.request_id]
    again.settle(req.request_id, state="placed", index=0, version=1)
    again.settle(req.request_id, state="failed", detail="replay")  # no-op: consumed
    assert cr.RequestLedger(path).get(req.request_id).state == "placed"


def test_ledger_tolerates_a_corrupt_file(tmp_path):
    path = tmp_path / "credential_requests.json"
    path.write_text("{not json")
    assert cr.RequestLedger(path).pending() == []


# ── placement ────────────────────────────────────────────────────────────────


def test_place_writes_a_private_store_the_spawn_path_reads(tmp_path):
    key = cr.place(agent_dir=tmp_path, credential_type=cr.TYPE_CLAUDE_TOKEN, index=0, version=2, value=SECRET)
    assert key == cr.TYPE_CLAUDE_TOKEN
    p = cr.store_path(tmp_path)
    assert p.parent == tmp_path and "workspace" not in str(p)
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert cr.stored_value(tmp_path, cr.TYPE_CLAUDE_TOKEN) == SECRET.decode()
    assert cr.stored_value(tmp_path, cr.TYPE_CHATGPT) == ""


def test_customized_is_keyed_by_alias_and_exposed_as_env(tmp_path):
    cr.place(agent_dir=tmp_path, credential_type=cr.TYPE_CUSTOMIZED, index=0, version=1, value=b"a", alias="GITHUB_TOKEN")
    cr.place(agent_dir=tmp_path, credential_type=cr.TYPE_CUSTOMIZED, index=1, version=1, value=b"b", alias="NPM_TOKEN")
    assert cr.customized_env(tmp_path) == {"GITHUB_TOKEN": "a", "NPM_TOKEN": "b"}
    with pytest.raises(ValueError):
        cr.place(agent_dir=tmp_path, credential_type=cr.TYPE_CUSTOMIZED, index=2, version=1, value=b"c", alias="bad name")


# ── the untrusted filed reply ────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path):
    agent_dir = tmp_path / "agent"
    workspace = tmp_path / "ws"
    agent_dir.mkdir(); workspace.mkdir()
    ledger = cr.RequestLedger(agent_dir / "credential_requests.json")
    req = ledger.issue(credential_type=cr.TYPE_CLAUDE_TOKEN, reason="r")
    return SimpleNamespace(agent_dir=agent_dir, workspace=workspace, ledger=ledger, req=req)


async def _run(env, items, creds):
    return await cr.handle_filed_replies(
        items, owner_slug=OWNER, ledger=env.ledger, credentials=creds,
        agent_dir=env.agent_dir, workspace=env.workspace, harness="claude-code", agent_id="a1",
    )


@pytest.mark.asyncio
async def test_owner_reply_places_consumes_and_requests_a_reload(env, caplog):
    creds = FakeCredentials(listed=[0], held=_held())
    with caplog.at_level(logging.INFO):
        out = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN))], creds)
    assert [(o.state) for o in out] == ["placed"]
    assert cr.stored_value(env.agent_dir, cr.TYPE_CLAUDE_TOKEN) == SECRET.decode()
    flag = env.workspace / ".puffo-agent" / "refresh_provider_auth.flag"
    assert flag.exists() and "credential placed" in flag.read_text()
    assert env.ledger.get(env.req.request_id).state == "placed"
    # confirmed against the server's list BEFORE the fetch
    assert creds.held_calls == [cr.TYPE_CLAUDE_TOKEN] and creds.get_calls == [(cr.TYPE_CLAUDE_TOKEN, 0)]
    # THE property: the value is nowhere but the store
    assert SECRET.decode() not in caplog.text
    assert all(SECRET.decode() not in (o.detail or "") for o in out)
    assert cr.fingerprint(SECRET) in caplog.text
    # replay: consumed once — and with nothing pending the handler does no work at all
    out2 = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN))], creds)
    assert out2 == [] and len(creds.get_calls) == 1


@pytest.mark.asyncio
async def test_a_reply_from_anyone_but_the_owner_is_not_acted_on(env):
    creds = FakeCredentials(listed=[0], held=_held())
    out = await _run(env, [_dm("someone-else", _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN))], creds)
    assert out == [] and creds.get_calls == [] and creds.held_calls == []
    assert env.ledger.get(env.req.request_id).state == "pending"
    assert cr.read_store(env.agent_dir) == {}


@pytest.mark.asyncio
async def test_a_channel_message_is_not_acted_on_even_from_the_owner(env):
    creds = FakeCredentials(listed=[0], held=_held())
    out = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN), kind="channel")], creds)
    assert out == [] and creds.get_calls == []


@pytest.mark.asyncio
async def test_an_unknown_request_id_is_ignored(env):
    creds = FakeCredentials(listed=[0], held=_held())
    out = await _run(env, [_dm(OWNER, _filed(str(uuid.uuid4()), cr.TYPE_CLAUDE_TOKEN))], creds)
    assert out[0].state == "ignored" and creds.get_calls == []


@pytest.mark.asyncio
async def test_a_type_that_differs_from_the_request_fails_it(env):
    creds = FakeCredentials(listed=[0], held=_held(ctype=cr.TYPE_CHATGPT))
    out = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CHATGPT))], creds)
    assert out[0].state == "failed" and creds.get_calls == []
    assert env.ledger.get(env.req.request_id).state == "failed"


@pytest.mark.asyncio
async def test_an_index_the_server_does_not_list_for_us_is_refused_without_a_fetch(env):
    creds = FakeCredentials(listed=[7], held=_held())
    out = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN, index=0))], creds)
    assert out[0].state == "failed" and "not listed" in out[0].detail
    assert creds.get_calls == []


@pytest.mark.asyncio
async def test_not_distributed_fails_the_request(env):
    creds = FakeCredentials(listed=[0], held=None)
    out = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN))], creds)
    assert out[0].state == "failed" and "not distributed" in out[0].detail
    assert cr.read_store(env.agent_dir) == {}


@pytest.mark.asyncio
async def test_a_fetch_error_never_escapes_and_names_no_value(env):
    creds = FakeCredentials(listed=[0], get_error=RuntimeError(SECRET.decode()))
    out = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN))], creds)
    assert out[0].state == "failed"
    assert SECRET.decode() not in out[0].detail and SECRET.decode() not in env.ledger.get(env.req.request_id).detail


@pytest.mark.asyncio
async def test_a_list_error_never_escapes(env):
    creds = FakeCredentials(list_error=RuntimeError("boom"))
    out = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN))], creds)
    assert out[0].state == "failed" and creds.get_calls == []


@pytest.mark.asyncio
async def test_a_server_behind_the_reply_leaves_the_request_pending(env):
    creds = FakeCredentials(listed=[0], held=_held(version=1))
    out = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN, version=2))], creds)
    assert out[0].state == "ignored" and env.ledger.get(env.req.request_id).state == "pending"
    assert cr.read_store(env.agent_dir) == {}


@pytest.mark.asyncio
async def test_a_refiled_newer_version_bypasses_the_readers_cache(env):
    """Found by the cloud rotation test: get() serves memory while unexpired,
    so without forget() a newer filing would read as "server behind" forever."""

    class CachingCreds(FakeCredentials):
        def __init__(self):
            super().__init__(listed=[0])
            self.versions = iter([_held(version=1, value=b"v1"), _held(version=2, value=b"v2")])
            self.forgotten = []

        def forget(self, ctype, index):
            self.forgotten.append((ctype, index))

        async def get(self, ctype, index):
            self.get_calls.append((ctype, index))
            return next(self.versions)

    creds = CachingCreds()
    first = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN, version=1))], creds)
    assert first[0].state == "placed"
    req2 = env.ledger.issue(credential_type=cr.TYPE_CLAUDE_TOKEN, reason="again")
    second = await _run(env, [_dm(OWNER, _filed(req2.request_id, cr.TYPE_CLAUDE_TOKEN, version=2))], creds)
    assert second[0].state == "placed"
    assert creds.forgotten == [(cr.TYPE_CLAUDE_TOKEN, 0)] * 2
    assert cr.stored_value(env.agent_dir, cr.TYPE_CLAUDE_TOKEN) == "v2"


def test_forget_reaches_agent_credentials_private_invalidate():
    """The native reader has no forget(); its generation fence is the seam."""
    calls = []

    class Native:
        def _invalidate(self, drop):
            calls.append(drop)

    cr._forget(Native(), cr.TYPE_CHATGPT, 3)
    assert len(calls) == 1
    assert calls[0](SimpleNamespace(type=cr.TYPE_CHATGPT, index=3)) is True
    assert calls[0](SimpleNamespace(type=cr.TYPE_CHATGPT, index=4)) is False
    cr._forget(object(), cr.TYPE_CHATGPT, 3)  # neither: a no-op, never raises


@pytest.mark.asyncio
async def test_no_credential_reader_fails_cleanly(env):
    out = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN))], None)
    assert out[0].state == "failed"


def test_status_line_never_carries_the_value():
    req = cr.CredentialRequest(request_id="x", type=cr.TYPE_CHATGPT, reason="r", state="placed", index=1, version=3)
    assert cr.status_line(req) == f"placed {cr.TYPE_CHATGPT} #1 v3; restarting my CLI to pick it up"
    assert cr.status_line(None) == "unknown request_id"
    pending = cr.CredentialRequest(request_id="x", type=cr.TYPE_CHATGPT, reason="r")
    assert cr.status_line(pending).startswith("pending")
    failed = cr.CredentialRequest(request_id="x", type=cr.TYPE_CHATGPT, reason="r", state="failed", detail="why")
    assert cr.status_line(failed) == "failed: why"
    for r in (req, pending, failed):
        assert "value" not in cr.status_line(r) and SECRET.decode() not in cr.status_line(r)


# ── boot reconcile ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_reconcile_places_what_the_server_holds(tmp_path):
    creds = FakeCredentials(listed=[0], held=_held(ctype=cr.TYPE_CHATGPT))
    assert await cr.reconcile_at_boot(credentials=creds, agent_dir=tmp_path, harness="codex") is True
    assert cr.stored_value(tmp_path, cr.TYPE_CHATGPT) == SECRET.decode()
    assert creds.held_calls == [cr.TYPE_CHATGPT]
    # idempotent at the same version
    assert await cr.reconcile_at_boot(credentials=creds, agent_dir=tmp_path, harness="codex") is False


@pytest.mark.asyncio
async def test_boot_reconcile_says_so_on_every_branch(tmp_path, caplog):
    """Found on the first staging rebuild: with nothing in CCS the fall-through
    was SILENT, so the log could not distinguish "asked, nothing listed" from
    "never ran". Every branch now leaves one INFO line; none carries a value."""
    with caplog.at_level(logging.INFO):
        assert await cr.reconcile_at_boot(credentials=FakeCredentials(listed=[]), agent_dir=tmp_path, harness="codex", agent_id="a1") is False
        assert "server lists no PUFFO_CHATGPT_CREDENTIAL_JSON_v1" in caplog.text and "using the environment" in caplog.text
        assert SECRET.decode() not in caplog.text
        caplog.clear()
        creds = FakeCredentials(listed=[0], held=_held(ctype=cr.TYPE_CHATGPT))
        assert await cr.reconcile_at_boot(credentials=creds, agent_dir=tmp_path, harness="codex", agent_id="a1") is True
        assert "boot reconcile placed" in caplog.text
        # the branch that handles the real value: checked BEFORE the clear, or
        # the leak check never sees it (review of #454)
        assert SECRET.decode() not in caplog.text and cr.fingerprint(SECRET) in caplog.text
        caplog.clear()
        assert await cr.reconcile_at_boot(credentials=creds, agent_dir=tmp_path, harness="codex", agent_id="a1") is False
        assert "already placed" in caplog.text
        assert SECRET.decode() not in caplog.text


@pytest.mark.asyncio
async def test_boot_reconcile_registers_the_key_before_listing(tmp_path, caplog):
    """A pre-#437 cloud agent has no key; the list-first order never reached
    the POST that would create one. The register call must come FIRST, and an
    empty list after it is still a clean fall-through."""
    order = []

    class Cloud(FakeCredentials):
        async def ensure_registered(self):
            order.append("register"); return 1

        async def held(self, ctype):
            order.append("list"); return await super().held(ctype)

    with caplog.at_level(logging.INFO):
        assert await cr.reconcile_at_boot(credentials=Cloud(listed=[]), agent_dir=tmp_path, harness="codex", agent_id="a1") is False
    assert order == ["register", "list"]
    assert "credential key registered (v1)" in caplog.text and "server lists no" in caplog.text

    class Broken(Cloud):
        async def ensure_registered(self):
            raise RuntimeError("down")

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert await cr.reconcile_at_boot(credentials=Broken(listed=[]), agent_dir=tmp_path, harness="codex", agent_id="a1") is False
    assert "key registration unavailable" in caplog.text  # fail-open, still lists
    # a native reader has no ensure_registered: untouched
    assert await cr.reconcile_at_boot(credentials=FakeCredentials(listed=[]), agent_dir=tmp_path, harness="codex") is False


@pytest.mark.asyncio
async def test_boot_reconcile_fails_open(tmp_path):
    assert await cr.reconcile_at_boot(credentials=FakeCredentials(list_error=RuntimeError("down")), agent_dir=tmp_path, harness="codex") is False
    assert await cr.reconcile_at_boot(credentials=FakeCredentials(listed=[]), agent_dir=tmp_path, harness="codex") is False
    assert await cr.reconcile_at_boot(credentials=None, agent_dir=tmp_path, harness="codex") is False
    assert await cr.reconcile_at_boot(credentials=FakeCredentials(listed=[0], held=_held()), agent_dir=tmp_path, harness="acp") is False
    assert cr.read_store(tmp_path) == {}


# ── subscription_token prefers the store, then the environment ───────────────


def test_subscription_token_prefers_the_placed_store(tmp_path, monkeypatch):
    from puffo_agent.portal import state

    monkeypatch.setattr(state, "agent_dir", lambda agent_id: tmp_path / agent_id)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-the-vault")
    monkeypatch.setenv("CODEX_SUBSCRIPTION_AUTH_JSON", '{"vault":1}')
    assert state.subscription_token(None, "claude-code", agent_id="a1") == "from-the-vault"
    (tmp_path / "a1").mkdir()
    cr.place(agent_dir=tmp_path / "a1", credential_type=cr.TYPE_CLAUDE_TOKEN, index=0, version=1, value=b"from-the-owner")
    assert state.subscription_token(None, "claude-code", agent_id="a1") == "from-the-owner"
    # codex reads the other type, untouched here, so still the env
    assert state.subscription_token(None, "codex", agent_id="a1") == '{"vault":1}'
    # no agent_id: the legacy behaviour, byte for byte
    assert state.subscription_token(None, "claude-code") == "from-the-vault"


# ── the three KEM derivations must agree (server, web, daemon) ───────────────


@pytest.mark.parametrize(
    "version, okm_hex",
    [
        (1, "66710061a90e3cf8e37f69b8796f74f298da132458a4a2b56107920fc8e63cac"),
        (2, "371b1db3167ec41dfad34109f6a709898c5e4b651ab18deb72a2814f7d1cf4e8"),
    ],
)
def test_kem_derivation_matches_the_server_and_web_known_answers(version, okm_hex):
    """puffo-server #437 and the web pin these for ikm 00..1f. A daemon that
    derived differently would open NO credential and nothing would fail loudly;
    for a cloud agent the server hands this very key over, so agreement is
    what makes the hand-over meaningful."""
    from puffo_agent.crypto.credential_keys import derive_credential_kem_keypair
    from puffo_agent.crypto.primitives import KemKeyPair

    ikm = bytes(range(32))
    expected = KemKeyPair.from_secret_bytes(bytes.fromhex(okm_hex)).public_key_bytes()
    assert derive_credential_kem_keypair(ikm, version).public_key_bytes() == expected


# ── the arrival seam: a notice turn admits nothing, so act when the row LANDS ──
#
# Found live on staging (codex4, 2026-10-06 01:28Z): the owner's filed reply was
# stored correctly, the turn was admitted with message_count 0, and the row joined
# the turn only when the model called read_inbox — after the turn-start scan had
# run on an empty batch. The daemon never acted. These tests go through the REAL
# MessageStore with the hook attached and NOTHING else (no turn scan, no sweep),
# so removing the hook fails them and nothing can mask its absence.

import asyncio

import pytest_asyncio

from puffo_agent.agent.message_store import MessageStore
from puffo_agent.agent.message_store_models import ReceiptDisposition, StoredMessage


async def _settle_tasks(store):
    """Join the store's observer tasks — a real aiosqlite round-trip, not a tick."""
    for _ in range(3):
        await asyncio.sleep(0)
        if store.observer_tasks:
            await asyncio.gather(*list(store.observer_tasks), return_exceptions=True)


def _dm_payload(envelope_id, seq, sender, text, *, root=None):
    return {
        "envelope_id": envelope_id, "envelope_kind": "dm", "sender_slug": sender,
        "recipient_slug": "bot", "channel_id": None, "space_id": None,
        "content": {"text": text}, "content_type": "text/plain", "sent_at": seq,
        "is_encrypted": True, "thread_root_id": root,
    }


@pytest_asyncio.fixture
async def real_store(tmp_path):
    store = MessageStore(tmp_path / "messages.db")
    await store.open()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_a_filed_reply_is_placed_on_arrival_with_no_turn_at_all(env, real_store, caplog):
    """The notice-turn case, end to end through the store: nothing admits the
    row to a turn; the hook alone must place it."""
    creds = FakeCredentials(listed=[1], held=_held(index=1))
    seen = []

    async def on_stored(row):
        assert isinstance(row, StoredMessage)  # the store's own type, never a duck
        seen.append(row.envelope_id)
        await cr.handle_filed_replies([row], owner_slug=OWNER, ledger=env.ledger, credentials=creds,
                                      agent_dir=env.agent_dir, workspace=env.workspace, harness="claude-code", agent_id="a1")

    real_store.on_receipt_stored = on_stored
    with caplog.at_level(logging.INFO):
        res = await real_store.store_receipt(
            _dm_payload("msg_filed", 10, OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN, index=1), root="msg_card"),
            server_seq=10, disposition=ReceiptDisposition.ELIGIBLE, reason="test",
        )
        await _settle_tasks(real_store)
    assert res.status.value == "committed" and seen == ["msg_filed"]
    assert env.ledger.get(env.req.request_id).state == "placed"
    assert cr.stored_value(env.agent_dir, cr.TYPE_CLAUDE_TOKEN) == SECRET.decode()
    assert SECRET.decode() not in caplog.text and cr.fingerprint(SECRET) in caplog.text


@pytest.mark.asyncio
async def test_the_hook_fires_once_per_committed_receipt_and_never_raises_into_delivery(real_store):
    calls = []

    async def boom(row):
        calls.append(row.envelope_id); raise RuntimeError("observer bug")

    real_store.on_receipt_stored = boom
    p = _dm_payload("msg_1", 1, OWNER, "hi")
    r1 = await real_store.store_receipt(p, server_seq=1, disposition=ReceiptDisposition.ELIGIBLE, reason="t")
    r2 = await real_store.store_receipt(p, server_seq=1, disposition=ReceiptDisposition.ELIGIBLE, reason="t")  # replay
    await _settle_tasks(real_store)
    assert r1.status.value == "committed" and r2.status.value != "committed"
    assert calls == ["msg_1"]  # once, not on the idempotent replay; the raise stayed inside the task


@pytest.mark.asyncio
async def test_claim_makes_consume_once_a_race_safe_claim_not_a_check(env):
    """Three paths can see one reply. The guard is a synchronous claim before the
    first await, so of two racing handlers exactly one places."""
    gate = asyncio.Event()

    class Slow(FakeCredentials):
        async def held(self, ctype):
            await gate.wait(); return await super().held(ctype)

    creds = Slow(listed=[0], held=_held())
    item = _dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN))
    a = asyncio.create_task(_run(env, [item], creds))
    b = asyncio.create_task(_run(env, [item], creds))
    await asyncio.sleep(0); await asyncio.sleep(0)
    assert env.ledger.get(env.req.request_id).state == "in_flight"
    assert cr.status_line(env.ledger.get(env.req.request_id)).startswith("placing")
    gate.set()
    ra, rb = await a, await b
    # the winner places; the loser either saw in_flight ("ignored") or, having
    # started after the claim, saw nothing pending and did no work at all
    states = sorted(o.state for o in ra + rb)
    assert states in (["ignored", "placed"], ["placed"])
    assert creds.get_calls == [(cr.TYPE_CLAUDE_TOKEN, 0)]  # ONE fetch, one placement


def test_failed_is_terminal_and_pending_is_never_restored(tmp_path):
    ledger = cr.RequestLedger(tmp_path / "l.json")
    req = ledger.issue(credential_type=cr.TYPE_CHATGPT, reason="r")
    assert ledger.claim(req.request_id) is True
    assert ledger.claim(req.request_id) is False  # the loser
    ledger.release(req.request_id)  # a release only moves in_flight → pending
    assert ledger.get(req.request_id).state == "pending"
    assert ledger.claim(req.request_id) is True
    ledger.settle(req.request_id, state="failed", detail="x")
    assert ledger.get(req.request_id).state == "failed"
    assert ledger.claim(req.request_id) is False
    ledger.release(req.request_id)  # failed is terminal: release does nothing
    assert ledger.get(req.request_id).state == "failed"
    with pytest.raises(AssertionError):
        ledger.settle(req.request_id, state="pending")


@pytest.mark.asyncio
async def test_an_unexpected_row_type_is_logged_not_silently_skipped(env, caplog):
    with caplog.at_level(logging.WARNING):
        out = await _run(env, ["not a row", 42], FakeCredentials(listed=[0], held=_held()))
    assert out == [] and "unexpected row type str" in caplog.text


# ── the boot sweep ───────────────────────────────────────────────────────────


class FakeStore:
    """Newest-first keyset paging like the real store (before_envelope_id)."""

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: -r.sent_at); self.calls = 0  # newest first, like the SELECT
        self.pages: list[set] = []

    async def get_dm_history(self, peer, limit=50, before_envelope_id=None):
        """Mirrors production: SELECT sent_at DESC, keyset on before_envelope_id,
        then the page is returned OLDEST-FIRST (message_store.py selected_oldest_first)."""
        self.calls += 1
        rows = self.rows
        if before_envelope_id is not None:
            idx = next(i for i, r in enumerate(rows) if r.envelope_id == before_envelope_id)
            rows = rows[idx + 1:]
        page = list(reversed(rows[:limit]))
        self.pages.append({r.envelope_id for r in page})
        return page


_rown = [0]


def _row(sender, text, received_at_ms, kind="dm"):
    _rown[0] += 1
    return SimpleNamespace(envelope_id=f"msg_{_rown[0]}", envelope_kind=kind, sender_slug=sender,
                           content={"text": text}, received_at=received_at_ms, sent_at=received_at_ms - 5)


async def _sweep(env, store, creds):
    return await cr.sweep_stored_replies(store, owner_slug=OWNER, ledger=env.ledger, credentials=creds,
                                         agent_dir=env.agent_dir, workspace=env.workspace, harness="claude-code", agent_id="a1")


@pytest.mark.asyncio
async def test_sweep_places_a_reply_stored_before_boot_and_is_idempotent(env):
    now_ms = int(time.time() * 1000)
    creds = FakeCredentials(listed=[0], held=_held())
    store = FakeStore([_row(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN), now_ms)])
    out = await _sweep(env, store, creds)
    assert [o.state for o in out] == ["placed"] and store.calls == 1
    # second boot: nothing pending → no query at all
    assert await _sweep(env, store, creds) == [] and store.calls == 1
    assert creds.get_calls == [(cr.TYPE_CLAUDE_TOKEN, 0)]


@pytest.mark.asyncio
async def test_sweep_is_bounded_to_dms_newer_than_the_oldest_pending_request(env):
    """A stale DM carrying a valid-looking fence for a current request id must not
    be acted on: only rows that arrived after the request was issued can answer it."""
    old_ms = (env.req.issued_at - 3600) * 1000
    creds = FakeCredentials(listed=[0], held=_held())
    store = FakeStore([_row(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN), old_ms)])
    assert await _sweep(env, store, creds) == []
    assert creds.get_calls == [] and env.ledger.get(env.req.request_id).state == "pending"


@pytest.mark.asyncio
async def test_sweep_finds_a_receipt_buried_under_newer_owner_chatter(env):
    """limit truncates BEFORE the floor filter; a chatty owner must not push the
    receipt out of the first page. The sweep pages back until the floor."""
    now_ms = int(time.time() * 1000)
    receipt = _row(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN), now_ms)
    chatter = [_row(OWNER, f"hi {i}", now_ms + 1000 + i) for i in range(450)]  # newer than the receipt
    creds = FakeCredentials(listed=[0], held=_held())
    store = FakeStore(chatter + [receipt])
    out = await _sweep(env, store, creds)
    assert [o.state for o in out] == ["placed"]
    assert store.calls == 3  # two full pages of 200, then the page holding the receipt
    # consecutive pages are disjoint: the cursor moved a whole page, not one row
    for a, b in zip(store.pages, store.pages[1:]):
        assert not (a & b)


@pytest.mark.asyncio
async def test_sweep_warns_instead_of_silently_giving_up_when_the_cap_is_hit(env, caplog):
    now_ms = int(time.time() * 1000)
    creds = FakeCredentials(listed=[0], held=_held())
    store = FakeStore([_row(OWNER, f"hi {i}", now_ms + 1000 + i) for i in range(5000)])  # all newer than the floor
    with caplog.at_level(logging.WARNING):
        assert await _sweep(env, store, creds) == []
    assert "without reaching the time floor" in caplog.text and "4000 owner DMs" in caplog.text


@pytest.mark.asyncio
async def test_a_stranded_in_flight_claim_is_recovered_at_boot_and_placed(env):
    """A death between claim and settle must not strand the request forever."""
    assert env.ledger.claim(env.req.request_id) is True
    # "the process died here" — a fresh process reloads the ledger from disk
    reloaded = cr.RequestLedger(env.agent_dir / "credential_requests.json")
    assert reloaded.get(env.req.request_id).state == "in_flight"
    assert reloaded.claim(env.req.request_id) is False  # stranded: refuses forever without recovery
    assert reloaded.recover_in_flight() == 1
    env.ledger = reloaded
    creds = FakeCredentials(listed=[0], held=_held())
    out = await _sweep(env, FakeStore([_row(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN), int(time.time() * 1000))]), creds)
    assert [o.state for o in out] == ["placed"]


@pytest.mark.asyncio
async def test_owner_dms_cost_nothing_when_no_request_is_open(tmp_path):
    ledger = cr.RequestLedger(tmp_path / "l.json")  # nothing pending
    creds = FakeCredentials(listed=[0], held=_held())
    out = await cr.handle_filed_replies([_dm(OWNER, _filed(str(uuid.uuid4()), cr.TYPE_CLAUDE_TOKEN))],
                                        owner_slug=OWNER, ledger=ledger, credentials=creds, agent_dir=tmp_path,
                                        workspace=tmp_path, harness="claude-code")
    assert out == [] and creds.held_calls == []


@pytest.mark.asyncio
async def test_sweep_fails_open_when_the_store_cannot_be_read(env):
    class Broken:
        async def get_dm_history(self, *a, **k): raise RuntimeError("db")
    assert await _sweep(env, Broken(), FakeCredentials(listed=[0], held=_held())) == []
    assert env.ledger.get(env.req.request_id).state == "pending"


# ── the file, not the object, is the ledger ──────────────────────────────────
#
# Staging 2026-10-06, Desk: the worker cached a RequestLedger built at BOOT
# (file absent → empty). The MCP tool handler built its OWN instance at
# 02:15:30Z and issued b081f515. The receipt arrived at 02:16:35Z, the arrival
# hook asked the worker's cached ledger, saw nothing pending, and returned
# silently. Nothing was placed and nothing was logged.


@pytest.mark.asyncio
async def test_a_request_issued_by_another_instance_is_seen_by_the_handler(tmp_path, caplog, monkeypatch):
    """The exact staging interleaving: the worker takes its ledger at BOOT (file
    absent), the tool handler issues later, the receipt arrives. Both must be the
    same instance or the receipt is dropped."""
    monkeypatch.setattr(cr, "_LEDGERS", {})
    path = tmp_path / "credential_requests.json"
    worker_side = cr.ledger_for(path)             # taken at boot, file absent
    assert worker_side.pending() == []
    tool_side = cr.ledger_for(path)               # the MCP tool handler's
    req = tool_side.issue(credential_type=cr.TYPE_CUSTOMIZED, reason="r", alias="YOUTUBE_API_KEY")

    creds = FakeCredentials(listed=[0], held=_held(ctype=cr.TYPE_CUSTOMIZED))
    with caplog.at_level(logging.INFO):
        out = await cr.handle_filed_replies(
            [_dm(OWNER, _filed(req.request_id, cr.TYPE_CUSTOMIZED))],
            owner_slug=OWNER, ledger=worker_side, credentials=creds,
            agent_dir=tmp_path, workspace=tmp_path, harness="claude-code", agent_id="a1",
        )
    assert [o.state for o in out] == ["placed"], "the worker's ledger must see the tool's request"
    assert worker_side.get(req.request_id).state == "placed"
    assert cr.RequestLedger(path).get(req.request_id).state == "placed"  # and it is on disk
    assert cr.customized_env(tmp_path) == {"YOUTUBE_API_KEY": SECRET.decode()}
    assert "placed" in caplog.text and SECRET.decode() not in caplog.text


def test_two_handles_to_one_file_cannot_both_claim(tmp_path, monkeypatch):
    """The race reload-on-read would have reopened. claim() is atomic only
    because ONE instance does check → flip → save; two instances that each hold
    the pending row can both pass. ledger_for keeps it one instance."""
    monkeypatch.setattr(cr, "_LEDGERS", {})
    path = tmp_path / "l.json"
    tool = cr.ledger_for(path)
    req = tool.issue(credential_type=cr.TYPE_CUSTOMIZED, reason="r", alias="X")
    # the arrival hook and the boot sweep, both obtained AFTER the request exists
    h1, h2 = cr.ledger_for(path), cr.ledger_for(path)
    assert h1 is h2 is tool, "one ledger per file per process"
    assert sorted([h1.claim(req.request_id), h2.claim(req.request_id)]) == [False, True]
    assert cr.RequestLedger(path).get(req.request_id).state == "in_flight"


def test_ledger_for_is_per_resolved_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "_LEDGERS", {})
    a = cr.ledger_for(tmp_path / "l.json")
    b = cr.ledger_for(tmp_path / "sub" / ".." / "l.json")  # same file, different spelling
    assert a is b
    assert cr.ledger_for(tmp_path / "other.json") is not a


@pytest.mark.asyncio
async def test_a_filed_fence_with_nothing_pending_logs_the_signature(tmp_path, caplog):
    """The impossible combination: an owner DM carrying a filed fence while the
    ledger reports nothing pending. That is this bug's one-line signature, so the
    fast exit must not be silent."""
    rid = str(uuid.uuid4())
    with caplog.at_level(logging.WARNING):
        out = await cr.handle_filed_replies(
            [_dm(OWNER, _filed(rid, cr.TYPE_CUSTOMIZED))],
            owner_slug=OWNER, ledger=cr.RequestLedger(tmp_path / "empty.json"),
            credentials=FakeCredentials(), agent_dir=tmp_path, workspace=tmp_path,
            harness="claude-code", agent_id="a1",
        )
    assert out == []
    assert "nothing pending" in caplog.text and rid in caplog.text and "ledger_for" in caplog.text


def test_the_pending_status_never_invites_a_resubmission():
    """Twice on staging an agent read "pending" as "the filing failed" and asked
    the owner to submit again. The text must forbid that explicitly."""
    pending = cr.CredentialRequest(request_id="x", type=cr.TYPE_CHATGPT, reason="r")
    line = cr.status_line(pending)
    assert line.startswith("pending: waiting for the owner")
    assert "may already have filed" in line and "does NOT mean it failed" in line
    assert "do NOT ask them to submit it again" in line
    in_flight = cr.status_line(cr.CredentialRequest(request_id="x", type=cr.TYPE_CHATGPT, reason="r", state="in_flight"))
    assert in_flight.startswith("placing") and "HAS already filed" in in_flight
    assert "Do NOT ask them to submit it again" in in_flight
