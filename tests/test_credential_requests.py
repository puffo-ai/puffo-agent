"""Agentic credential setup: the DM contract, the ledger, placement, and the
untrusted filed reply (``portal/credential_requests``).

The property every test here guards: a fetched value reaches its destination
and NOTHING else — not a tool result, not a message, not a log line."""

from __future__ import annotations

import json
import logging
import os
import stat
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
    # replay: consumed once
    out2 = await _run(env, [_dm(OWNER, _filed(env.req.request_id, cr.TYPE_CLAUDE_TOKEN))], creds)
    assert out2[0].state == "ignored" and len(creds.get_calls) == 1


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
