"""Pi Puffo tool bridge: installation, runtime attestation, and node interop.

The interop tests run the real bridge source under plain ``node`` against a
stub MCP server, so the framing, correlation, validation, and failure paths
exercised here are the shipped ones. Two things they deliberately do not cover,
because nothing offline can:

* Pi's own ``registerTool`` binding -- the harness supplies a recording double.
* TypeBox -- the bridge's single call, ``Type.Unsafe``, is a pass-through, and
  the 0.84.3 bundle hands ``tool.parameters`` straight to the provider, so the
  pinned shim matches both sides. See ``fixtures/pi_bridge_typebox_shim``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from puffo_agent.agent.harness.driver import McpServerSpec, RuntimeSpec
from puffo_agent.agent.harness.drivers.pi_bridge import (
    BRIDGE_CONFIG_ENV,
    BRIDGE_NONCE_ENV,
    BRIDGE_READY_FILE_ENV,
    await_bridge_ready,
    bridge_install_path,
    bridge_source_text,
    build_bridge_environment,
    clear_ready_file,
    install_pi_tool_bridge,
    mint_bridge_nonce,
    read_ready_attestation,
    ready_file_path,
)
from puffo_agent.agent.harness.drivers.pi import (
    PI_AGENT_DIR_ENV,
    PiDriver,
    PiToolBridgeUnavailableError,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_NODE = shutil.which("node")

requires_node = pytest.mark.skipif(_NODE is None, reason="node is not installed")


# -- installation ------------------------------------------------------------


def test_install_writes_the_bridge_where_pi_auto_discovers_it(tmp_path):
    target = install_pi_tool_bridge(tmp_path)
    assert target == bridge_install_path(tmp_path)
    assert target.parent.name == "extensions"
    assert target.read_text(encoding="utf-8") == bridge_source_text()


def test_install_is_idempotent_and_does_not_touch_an_unchanged_file(tmp_path):
    """Rewriting every spawn makes "when did this change?" unanswerable."""
    first = install_pi_tool_bridge(tmp_path)
    stamp = first.stat().st_mtime_ns
    again = install_pi_tool_bridge(tmp_path)
    assert again.stat().st_mtime_ns == stamp


def test_install_refreshes_a_stale_bridge(tmp_path):
    target = bridge_install_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("// an older bridge", encoding="utf-8")
    install_pi_tool_bridge(tmp_path)
    assert target.read_text(encoding="utf-8") == bridge_source_text()


def test_bridge_environment_carries_the_server_location_not_a_guess():
    ready = Path("/agent/.pi/agent/puffo-bridge-ready.json")
    env = build_bridge_environment(
        mcp=McpServerSpec(
            name="puffo",
            command="/usr/bin/python3",
            args=("-m", "puffo_agent.mcp.puffo_core_server"),
            environment={"PUFFO_AGENT_ID": "a1"},
        ),
        ready_file=ready,
        nonce="n1",
    )
    config = json.loads(env[BRIDGE_CONFIG_ENV])
    assert config["command"] == "/usr/bin/python3"
    assert config["args"] == ["-m", "puffo_agent.mcp.puffo_core_server"]
    assert config["environment"] == {"PUFFO_AGENT_ID": "a1"}
    assert env[BRIDGE_READY_FILE_ENV] == str(ready)
    assert env[BRIDGE_NONCE_ENV] == "n1"


# -- runtime attestation -----------------------------------------------------


def test_attestation_requires_this_spawns_nonce(tmp_path):
    """A previous run's file must not attest the current process."""
    path = ready_file_path(tmp_path)
    path.write_text(json.dumps({"nonce": "old", "tools": 3}))
    assert read_ready_attestation(path, "fresh") is None
    assert read_ready_attestation(path, "old") == 3


def test_zero_tool_attestation_is_not_readiness(tmp_path):
    """Reporting success with no tools is the mute agent, self-declared."""
    path = ready_file_path(tmp_path)
    path.write_text(json.dumps({"nonce": "n", "tools": 0}))
    assert read_ready_attestation(path, "n") is None


@pytest.mark.parametrize(
    "payload", ["not json", '{"nonce": "n"}', "[]", '{"nonce":"n","tools":"3"}']
)
def test_malformed_attestation_is_not_readiness(tmp_path, payload):
    path = ready_file_path(tmp_path)
    path.write_text(payload)
    assert read_ready_attestation(path, "n") is None


