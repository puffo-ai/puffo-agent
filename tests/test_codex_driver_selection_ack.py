"""Integration coverage for Codex runtime-selection acknowledgments."""

from __future__ import annotations

import asyncio
import json

import pytest

from puffo_agent.agent.harness.driver import RuntimeSpec
from puffo_agent.agent.harness.drivers.codex import CodexAppServerDriver


class _ResponsiveStdin:
    def __init__(self, process: _ResponsiveProcess) -> None:
        self._process = process

    def write(self, value: bytes) -> None:
        frame = json.loads(value)
        if frame.get("method") == "initialize":
            self._process.feed({"id": frame["id"], "result": {"server": "fake"}})
        elif frame.get("method") == "thread/start":
            self._process.feed({
                "id": frame["id"],
                "result": {"thread": {"id": "th_1"}},
            })

    async def drain(self) -> None:
        await asyncio.sleep(0)


class _ResponsiveProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = _ResponsiveStdin(self)
        self.returncode: int | None = None

    def feed(self, frame: dict) -> None:
        self.stdout.feed_data(json.dumps(frame).encode() + b"\n")

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.feed_eof()

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        return int(self.returncode or 0)


@pytest.mark.asyncio
async def test_codex_open_reports_unacknowledged_model_and_inference_level():
    """A provider response without selection echoes must never look validated."""
    process = _ResponsiveProcess()
    driver = CodexAppServerDriver(lambda _spec: process)

    opened = await driver.open(RuntimeSpec(
        "/workspace", model="gpt-6-astra", inference_level="high",
    ))

    assert opened.diagnostics.warnings == (
        "model_ack_unavailable",
        "inference_level_ack_unavailable",
    )
    await driver.close()
