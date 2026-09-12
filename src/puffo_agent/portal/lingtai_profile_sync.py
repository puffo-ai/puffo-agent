"""Daemon-owned, read-only projection of LingTai names to Puffo identities."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .control.lingtai_profile import _read_source_object, is_lingtai_runtime, read_source_profile
from .state import AgentConfig, discover_agents, home_dir

logger = logging.getLogger(__name__)


def _source_directory(command: list[str]) -> Path | None:
    """Resolve only the active binding in Puffo's registry, never a guessed folder."""
    flags: dict[str, str] = {}
    for index, arg in enumerate(command):
        if arg in {"--registry", "--runtime-id"} and index + 1 < len(command):
            flags[arg] = command[index + 1]
        elif arg.startswith(("--registry=", "--runtime-id=")):
            key, value = arg.split("=", 1)
            flags[key] = value
    registry = home_dir().resolve() / "lingtai" / "runtime-registry.json"
    if flags.get("--registry") != str(registry) or not flags.get("--runtime-id"):
        return None
    try:
        document = _read_source_object(registry)
        entries = document.get("runtimes")
        if not isinstance(entries, dict):
            return None
        entry = entries.get(flags["--runtime-id"])
        if not isinstance(entry, dict) or entry.get("status") != "active":
            return None
        if entry.get("runtime_id") != flags["--runtime-id"]:
            return None
        directory = entry.get("agent_dir")
        if isinstance(directory, str) and Path(directory).is_absolute():
            return Path(directory)
    except (OSError, ValueError, RecursionError):
        pass
    return None


class LingtaiProfileSync:
    """Single lifecycle owner; remember only successfully published source names."""

    def __init__(self) -> None:
        self._published: dict[str, tuple] = {}

    async def sync_one(self, cfg: AgentConfig) -> None:
        if not is_lingtai_runtime(cfg.runtime) or cfg.puffo_core.transport == "bridge":
            return
        directory = _source_directory(cfg.runtime.harness_command)
        if directory is None:
            return
        source = read_source_profile(directory)
        if source.profile_read_error:
            return
        # A lost current manifest can expose an unnamed bootstrap manifest;
        # that is not evidence of an intentional name clear after import.
        if source.agent_name is None and (source.name_source_file != ".agent.json" or not source.name_is_explicit_null):
            return
        snapshot = (cfg.puffo_core.server_url, cfg.puffo_core.slug,
                    tuple(cfg.runtime.harness_command), str(directory), source.agent_name)
        if self._published.get(cfg.id) == snapshot:
            return
        from .profile_sync import sync_agent_profile

        # Only the public source name crosses this boundary. No prompt, model,
        # description, nickname, or workspace content is published.
        await sync_agent_profile(cfg, {"display_name": source.agent_name})
        current = AgentConfig.load(cfg.id)
        if current.runtime != cfg.runtime or current.puffo_core != cfg.puffo_core:
            return
        name = source.agent_name or ""
        if current.display_name != name:
            current.display_name = name
            current.save()
        self._published[cfg.id] = snapshot

    async def run_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            ids = set(discover_agents())
            self._published = {key: value for key, value in self._published.items() if key in ids}
            for agent_id in sorted(ids):
                if stop.is_set():
                    return
                try:
                    cfg = AgentConfig.load(agent_id)
                    if cfg.puffo_core.is_configured():
                        async with asyncio.timeout(10):
                            await self.sync_one(cfg)
                except Exception as exc:  # noqa: BLE001 — isolate each identity; retry next pass
                    logger.warning("agent %s: LingTai name sync failed: %s", agent_id, exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except TimeoutError:
                pass
