from __future__ import annotations

from types import SimpleNamespace

from puffo_agent.portal.control import ownership


def _wire(monkeypatch, *, slug="agent-1", identity_json='{"declared_operator_public_key":"op"}'):
    monkeypatch.setattr(
        ownership.AgentConfig,
        "load",
        lambda _agent_id: SimpleNamespace(puffo_core=SimpleNamespace(slug=slug)),
    )
    store = SimpleNamespace(
        load_identity=lambda _slug: SimpleNamespace(identity_cert_json=identity_json)
    )
    monkeypatch.setattr(ownership.KeyStore, "for_agent", lambda _agent_id: store)


def test_owner_key_and_comparison(monkeypatch):
    _wire(monkeypatch)
    assert ownership.agent_owner_root_pubkey("agent-1") == "op"
    assert ownership.is_owner("agent-1", "op") is True
    assert ownership.is_owner("agent-1", "other") is False


def test_missing_or_invalid_owner_material_returns_none(monkeypatch):
    monkeypatch.setattr(
        ownership.AgentConfig,
        "load",
        lambda _agent_id: (_ for _ in ()).throw(ValueError("bad config")),
    )
    assert ownership.agent_owner_root_pubkey("agent-1") is None

    _wire(monkeypatch, slug="")
    assert ownership.agent_owner_root_pubkey("agent-1") is None

    _wire(monkeypatch)
    monkeypatch.setattr(
        ownership.KeyStore,
        "for_agent",
        lambda _agent_id: SimpleNamespace(
            load_identity=lambda _slug: (_ for _ in ()).throw(FileNotFoundError())
        ),
    )
    assert ownership.agent_owner_root_pubkey("agent-1") is None

    _wire(monkeypatch, identity_json="not-json")
    assert ownership.agent_owner_root_pubkey("agent-1") is None


def test_non_object_empty_and_non_string_owner_are_absent(monkeypatch):
    for identity_json in ("[]", "{}", '{"declared_operator_public_key": 3}'):
        _wire(monkeypatch, identity_json=identity_json)
        assert ownership.agent_owner_root_pubkey("agent-1") is None
