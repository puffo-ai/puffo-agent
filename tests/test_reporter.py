"""Unit tests for the agent.status reporter (resolve owner → seal → send)."""

from __future__ import annotations

import json

import pytest

from puffo_agent.crypto.encoding import base64url_decode, base64url_encode
from puffo_agent.crypto.primitives import (
    Ed25519KeyPair,
    KemKeyPair,
    aead_decrypt,
    hpke_open,
)
from puffo_agent.portal.control import agent_message
from puffo_agent.portal.control import reporter as reporter_mod
from puffo_agent.portal.control.store import MachineControlIdentity


def _machine() -> MachineControlIdentity:
    s = Ed25519KeyPair.generate()
    k = KemKeyPair.generate()
    return MachineControlIdentity(
        "mac_test", base64url_encode(s.secret_bytes()), base64url_encode(k.secret_bytes())
    )


class _Cfg:
    def __init__(self, op, harness="claude-code"):
        self.puffo_core = type("PC", (), {"operator_slug": op})()
        self.runtime = type("RT", (), {"harness": harness})()


class _Pairing:
    server_url = "http://localhost:3000"
    operator_root_pubkey = "oproot"


def _wire(monkeypatch, *, owner="op-1", pairings=None, recipients=None):
    monkeypatch.setattr(reporter_mod.AgentConfig, "load", lambda slug: _Cfg(owner))
    monkeypatch.setattr(
        reporter_mod, "load_pairings",
        lambda: {"op-1": _Pairing()} if pairings is None else pairings,
    )
    monkeypatch.setattr(reporter_mod, "load_or_create_machine", _machine)

    async def _fetch(*a, **k):
        return recipients if recipients is not None else []

    monkeypatch.setattr(agent_message, "fetch_active_recipients", _fetch)


@pytest.mark.asyncio
async def test_emit_seals_and_sends_to_owner(monkeypatch):
    dev_kem = KemKeyPair.generate()
    recip = agent_message.Recipient("dev_1", dev_kem.public_key_bytes())
    _wire(monkeypatch, recipients=[recip])

    r = reporter_mod.AgentStatusReporter()
    captured = {}

    async def sender(op, env):
        captured["op"], captured["env"] = op, env

    r.set_sender(sender)
    await r.emit("scout-1", "turn_complete", {"tokens": {"input": 5, "output": 7}})

    assert captured["op"] == "op-1"
    env = captured["env"]
    assert env["machine_id"] == "mac_test"
    assert len(env["recipients"]) == 1

    # The owner's device decrypts the agent.status payload.
    mid = env["message_id"]
    entry = env["recipients"][0]
    ck = hpke_open(
        dev_kem,
        base64url_decode(entry["hpke_enc"]),
        agent_message.MACHINE_MSG_HPKE_INFO,
        f"{mid}:dev_1".encode(),
        base64url_decode(entry["wrapped_content_key"]),
    )
    payload = json.loads(
        aead_decrypt(ck, base64url_decode(env["nonce"]), base64url_decode(env["ciphertext"]), mid.encode())
    )
    assert payload == {
        "type": "agent.status",
        "agent_slug": "scout-1",
        "event": "turn_complete",
        "payload": {"tokens": {"input": 5, "output": 7}},
    }


@pytest.mark.asyncio
async def test_emit_noop_without_sender(monkeypatch):
    _wire(monkeypatch, recipients=[agent_message.Recipient("d", KemKeyPair.generate().public_key_bytes())])
    r = reporter_mod.AgentStatusReporter()
    # No sender registered (WS down) → must not raise, must not fetch/send.
    await r.emit("scout-1", "turn_complete", {})


@pytest.mark.asyncio
async def test_emit_noop_when_owner_not_linked(monkeypatch):
    _wire(monkeypatch, pairings={})  # no pairing for the owner
    r = reporter_mod.AgentStatusReporter()
    sent = []
    r.set_sender(lambda op, env: sent.append(op))
    await r.emit("scout-1", "turn_complete", {})
    assert sent == []


# ── PUF-364: per-harness token accumulation ────────────────────────


def _harness_map(monkeypatch, mapping):
    monkeypatch.setattr(
        reporter_mod.AgentConfig,
        "load",
        lambda slug: _Cfg("op-1", harness=mapping.get(slug, "claude-code")),
    )


def test_record_usage_accumulates_per_harness(monkeypatch):
    _harness_map(monkeypatch, {"cc-agent": "claude-code", "cx-agent": "codex"})
    r = reporter_mod.AgentStatusReporter()
    r.record_turn_usage("cc-agent", 10, 3)
    r.record_turn_usage("cc-agent", 5, 2)
    r.record_turn_usage("cx-agent", 100, 40)
    assert r.snapshot_usage() == {"claude-code": (15, 5), "codex": (100, 40)}


def test_snapshot_hides_zero_and_ignores_nonpositive(monkeypatch):
    _harness_map(monkeypatch, {"a": "claude-code"})
    r = reporter_mod.AgentStatusReporter()
    r.record_turn_usage("a", 0, 0)    # no-op
    r.record_turn_usage("a", -5, -1)  # no-op — server rejects negative deltas
    assert r.snapshot_usage() == {}
    r.record_turn_usage("a", 7, 0)
    assert r.snapshot_usage() == {"claude-code": (7, 0)}


def test_commit_usage_subtracts_only_sent_delta(monkeypatch):
    # A turn accruing DURING the in-flight POST is preserved: snapshot is
    # taken, more usage lands, commit subtracts only the snapshot.
    _harness_map(monkeypatch, {"a": "codex"})
    r = reporter_mod.AgentStatusReporter()
    r.record_turn_usage("a", 20, 8)
    snap = r.snapshot_usage()
    r.record_turn_usage("a", 5, 2)  # lands mid-POST
    r.commit_usage_sent(snap)
    assert r.snapshot_usage() == {"codex": (5, 2)}


def test_harness_lookup_is_cached(monkeypatch):
    calls = []

    def _load(slug):
        calls.append(slug)
        return _Cfg("op-1", harness="claude-code")

    monkeypatch.setattr(reporter_mod.AgentConfig, "load", _load)
    r = reporter_mod.AgentStatusReporter()
    r.record_turn_usage("a", 1, 1)
    r.record_turn_usage("a", 1, 1)
    assert calls == ["a"]  # loaded once, cached thereafter


def test_record_usage_noop_when_config_unreadable(monkeypatch):
    def _boom(slug):
        raise RuntimeError("no config")

    monkeypatch.setattr(reporter_mod.AgentConfig, "load", _boom)
    r = reporter_mod.AgentStatusReporter()
    r.record_turn_usage("a", 9, 9)  # must not raise
    assert r.snapshot_usage() == {}
