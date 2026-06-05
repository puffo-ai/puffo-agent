"""Handshake authentication by ``.puffoagent`` decryption.

``authenticate_bundle`` delegates to ``export.unpack`` (round-trip of
the real scrypt+AES-GCM format is covered by the export suite), so here
we drive its branches: a single-agent manifest passes; a bad password /
corrupt blob, a non-single-agent manifest, and a manifest missing
slug/id are each rejected as ``AuthError``.
"""

from __future__ import annotations

import pytest

from puffo_agent.portal.export import ImportPackError, UnpackedBundle
from puffo_agent.portal.ws_local import auth
from puffo_agent.portal.ws_local.auth import (
    AuthedAgent,
    AuthError,
    authenticate_bundle,
)


def _bundle(agents: list[dict]) -> UnpackedBundle:
    return UnpackedBundle(manifest={"agents": agents}, agents={})


def test_valid_single_agent_export_authenticates(monkeypatch):
    monkeypatch.setattr(auth, "unpack", lambda blob, pw: _bundle([
        {"id": "puffotest-19b1", "slug": "puffotest", "display_name": "Puffo Test"},
    ]))
    result = authenticate_bundle(b"blob", "pw")
    assert result == AuthedAgent("puffotest-19b1", "puffotest", "Puffo Test")


def test_wrong_password_is_auth_error(monkeypatch):
    def boom(blob, pw):
        raise ImportPackError("decryption failed (wrong password or corrupted archive)")

    monkeypatch.setattr(auth, "unpack", boom)
    with pytest.raises(AuthError, match="decryption failed"):
        authenticate_bundle(b"blob", "nope")


def test_multi_agent_export_rejected(monkeypatch):
    monkeypatch.setattr(auth, "unpack", lambda blob, pw: _bundle([
        {"id": "a1", "slug": "a", "display_name": "A"},
        {"id": "b2", "slug": "b", "display_name": "B"},
    ]))
    with pytest.raises(AuthError, match="single-agent"):
        authenticate_bundle(b"blob", "pw")


def test_empty_manifest_rejected(monkeypatch):
    monkeypatch.setattr(auth, "unpack", lambda blob, pw: _bundle([]))
    with pytest.raises(AuthError, match="single-agent"):
        authenticate_bundle(b"blob", "pw")


@pytest.mark.parametrize("entry", [
    {"id": "a1", "slug": "", "display_name": "A"},   # no slug
    {"id": "", "slug": "a", "display_name": "A"},     # no id
])
def test_manifest_missing_slug_or_id_rejected(monkeypatch, entry):
    monkeypatch.setattr(auth, "unpack", lambda blob, pw: _bundle([entry]))
    with pytest.raises(AuthError, match="missing slug/id"):
        authenticate_bundle(b"blob", "pw")


def test_display_name_optional(monkeypatch):
    monkeypatch.setattr(auth, "unpack", lambda blob, pw: _bundle([
        {"id": "a1", "slug": "a"},
    ]))
    assert authenticate_bundle(b"blob", "pw").display_name == ""
