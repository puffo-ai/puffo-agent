"""``DockerCLIAdapter`` exposes host Claude Code plugins to the
container.

Two pieces — the bind-mount that surfaces ``~/.claude/plugins/`` at
the canonical in-container path, and the ``sync_host_enabled_plugins``
call that propagates the ``enabledPlugins`` array via the per-agent
settings.json. The image bakes node/npm/python+uv so most
``npx``/``uvx`` plugin commands resolve naturally; this test suite
covers only the wiring — actual plugin-load behavior lives in
Claude's own pipeline.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from puffo_agent.agent.adapters import docker_cli
from puffo_agent.agent.adapters.docker_cli import DockerCLIAdapter


def _make_adapter(tmp_path):
    return DockerCLIAdapter(
        agent_id="t",
        model="",
        image="puffo/agent-runtime:test",
        workspace_dir=str(tmp_path / "ws"),
        claude_dir=str(tmp_path / "ws" / ".claude"),
        session_file=str(tmp_path / "session.json"),
        agent_home_dir=str(tmp_path / "home"),
        shared_fs_dir=str(tmp_path / "shared"),
    )


def _capture_docker_run_argv(adapter, monkey_home) -> list[str]:
    """Drive ``_start_container`` with ``_run_cmd`` patched to a
    capture-and-noop stub. Mirrors the helper in
    ``test_docker_memory_limits.py`` — kept inline to avoid a shared
    test-helper module for a 15-line function."""
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


# ── bind-mount ──────────────────────────────────────────────────────────────


def test_plugins_bindmount_injected_when_host_dir_exists(tmp_path):
    """Host has ``~/.claude/plugins/`` → the docker run argv gains a
    nested read-only bind-mount surfacing it at the canonical in-
    container path."""
    host = tmp_path / "host"
    plugins = host / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text("{}", encoding="utf-8")
    adapter = _make_adapter(tmp_path)

    argv = _capture_docker_run_argv(adapter, host)

    mount = f"{plugins}:/home/agent/.claude/plugins:ro"
    assert mount in argv, (
        f"expected bind-mount {mount!r} in docker-run argv; got: {argv!r}"
    )
    # The outer .claude bind-mount has to come BEFORE the nested
    # plugins one for Docker to honor the overlay.
    claude_mount_idx = next(
        i for i, a in enumerate(argv)
        if isinstance(a, str) and a.endswith(":/home/agent/.claude")
    )
    plugins_mount_idx = argv.index(mount)
    assert claude_mount_idx < plugins_mount_idx


def test_plugins_bindmount_absent_when_host_dir_missing(tmp_path):
    """No host plugins dir → no extra mount. Avoids Docker
    auto-creating an empty plugins/ on the host."""
    host = tmp_path / "host"  # no .claude/plugins/
    (host / ".claude").mkdir(parents=True)
    adapter = _make_adapter(tmp_path)

    argv = _capture_docker_run_argv(adapter, host)

    plugin_mounts = [a for a in argv if isinstance(a, str) and "/.claude/plugins" in a]
    assert plugin_mounts == [], (
        f"expected no plugins mount when host has none; got: {plugin_mounts!r}"
    )


def test_plugins_bindmount_is_readonly(tmp_path):
    """The mount must be ``:ro``. Agents must never write back into
    the operator's plugin tree — a stray write could corrupt a
    marketplace's git checkout or an installed_plugins.json other
    agents depend on."""
    host = tmp_path / "host"
    plugins = host / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    adapter = _make_adapter(tmp_path)

    argv = _capture_docker_run_argv(adapter, host)

    plugins_mount = next(
        a for a in argv
        if isinstance(a, str) and "/.claude/plugins" in a and a.endswith(":ro")
    )
    assert plugins_mount.endswith(":/home/agent/.claude/plugins:ro")


def test_plugins_bindmount_appears_before_image(tmp_path):
    """Docker rejects flags after the image positional, same
    constraint as the memory-limit tests."""
    host = tmp_path / "host"
    plugins = host / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    adapter = _make_adapter(tmp_path)

    argv = _capture_docker_run_argv(adapter, host)

    image_idx = argv.index("puffo/agent-runtime:test")
    plugins_idx = next(
        i for i, a in enumerate(argv)
        if isinstance(a, str) and "/.claude/plugins:ro" in a
    )
    assert plugins_idx < image_idx


# ── enabledPlugins propagation ──────────────────────────────────────────────


def test_ensure_started_propagates_enabled_plugins(tmp_path):
    """``_ensure_started`` calls ``sync_host_enabled_plugins`` so the
    per-agent settings.json — which is reachable inside the
    container via the existing ``.claude`` bind-mount — carries the
    host's enabledPlugins array.

    The integration path is heavily mocked (``_run_cmd``,
    ``_ensure_image``, ``_start_container``) so the test can run
    without a docker daemon. The assertion targets the on-disk
    settings.json the sync helper writes, not the docker argv —
    that's covered by the bind-mount tests above."""
    host = tmp_path / "host"
    (host / ".claude").mkdir(parents=True)
    (host / ".claude" / "settings.json").write_text(
        json.dumps({
            "enabledPlugins": {
                "imessage@claude-plugins-official": True,
                "chrome-devtools-mcp@claude-plugins-official": True,
            },
        }),
        encoding="utf-8",
    )
    adapter = _make_adapter(tmp_path)
    agent_home = tmp_path / "home"

    async def _noop_coro(*_args, **_kwargs):
        return None

    async def _fake_run_cmd(cmd, check=True):
        # ``_container_state`` parses an empty stdout as "", which
        # routes _ensure_started into the build+run branch we noop
        # below. ``_puffo_pkg_mount_is_current`` is bypassed because
        # ``existed`` resolves False (state == "").
        return 0, b"", b""

    async def _run() -> None:
        with patch.object(docker_cli, "_run_cmd", new=_fake_run_cmd), \
             patch.object(docker_cli.Path, "home", staticmethod(lambda: host)), \
             patch.object(docker_cli.shutil, "which", lambda _: "/fake/docker"), \
             patch.object(adapter, "_ensure_image", side_effect=_noop_coro), \
             patch.object(adapter, "_start_container", side_effect=_noop_coro):
            await adapter._ensure_started()

    asyncio.run(_run())

    settings = json.loads(
        (agent_home / ".claude" / "settings.json").read_text(encoding="utf-8"),
    )
    assert settings["enabledPlugins"] == {
        "imessage@claude-plugins-official": True,
        "chrome-devtools-mcp@claude-plugins-official": True,
    }
