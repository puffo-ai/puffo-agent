from __future__ import annotations

import asyncio

import pytest

from puffo_agent.agent.harness.runtime import docker_support


def test_probe_result_is_fail_closed():
    assert docker_support.probe_result(0) is True
    assert docker_support.probe_result(docker_support.PROBE_FALSE_EXIT) is False
    assert docker_support.probe_result(1) is None
    assert docker_support.probe_result(125) is None


class _HangingProcess:
    def __init__(self):
        self.returncode = None
        self.killed = False
        self.reaped = False
        self._done = asyncio.Event()

    async def communicate(self, _input=None):
        await self._done.wait()
        self.reaped = True
        return b"", b""

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode


@pytest.mark.parametrize("cancel", [False, True])
def test_docker_child_is_killed_and_reaped(cancel):
    async def scenario():
        proc = _HangingProcess()
        task = asyncio.create_task(
            docker_support.communicate_with_timeout(
                proc,
                timeout_seconds=60 if cancel else 0.01,
                operation="docker inspect",
            )
        )
        if cancel:
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError, match="timed out"):
                await task
        assert proc.killed is True
        assert proc.reaped is True

    asyncio.run(scenario())


def test_puffo_agent_pkg_dir_is_the_package_not_site_packages():
    """Mount the package itself, never its parent.

    Under a non-editable install the parent IS site-packages; mounting it puts
    the host's platform-specific builds (pydantic_core, cryptography, PIL) ahead
    of the image's own on PYTHONPATH. On a macOS/Windows host those are the
    wrong ABI, the in-container MCP server dies at import, and every agent
    silently loses send_message while still reporting healthy.
    """
    from pathlib import Path

    import puffo_agent
    from puffo_agent.agent.harness.runtime.docker_support import (
        puffo_agent_pkg_dir,
    )

    pkg_dir = puffo_agent_pkg_dir()
    assert pkg_dir.name == "puffo_agent"
    assert (pkg_dir / "__init__.py").is_file()
    assert pkg_dir == Path(puffo_agent.__file__).resolve().parent
    # site-packages would contain sibling distributions; the package must not.
    assert not (pkg_dir / "pydantic_core").exists()
