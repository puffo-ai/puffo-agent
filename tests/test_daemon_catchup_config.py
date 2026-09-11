"""Regression coverage for the daemon catch-up configuration contract."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
import yaml

from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.crypto.keystore import KeyStore, StoredIdentity, encode_secret
from puffo_agent.limits import DEFAULT_CATCHUP_STALE_HOURS
from puffo_agent.portal import profile_sync
from puffo_agent.portal.daemon import Daemon
from puffo_agent.portal.state import (
    AgentConfig,
    DaemonConfig,
    PuffoCoreConfig,
    RuntimeConfig,
)
from puffo_agent.portal.worker import _build_puffo_core_client


def test_fresh_daemon_config_uses_canonical_default() -> None:
    assert DEFAULT_CATCHUP_STALE_HOURS == 48.0
    assert DaemonConfig().catchup_stale_hours == DEFAULT_CATCHUP_STALE_HOURS


def test_legacy_daemon_config_without_key_uses_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    (tmp_path / "daemon.yml").write_text(
        "default_provider: anthropic\n",
        encoding="utf-8",
    )

    loaded = DaemonConfig.load()

    assert loaded.catchup_stale_hours == DEFAULT_CATCHUP_STALE_HOURS


def test_explicit_daemon_config_value_loads_as_float(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    (tmp_path / "daemon.yml").write_text(
        "catchup_stale_hours: 12.5\n",
        encoding="utf-8",
    )

    loaded = DaemonConfig.load()

    assert isinstance(loaded.catchup_stale_hours, float)
    assert loaded.catchup_stale_hours == 12.5


def test_explicit_daemon_config_value_saves_and_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    config = DaemonConfig(catchup_stale_hours=12.5)

    config.save()

    raw = yaml.safe_load((tmp_path / "daemon.yml").read_text(encoding="utf-8"))
    assert raw["catchup_stale_hours"] == 12.5
    assert DaemonConfig.load().catchup_stale_hours == 12.5


def _configured_agent(agent_id: str) -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        state="running",
        puffo_core=PuffoCoreConfig(
            server_url="http://127.0.0.1:9",
            slug=f"{agent_id}-slug",
            device_id=f"{agent_id}-device",
            space_id=f"{agent_id}-space",
        ),
        runtime=RuntimeConfig(kind="ws-local"),
    )


def _save_ws_local_identity(config: AgentConfig) -> None:
    secret = encode_secret(b"w" * 32)
    KeyStore.for_agent(config.id).save_identity(
        StoredIdentity(
            slug=config.puffo_core.slug,
            device_id=config.puffo_core.device_id,
            root_secret_key=secret,
            device_signing_secret_key=secret,
            kem_secret_key=secret,
            server_url=config.puffo_core.server_url,
        )
    )


async def _assert_real_worker_builder_threads_catchup_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    daemon_config = DaemonConfig(catchup_stale_hours=12.5)
    agent_config = _configured_agent("builder-agent")

    client = cast(
        PuffoCoreMessageClient,
        _build_puffo_core_client(
            agent_config,
            agent_config.id,
            daemon_cfg=daemon_config,
        ),
    )
    try:
        assert isinstance(client, PuffoCoreMessageClient)
        assert client._catchup_stale_ms == 45_000_000
    finally:
        await client.stop()


def test_real_worker_builder_threads_catchup_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _assert_real_worker_builder_threads_catchup_threshold(
            tmp_path, monkeypatch,
        )
    )


async def _assert_real_reconcile_initializes_multiple_ws_local_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    async def no_profile_sync(_config: AgentConfig) -> None:
        return None

    monkeypatch.setattr(profile_sync, "sync_full_profile", no_profile_sync)

    agent_configs = [_configured_agent("alpha"), _configured_agent("bravo")]
    for agent_config in agent_configs:
        _save_ws_local_identity(agent_config)
        agent_config.save()

    daemon_config = DaemonConfig(catchup_stale_hours=12.5)
    daemon = Daemon(daemon_config)
    try:
        await daemon._reconcile_once()

        assert set(daemon.workers) == {"alpha", "bravo"}
        for agent_config in agent_configs:
            worker = daemon.workers[agent_config.id]
            assert worker.runtime.status == "running"
            assert worker.runtime.error == ""
            assert "AttributeError" not in worker.runtime.error
            client = worker._client
            assert isinstance(client, PuffoCoreMessageClient)
            assert client._catchup_stale_ms == 45_000_000
    finally:
        await daemon._stop_all_workers()


def test_real_reconcile_initializes_multiple_ws_local_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _assert_real_reconcile_initializes_multiple_ws_local_workers(
            tmp_path, monkeypatch,
        )
    )


@pytest.mark.asyncio
async def test_daemon_observes_worker_start_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    daemon = Daemon(DaemonConfig())
    worker = SimpleNamespace(
        wait_warm=AsyncMock(return_value=False),
        runtime=SimpleNamespace(status="error"),
    )

    with caplog.at_level(logging.WARNING):
        await daemon._observe_worker_start("broken-agent", worker)

    worker.wait_warm.assert_awaited_once_with(
        timeout=daemon._warm_serialise_timeout,
    )
    assert "broken-agent: worker did not reach running" in caplog.text
    assert "status=error" in caplog.text


async def _assert_reconcile_replaces_post_start_fatal_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))

    async def no_profile_sync(_config: AgentConfig) -> None:
        return None

    monkeypatch.setattr(profile_sync, "sync_full_profile", no_profile_sync)
    config = _configured_agent("restart-agent")
    _save_ws_local_identity(config)
    config.save()
    daemon = Daemon(DaemonConfig())
    try:
        await daemon._reconcile_once()
        failed = daemon.workers[config.id]
        failed._restart_required = True
        assert failed._task is not None
        failed._task.cancel()
        await asyncio.gather(failed._task, return_exceptions=True)

        await daemon._reconcile_once()

        replacement = daemon.workers[config.id]
        assert replacement is not failed
        assert replacement.runtime.status == "running"
        assert failed._client is None
    finally:
        await daemon._stop_all_workers()


def test_reconcile_replaces_post_start_fatal_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _assert_reconcile_replaces_post_start_fatal_worker(tmp_path, monkeypatch)
    )
