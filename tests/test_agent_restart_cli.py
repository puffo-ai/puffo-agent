"""A quarantined turn parks its agent until an operator accepts that replay
may repeat external effects. Only the operator's remote client could accept
that, so a local deployment had no way out; `agent restart` is that way."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from puffo_agent.agent.turn_recovery import (
    TurnRecovery,
    authorize_retry,
    read_recovery,
    write_recovery,
)
from puffo_agent.portal.cli import main
from puffo_agent.portal.state import AgentConfig, restart_flag_path

from _global_inbox_support import Adapter, make_store, receipt

STOPPED = TurnRecovery(
    session_ref="session_e3ce", turn_ref="turn_300f",
    provider_session_id="903c7bce", provider_turn_id="native-turn",
    owner="owner", reason="autonomous idle timeout",
    durable_turn_id="turn_2049", stop_attempted=True, stopped=True,
)


def _agent(aid: str = "helen-parr-3874", state: str = "running"):
    cfg = AgentConfig(id=aid, display_name=aid, state=state)
    cfg.save()
    return cfg


def _park(cfg: AgentConfig, record: TurnRecovery = STOPPED):
    workspace = cfg.resolve_workspace_dir()
    workspace.mkdir(parents=True, exist_ok=True)
    write_recovery(workspace, record)
    return workspace


def test_authorize_retry_marks_the_record(tmp_path):
    write_recovery(tmp_path, STOPPED)
    assert authorize_retry(tmp_path).retry_requested is True
    assert read_recovery(tmp_path).retry_requested is True
    # idempotent
    assert authorize_retry(tmp_path).retry_requested is True


def test_authorize_retry_is_a_noop_without_an_open_gate(tmp_path):
    assert authorize_retry(tmp_path) is None
    write_recovery(tmp_path, replace(STOPPED, resolved=True))
    assert authorize_retry(tmp_path) is None


def test_authorize_retry_refuses_while_provider_may_still_run(tmp_path):
    write_recovery(tmp_path, replace(STOPPED, stopped=False, stop_attempted=False))
    with pytest.raises(ValueError, match="stop is unconfirmed"):
        authorize_retry(tmp_path)
    assert read_recovery(tmp_path).retry_requested is False


def test_restart_clears_the_gate_and_requests_a_respawn(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    from puffo_agent.portal import cli as cli_mod

    monkeypatch.setattr(cli_mod, "is_daemon_alive", lambda: True)
    cfg = _agent()
    workspace = _park(cfg)

    assert main(["agent", "restart", cfg.id]) == 0

    assert read_recovery(workspace).retry_requested is True
    assert restart_flag_path(cfg.id).exists()
    out = capsys.readouterr().out
    assert "clearing the recovery gate" in out
    assert "respawn it on the next tick" in out


def test_restart_without_a_gate_is_a_plain_respawn(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    cfg = _agent()

    assert main(["agent", "restart", cfg.id]) == 0

    assert restart_flag_path(cfg.id).exists()
    out = capsys.readouterr().out
    assert "restart requested" in out
    assert "recovery gate" not in out
    assert "daemon is not running" in out


def test_restart_keeps_the_gate_when_the_provider_stop_is_unconfirmed(
    tmp_path, monkeypatch, capsys,
):
    """Replay could double-run a turn whose provider may still be alive, so
    the respawn happens but the gate stays."""
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    cfg = _agent()
    workspace = _park(cfg, replace(STOPPED, stopped=False))

    assert main(["agent", "restart", cfg.id]) == 0

    assert read_recovery(workspace).retry_requested is False
    assert restart_flag_path(cfg.id).exists()
    captured = capsys.readouterr()
    assert "stays parked" in captured.err
    assert "restart requested" in captured.out


def test_restart_refuses_a_paused_or_unknown_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    paused = _agent("paused-0001", state="paused")

    assert main(["agent", "restart", paused.id]) == 1
    assert "resume it instead" in capsys.readouterr().err
    assert not restart_flag_path(paused.id).exists()
    assert main(["agent", "restart", "nope-0000"]) == 2


def test_show_reports_an_open_gate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    cfg = _agent()
    workspace = _park(cfg)

    assert main(["agent", "show", cfg.id]) == 0

    out = capsys.readouterr().out
    assert "recovery gate (agent is parked)" in out
    assert "autonomous idle timeout" in out
    assert "903c7bce" in out and "turn_2049" in out
    assert f"puffo-agent agent restart {cfg.id}" in out
    # read-only
    assert read_recovery(workspace).retry_requested is False
    assert not restart_flag_path(cfg.id).exists()


def test_show_is_quiet_without_a_gate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    cfg = _agent()
    _park(cfg, replace(STOPPED, resolved=True))

    assert main(["agent", "show", cfg.id]) == 0

    assert "recovery gate" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_authorized_retry_replays_a_turn_that_holds_messages(tmp_path):
    """The startup self-healing path deliberately refuses a turn with admitted
    rows; the operator-authorized replay is what requeues them."""
    from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime

    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store, adapter=Adapter(), run_turn=lambda _: None, workspace=tmp_path,
    )
    runtime.register_autonomous_adoption()
    runtime._autonomous_ready = True
    assert await runtime.adopt_autonomous_turn(
        provider_session_id="903c7bce", provider_turn_id="native-turn",
    )
    turn_id = runtime.active.turn_id
    await receipt(store, "blocked-message", 1)
    rows = await store.get_pending()
    await runtime._admit_inbox_page(
        SimpleNamespace(selected=rows, remaining_count=0), snapshot_generation=0,
        requesting_turn_id=turn_id,
        requesting_provider_session_id="903c7bce",
        requesting_provider_turn_id="native-turn",
    )
    write_recovery(tmp_path, replace(STOPPED, durable_turn_id=turn_id))

    # Parked: startup recovery cannot resolve a gate whose turn holds rows.
    assert await runtime.recover_orphaned_turns() == 0
    assert read_recovery(tmp_path).resolved is False

    authorize_retry(tmp_path)
    await runtime._retry_quarantined_turn()

    assert read_recovery(tmp_path).resolved is True
    assert (await store.get_turn_run(turn_id)).state == "requeued"
    assert (
        await store.get_message_by_envelope("blocked-message")
    ).processing_state == "pending"
    await store.close()


@pytest.mark.asyncio
async def test_parked_diagnostic_names_the_restart_command(tmp_path):
    """The operator's only clue is this text, so it must name the command."""
    from puffo_agent.agent.global_inbox_runtime import GlobalInboxRuntime

    store = await make_store(tmp_path)
    runtime = GlobalInboxRuntime(
        store=store, adapter=Adapter(), run_turn=lambda _: None,
        workspace=tmp_path, agent_id="helen-parr-3874",
    )
    write_recovery(tmp_path, STOPPED)

    runtime._report_recovery_required()

    assert "puffo-agent agent restart helen-parr-3874" in runtime.health.diagnostic
    await store.close()
