from __future__ import annotations

import asyncio

import pytest

from puffo_agent.agent.harness import docker_support


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