def test_clear_ready_file_tolerates_an_absent_file(tmp_path):
    clear_ready_file(ready_file_path(tmp_path))  # must not raise


def test_clear_ready_file_fails_closed_when_stale_attestation_cannot_be_removed(
    tmp_path, monkeypatch
):
    path = ready_file_path(tmp_path)
    path.write_text(json.dumps({"nonce": "same-spec", "tools": 3}))

    def denied(_self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(PermissionError, match="denied"):
        clear_ready_file(path)


@pytest.mark.asyncio
async def test_await_bridge_ready_is_bounded(tmp_path):
    assert (
        await await_bridge_ready(
            ready_file_path(tmp_path), "n", timeout_seconds=0.2, poll_seconds=0.05
        )
        is None
    )


def test_minted_nonces_are_distinct():
    assert mint_bridge_nonce() != mint_bridge_nonce()


# -- driver refuses an unloaded bridge ---------------------------------------


def _installed_spec(tmp_path, *, nonce="n1", ready=True) -> RuntimeSpec:
    install_pi_tool_bridge(tmp_path)
    environment = {PI_AGENT_DIR_ENV: str(tmp_path), BRIDGE_NONCE_ENV: nonce}
    if ready:
        environment[BRIDGE_READY_FILE_ENV] = str(ready_file_path(tmp_path))
    return RuntimeSpec(workspace_dir="/tmp/ws", environment=environment)


@pytest.mark.asyncio
async def test_open_refuses_when_the_installed_bridge_never_loads(tmp_path):
    """Installation is not load.

    The file is present and correct here; the extension simply never attested.
    That is what a load-time throw, a disabled extension, or a stale install
    looks like from outside, and it must not open a session.
    """
    spec = _installed_spec(tmp_path)
    driver = PiDriver(
        process_factory=lambda s: _never_attesting_process(),
        bridge_ready_timeout=0.3,
    )
    with pytest.raises(PiToolBridgeUnavailableError, match="did not attest"):
        await driver.open(spec)


@pytest.mark.asyncio
async def test_open_refuses_a_stale_attestation_from_a_previous_spawn(tmp_path):
    """The nonce survives a restart, so the file must be cleared per spawn."""
    spec = _installed_spec(tmp_path)
    ready_file_path(tmp_path).write_text(json.dumps({"nonce": "n1", "tools": 3}))
    driver = PiDriver(
        process_factory=lambda s: _never_attesting_process(),
        bridge_ready_timeout=0.3,
    )
    with pytest.raises(PiToolBridgeUnavailableError, match="did not attest"):
        await driver.open(spec)


@pytest.mark.asyncio
async def test_open_refuses_when_readiness_cannot_be_attested_at_all(tmp_path):
    spec = _installed_spec(tmp_path, ready=False)
    driver = PiDriver(process_factory=lambda s: _never_attesting_process())
    with pytest.raises(PiToolBridgeUnavailableError, match="cannot be attested"):
        await driver.open(spec)


def _never_attesting_process():
    """A healthy Pi child whose extension never registered anything.

    Deliberately responsive: the point of these tests is that a working RPC
    surface is not evidence the tool bridge loaded.
    """
    import asyncio

    class _Stdin:
        def __init__(self, out):
            self._out = out

        def write(self, data: bytes) -> None:
            for line in data.split(b"\n"):
                if not line.strip():
                    continue
                frame = json.loads(line)
                self._out.feed_data(
                    json.dumps(
                        {
                            "type": "response",
                            "command": frame.get("type"),
                            "success": True,
                            "id": frame.get("id"),
                            "data": {"sessionFile": "/sessions/s.jsonl"},
                        }
                    ).encode()
                    + b"\n"
                )

        async def drain(self) -> None:
            return None

    class _Proc:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdin = _Stdin(self.stdout)
            self.stderr = None
            self.returncode = None

        def terminate(self):
            self.returncode = 0
            self.stdout.feed_eof()

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return 0

    return _Proc()


# -- node interop against a stub MCP server ----------------------------------


def _run_bridge(tmp_path, *, mode="ok", scenario="start", nonce="n1", env=None):
    """Run the shipped bridge under node against the stub MCP server."""
    work = tmp_path / "bridge"
    work.mkdir(exist_ok=True)
    (work / "puffo-tools.ts").write_text(bridge_source_text(), encoding="utf-8")
    shutil.copy(_FIXTURES / "pi_bridge_harness.ts", work / "harness.ts")
    shim = work / "node_modules" / "typebox"
    if not shim.exists():
        shutil.copytree(_FIXTURES / "pi_bridge_typebox_shim", shim)
    (work / "package.json").write_text('{"type":"module"}')

    ready = work / "ready.json"
    server = _FIXTURES / "pi_bridge_stub_server.mjs"
    child_env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        BRIDGE_CONFIG_ENV: json.dumps(
            {
                "command": _NODE,
                "args": [str(server)],
                "environment": {"PUFFO_STUB_MODE": mode, "PATH": "/usr/bin:/bin"},
            }
        ),
        BRIDGE_READY_FILE_ENV: str(ready),
        BRIDGE_NONCE_ENV: nonce,
    }
    if env is not None:
        child_env.update(env)
    result = subprocess.run(
        [_NODE, str(work / "harness.ts"), scenario],
        capture_output=True,
        text=True,
        timeout=60,
        env=child_env,
        cwd=str(work),
    )
    assert result.stdout.strip(), f"no harness output; stderr={result.stderr[-800:]}"
    return json.loads(_last_line(result.stdout)), ready, result


