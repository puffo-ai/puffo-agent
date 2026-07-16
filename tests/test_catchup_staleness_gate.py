"""PUF-384: catch-up staleness gate.

On WS reconnect / daemon restart / resume-from-pause the server
redelivers backlog through ``handle_envelope``. Messages older than
``catchup_stale_hours`` are stored to chat history but skip the LLM
pipeline so the agent doesn't burn tokens replaying old context or fire
late replies into conversations that have moved on.
"""
from __future__ import annotations

import inspect

import puffo_agent.portal.state as state
from puffo_agent.agent.puffo_core_client import (
    DEFAULT_CATCHUP_STALE_HOURS,
    PuffoCoreMessageClient,
)

_48H_MS = 48 * 3600 * 1000
_NOW = 1_000_000_000_000


def _client(catchup_stale_ms: int) -> PuffoCoreMessageClient:
    """Bare client with only the staleness field set — enough to
    exercise the predicate without the full listen() harness."""
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
    # A huge-threshold client with an epoch-0 message is always stale
    # under the real clock — exercises the now_ms=None branch.
    assert _client(_48H_MS)._is_stale_for_catchup(0) is True


def test_gate_wired_into_listen_before_admit():
    """handle_envelope is a closure inside listen(); pin the gate's
    presence + ordering at the source level so a refactor can't silently
    drop it or move it past the LLM-admit."""
    src = inspect.getsource(PuffoCoreMessageClient.listen)
    assert "_is_stale_for_catchup(payload.sent_at)" in src
    assert "staleness-gate-skipped" in src
    gate = src.index("_is_stale_for_catchup(payload.sent_at)")
    admit = src.index("_admit_thread_message(")
    assert gate < admit, "staleness gate must precede _admit_thread_message"
    # The DB store must run BEFORE the gate — a skipped envelope is still
    # persisted (AC #2). Pin the ordering so a refactor can't move the
    # gate above the store.
    store = src.index("self.store.store(")
    assert store < gate, "store.store must precede the staleness gate"
    # The self-echo + invite/leave intercepts must run regardless of age,
    # so they sit ahead of the gate.
    self_echo = src.index("payload.sender_slug == self.slug")
    leave = src.index("_maybe_handle_leave_reply")
    assert self_echo < gate and leave < gate


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
