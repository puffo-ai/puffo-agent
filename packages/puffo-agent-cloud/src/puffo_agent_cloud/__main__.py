"""Standalone entry point for the cloud runtime.

In production the E2B sandbox runs ``puffo-agent-cloud`` directly (no
fat daemon/worker supervisor): ingest the install bundle(s) dropped
under ``~/.puffo-agent/api-puffo-install/``, resolve the single
provisioned agent, and run its bridge loop until SIGINT/SIGTERM."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from puffo_agent_core.paths import agents_dir

from .bundle import sweep_install_dir
from .runner import ApiPuffoRunner

logger = logging.getLogger(__name__)


def _provisioned_agent_id() -> str | None:
    """The single provisioned agent (an ``agents/<id>/agent.yml``)."""
    adir = agents_dir()
    if not adir.exists():
        return None
    for child in sorted(adir.iterdir()):
        if child.is_dir() and (child / "agent.yml").exists():
            return child.name
    return None


async def _run(agent_id: str) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # add_signal_handler is unavailable on some platforms
            # (e.g. Windows); fall back to the default handler.
            pass
    runner = ApiPuffoRunner(agent_id, stop)
    await runner.run()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] puffo-agent-cloud: %(message)s",
    )
    n = sweep_install_dir()
    if n:
        logger.info("ingested %d install bundle(s)", n)
    agent_id = _provisioned_agent_id()
    if agent_id is None:
        logger.error(
            "no provisioned agent found under %s — install bundle missing?",
            agents_dir(),
        )
        return 1
    logger.info("starting cloud runtime for agent %s", agent_id)
    try:
        asyncio.run(_run(agent_id))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