def _last_line(stdout: str) -> str:
    """Split on LF only.

    ``str.splitlines()`` also splits on U+2028/U+2029/U+0085, which a tool
    result may legally contain -- the same hazard the bridge itself guards
    against. Using it here tore this helper's own JSON in half.
    """
    return [line for line in stdout.split("\n") if line.strip()][-1]


@requires_node
def test_bridge_registers_every_tool_the_server_advertises(tmp_path):
    payload, ready, _proc = _run_bridge(tmp_path)
    assert payload["ok"] is True
    assert payload["registered"] == ["send_message", "read_inbox"]
    attested = json.loads(ready.read_text())
    assert attested == {"nonce": "n1", "tools": 2}


@requires_node
def test_bridge_calls_a_tool_end_to_end(tmp_path):
    payload, _ready, _proc = _run_bridge(tmp_path, scenario="call")
    assert payload["ok"] is True
    assert payload["result"]["isError"] is False
    assert payload["result"]["content"][0]["text"] == "sent"


@requires_node
def test_tool_result_survives_unicode_separators(tmp_path):
    """U+2028/U+2029/U+0085 are legal in a JSON string and must not split it."""
    payload, _ready, _proc = _run_bridge(tmp_path, mode="separators", scenario="call")
    assert payload["ok"] is True
    assert payload["result"]["content"][0]["text"] == ("before middle afterend")


@requires_node
@pytest.mark.parametrize(
    "mode,code",
    [
        ("empty", "puffo_bridge_no_tools"),
        ("duplicate", "puffo_bridge_tool_surface_invalid"),
        ("blank_name", "puffo_bridge_tool_surface_invalid"),
        ("bad_schema", "puffo_bridge_tool_surface_invalid"),
    ],
)
def test_unusable_tool_surface_fails_closed_and_attests_nothing(tmp_path, mode, code):
    """Never partially registered, and never attested as ready."""
    payload, ready, _proc = _run_bridge(tmp_path, mode=mode)
    assert payload["ok"] is False
    assert payload["code"] == code
    assert payload["known"] is True
    assert payload["registered"] == []
    assert not ready.exists()


@requires_node
def test_server_exit_before_listing_is_reported_as_unavailable(tmp_path):
    payload, ready, _proc = _run_bridge(tmp_path, mode="exit_before_list")
    assert payload["ok"] is False
    assert payload["code"] == "puffo_bridge_unavailable"
    assert not ready.exists()


@requires_node
def test_half_frame_then_server_exit_is_reported_as_unavailable(tmp_path):
    payload, ready, _proc = _run_bridge(tmp_path, mode="half_frame_before_list")
    assert payload["ok"] is False
    assert payload["code"] == "puffo_bridge_unavailable"
    assert not ready.exists()


