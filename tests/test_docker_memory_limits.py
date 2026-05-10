"""``DockerCLIAdapter`` injects --memory and --memory-reservation
into the ``docker run`` argv when the corresponding fields are set.

Per-container caps bound OOM kills to the offending container
rather than letting one runaway exhaust the VM's swap and cascade
to neighbours. Tests cover the argv-construction path only;
``_start_container`` is mocked at the boundary.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from puffo_agent.agent.adapters import docker_cli
from puffo_agent.agent.adapters.docker_cli import DockerCLIAdapter


def _make_adapter(tmp_path, memory_limit="", memory_reservation=""):
    return DockerCLIAdapter(
        agent_id="t",
        model="",
        image="puffo/agent-runtime:test",
        workspace_dir=str(tmp_path / "ws"),
        claude_dir=str(tmp_path / "ws" / ".claude"),
        session_file=str(tmp_path / "session.json"),
        agent_home_dir=str(tmp_path / "home"),
        shared_fs_dir=str(tmp_path / "shared"),
        memory_limit=memory_limit,
        memory_reservation=memory_reservation,
    )


def _capture_docker_run_argv(adapter, monkey_home) -> list[str]:
    """Drive ``_start_container`` with ``_run_cmd`` patched to a
    capture-and-noop stub. Returns the argv ``docker run`` would have
    received. ``Path.home`` is redirected so the host-credentials touch
    lands in a tmp dir, not the real operator home.
    """
    captured: list[list[str]] = []

    async def _fake_run_cmd(cmd, check=True):
        captured.append(list(cmd))
        return 0, b"", b""

    with patch.object(docker_cli, "_run_cmd", new=_fake_run_cmd), \
         patch.object(docker_cli.Path, "home", staticmethod(lambda: monkey_home)):
        asyncio.run(adapter._start_container())

    for cmd in captured:
        if len(cmd) >= 2 and cmd[0] == "docker" and cmd[1] == "run":
            return cmd
    raise AssertionError(
        f"no docker-run invocation captured; got: {captured!r}"
    )


def test_no_flags_when_both_unset(tmp_path):
    adapter = _make_adapter(tmp_path)
    argv = _capture_docker_run_argv(adapter, tmp_path)
    assert "--memory" not in argv
    assert "--memory-reservation" not in argv


def test_memory_flag_injected(tmp_path):
    adapter = _make_adapter(tmp_path, memory_limit="1.5g")
    argv = _capture_docker_run_argv(adapter, tmp_path)
    idx = argv.index("--memory")
    assert argv[idx + 1] == "1.5g"
    assert "--memory-reservation" not in argv


def test_reservation_flag_injected(tmp_path):
    adapter = _make_adapter(tmp_path, memory_reservation="500m")
    argv = _capture_docker_run_argv(adapter, tmp_path)
    idx = argv.index("--memory-reservation")
    assert argv[idx + 1] == "500m"
    assert "--memory" not in argv


def test_both_flags_injected_in_expected_order(tmp_path):
    adapter = _make_adapter(
        tmp_path, memory_limit="1.5g", memory_reservation="500m",
    )
    argv = _capture_docker_run_argv(adapter, tmp_path)
    assert argv.index("--memory") < argv.index("--memory-reservation")
    assert argv[argv.index("--memory") + 1] == "1.5g"
    assert argv[argv.index("--memory-reservation") + 1] == "500m"


def test_flags_appear_before_image_token(tmp_path):
    """Docker rejects flags after the image positional. Both caps
    must be inserted *before* ``self.image`` in argv."""
    adapter = _make_adapter(
        tmp_path, memory_limit="1g", memory_reservation="256m",
    )
    argv = _capture_docker_run_argv(adapter, tmp_path)
    image_idx = argv.index("puffo/agent-runtime:test")
    assert argv.index("--memory") < image_idx
    assert argv.index("--memory-reservation") < image_idx


def test_daemon_defaults_apply_when_yaml_omits_fields(tmp_path):
    """``DaemonConfig`` defaults to 1.5g / 500m so fresh installs and
    older daemon.yml files without these fields both get caps applied."""
    from puffo_agent.portal.state import DaemonConfig
    cfg = DaemonConfig()
    assert cfg.docker_memory_limit == "1.5g"
    assert cfg.docker_memory_reservation == "500m"
