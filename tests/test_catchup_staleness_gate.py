"""Catch-up staleness gate.

On WS reconnect / daemon restart / resume-from-pause the server
redelivers backlog through ``handle_envelope``. Messages older than
``catchup_stale_hours`` are stored to chat history and reported
processed server-side, but skip the LLM pipeline.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import puffo_agent.portal.state as state
from puffo_agent.agent.puffo_core_client import (
    DEFAULT_CATCHUP_STALE_HOURS,
    PuffoCoreMessageClient,
)

_48H_MS = 48 * 3600 * 1000
_NOW = 1_000_000_000_000


def _client(catchup_stale_ms: int) -> PuffoCoreMessageClient:
    """Bare client — just enough to exercise the predicate."""
    c = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    c._catchup_stale_ms = catchup_stale_ms
    return c


def test_stale_message_is_gated():
    assert _client(_48H_MS)._is_stale_for_catchup(_NOW - 49 * 3600 * 1000, _NOW) is True


def test_fresh_message_passes():
    assert _client(_48H_MS)._is_stale_for_catchup(_NOW - 1000, _NOW) is False


def test_exact_boundary_is_not_stale():
    c = _client(_48H_MS)
    # sent_at == now - threshold is the boundary; strict ``<`` keeps it live.
    assert c._is_stale_for_catchup(_NOW - _48H_MS, _NOW) is False
    # one ms older tips it over.
    assert c._is_stale_for_catchup(_NOW - _48H_MS - 1, _NOW) is True


def test_zero_threshold_disables_gate():
    assert _client(0)._is_stale_for_catchup(0, _NOW) is False


def test_negative_threshold_disables_gate():
    assert _client(-1)._is_stale_for_catchup(0, _NOW) is False


def test_now_ms_defaults_to_wall_clock():
    # Epoch-0 is always stale under the real clock (now_ms=None branch).
    assert _client(_48H_MS)._is_stale_for_catchup(0) is True


@pytest.mark.asyncio
async def test_stale_receipt_is_reported_and_committed_terminal():
    from puffo_agent.agent.inbound_receipts import InboundReceiptHandler
    from puffo_agent.agent.message_store import ReceiptDisposition
    from puffo_agent.crypto.ws_client import TransportOutcome

    client = _client(_48H_MS)
    client._log = logging.getLogger("staleness-test")
    trace: list[tuple[str, object]] = []
    commit_outcome = TransportOutcome.ACK

    async def _report(message_id):
        trace.append(("report", message_id))

    client._report_stale_processed = _report

    async def _commit(disposition, reason):
        trace.append(("commit", (disposition, reason)))
        return commit_outcome

    committer = SimpleNamespace(
        payload=SimpleNamespace(
            envelope_id="msg_old", sent_at=_NOW - 49 * 3600 * 1000,
        ),
        stored_payload={"thread_root_id": ""},
        commit=_commit,
    )
    handler = InboundReceiptHandler.__new__(InboundReceiptHandler)
    handler.client = client

    assert await handler._stale_outcome(committer) is TransportOutcome.ACK
    assert trace == [
        ("commit", (ReceiptDisposition.TERMINAL, "stale catch-up")),
        ("report", "msg_old"),
    ]

    trace.clear()
    commit_outcome = TransportOutcome.HOLD
    assert await handler._stale_outcome(committer) is TransportOutcome.HOLD
    assert trace == [("commit", (ReceiptDisposition.TERMINAL, "stale catch-up"))]


def _init_client(catchup_stale_hours: float) -> PuffoCoreMessageClient:
    # Real __init__ with inert deps — exercises the float→ms computation.
    return PuffoCoreMessageClient(
        slug="t", device_id="d", space_id="s",
        keystore=None, http_client=None, message_store=None,
        catchup_stale_hours=catchup_stale_hours,
    )


def test_init_computes_ms_from_fractional_hours():
    # 0.5h = 1_800_000 ms; the float→int conversion is exercised.
    assert _init_client(0.5)._catchup_stale_ms == 1_800_000
    assert _init_client(48.0)._catchup_stale_ms == _48H_MS


def test_init_sub_millisecond_threshold_truncates_to_disabled():
    # 1e-7 h = 0.36 ms → int truncates to 0 → gate disabled.
    c = _init_client(1e-7)
    assert c._catchup_stale_ms == 0
    assert c._is_stale_for_catchup(0, _NOW) is False


def test_daemon_config_default_is_48h():
    assert state.DaemonConfig().catchup_stale_hours == DEFAULT_CATCHUP_STALE_HOURS
    assert DEFAULT_CATCHUP_STALE_HOURS == 48.0


def test_daemon_config_round_trip(tmp_path, monkeypatch):
    cfg_path = tmp_path / "daemon.yml"
    monkeypatch.setattr(state, "daemon_yml_path", lambda: cfg_path)
    state.DaemonConfig(catchup_stale_hours=12.5).save()
    assert state.DaemonConfig.load().catchup_stale_hours == 12.5


def test_daemon_config_absent_key_defaults(tmp_path, monkeypatch):
    # An older daemon.yml without the key still loads at the 48h default.
    cfg_path = tmp_path / "daemon.yml"
    cfg_path.write_text("default_provider: anthropic\n", encoding="utf-8")
    monkeypatch.setattr(state, "daemon_yml_path", lambda: cfg_path)
    assert state.DaemonConfig.load().catchup_stale_hours == 48.0


def _build_via_worker(monkeypatch, tmp_path, daemon_cfg):
    """Build via the worker with stubbed deps; returns ctor kwargs."""
    from puffo_agent.portal import worker
    from puffo_agent.portal.state import AgentConfig, PuffoCoreConfig, RuntimeConfig

    cfg = AgentConfig(
        id="agent-test-1234",
        puffo_core=PuffoCoreConfig(
            server_url="https://example.test", slug="agent-test-1234",
            device_id="dev-1", space_id="", operator_slug="",
        ),
        runtime=RuntimeConfig(kind="chat-local", harness="claude-code"),
    )
    monkeypatch.setattr(
        worker, "_ensure_agent_identity_imported", lambda *_a, **_k: None,
    )
    captured: dict[str, object] = {}

    class DummyClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "puffo_agent.agent.puffo_core_client.PuffoCoreMessageClient", DummyClient,
    )
    monkeypatch.setattr(
        "puffo_agent.crypto.keystore.KeyStore", lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        "puffo_agent.crypto.http_client.PuffoCoreHttpClient",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        "puffo_agent.agent.message_store.MessageStore", lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(
        AgentConfig, "resolve_workspace_dir", lambda self: tmp_path,
    )
    worker._build_puffo_core_client(cfg, "agent-test-1234", daemon_cfg=daemon_cfg)
    return captured


def test_worker_threads_catchup_stale_hours(monkeypatch, tmp_path):
    captured = _build_via_worker(
        monkeypatch, tmp_path, state.DaemonConfig(catchup_stale_hours=12.5),
    )
    assert captured.get("catchup_stale_hours") == 12.5


def test_worker_defaults_catchup_stale_hours_without_daemon_cfg(monkeypatch, tmp_path):
    captured = _build_via_worker(monkeypatch, tmp_path, None)
    assert captured.get("catchup_stale_hours") == DEFAULT_CATCHUP_STALE_HOURS