@requires_node
def test_a_silent_server_times_out_rather_than_hanging(tmp_path):
    payload, ready, _proc = _run_bridge(
        tmp_path, mode="no_handshake", env={"PUFFO_BRIDGE_TIMEOUT_MS": "500"}
    )
    assert payload["ok"] is False
    assert payload["code"] == "puffo_bridge_timeout"
    assert not ready.exists()


@requires_node
def test_a_failed_tool_call_reports_a_code_not_provider_text(tmp_path):
    """The stub's error message must not reach the model or the result."""
    payload, _ready, _proc = _run_bridge(tmp_path, mode="tool_error", scenario="call")
    assert payload["ok"] is True  # registration succeeded; the call failed
    assert payload["result"]["isError"] is True
    rendered = json.dumps(payload["result"])
    assert "SECRET-PROVIDER-TEXT" not in rendered
    assert payload["result"]["content"][0]["text"] == "puffo_tool_error"


_CONFIG_MARKER = "CONFIG-MUST-NOT-LEAK"


@requires_node
@pytest.mark.parametrize(
    "value,code",
    [
        (f"not json {_CONFIG_MARKER}", "puffo_bridge_config_invalid"),
        (f'["{_CONFIG_MARKER}"]', "puffo_bridge_config_invalid"),
        (f'{{"args":["{_CONFIG_MARKER}"]}}', "puffo_bridge_config_invalid"),
        (
            f'{{"command":"python","args":"{_CONFIG_MARKER}","environment":{{}}}}',
            "puffo_bridge_config_invalid",
        ),
        (
            f'{{"command":"python","args":[],"environment":["{_CONFIG_MARKER}"]}}',
            "puffo_bridge_config_invalid",
        ),
        (
            f'{{"command":"python","args":[],"environment":'
            f'{{"TOKEN":7,"MARKER":"{_CONFIG_MARKER}"}}}}',
            "puffo_bridge_config_invalid",
        ),
    ],
)
def test_bad_configuration_fails_closed_without_echoing_it(tmp_path, value, code):
    """The rejection must not quote the value it rejected.

    The configuration blob carries the Puffo MCP server's environment, so an
    error that echoes it to explain itself is a disclosure. Every case here
    plants a marker inside the value and asserts it reaches neither stream.
    """
    payload, ready, proc = _run_bridge(tmp_path, env={BRIDGE_CONFIG_ENV: value})
    assert payload["ok"] is False
    assert payload["code"] == code
    assert not ready.exists()
    assert _CONFIG_MARKER not in proc.stdout
    assert _CONFIG_MARKER not in proc.stderr


@requires_node
def test_missing_configuration_fails_closed(tmp_path):
    work = tmp_path / "bridge"
    work.mkdir(exist_ok=True)
    (work / "puffo-tools.ts").write_text(bridge_source_text(), encoding="utf-8")
    shutil.copy(_FIXTURES / "pi_bridge_harness.ts", work / "harness.ts")
    shutil.copytree(
        _FIXTURES / "pi_bridge_typebox_shim", work / "node_modules" / "typebox"
    )
    (work / "package.json").write_text('{"type":"module"}')
    result = subprocess.run(
        [_NODE, str(work / "harness.ts"), "start"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
        cwd=str(work),
    )
    payload = json.loads(_last_line(result.stdout))
    assert payload["ok"] is False
    assert payload["code"] == "puffo_bridge_config_missing"


@requires_node
@pytest.mark.parametrize("missing", [BRIDGE_READY_FILE_ENV, BRIDGE_NONCE_ENV])
def test_missing_attestation_configuration_fails_closed(tmp_path, missing):
    payload, ready, _proc = _run_bridge(tmp_path, env={missing: ""})
    assert payload["ok"] is False
    assert payload["code"] == "puffo_bridge_config_missing"
    assert not ready.exists()


def test_bridge_source_is_declared_as_package_data():
    """A wheel without the .ts installs a bridge that does not exist.

    Running from a source tree hides this completely: the file is simply
    there. Only the packaging declaration makes it true after install.
    """
    import tomllib

    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    package_data = config["tool"]["setuptools"]["package-data"]
    patterns = package_data.get("puffo_agent.agent.harness.drivers.pi_bridge", [])
    assert any(p.endswith(".ts") for p in patterns), (
        "puffo-tools.ts must ship as package data or the installed bridge "
        "is missing at runtime"
    )
