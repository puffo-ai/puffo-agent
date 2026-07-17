"""PUF-273: spawn-time provenance GC + cli-docker reject + hermes
early-return.

Covers the three follow-up items deferred from PUF-268:

  (a) ``prune_stale_desired_skills`` removes only desired-installed-
      only skill dirs whose ids no longer appear in the current
      desired list. host-synced and agent-installed markers win.
  (b) The cli-docker branch in ``portal.worker._build_adapter``
      raises when an agent.yml carries non-empty desired_skills /
      desired_mcps.
  (c) ``install_desired`` early-returns for harness=hermes without
      writing any skills or MCPs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.adapters.desired_install import (
    DESIRED_INSTALLED_MARKER,
    install_desired,
    prune_stale_desired_skills,
)


# ─── (a) prune_stale_desired_skills ─────────────────────────────────────────


def _make_skill_dir(root: Path, name: str, *markers: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    for marker in markers:
        (d / marker).write_text("body\n", encoding="utf-8")
    return d


def test_prune_removes_stale_desired_only_marker(tmp_path):
    root = tmp_path / "skills"
    keep = _make_skill_dir(root, "keep", DESIRED_INSTALLED_MARKER)
    stale = _make_skill_dir(root, "stale", DESIRED_INSTALLED_MARKER)

    pruned = prune_stale_desired_skills(root, ["keep"])

    assert pruned == 1
    assert keep.is_dir()
    assert not stale.exists()


def test_prune_keeps_host_synced(tmp_path):
    root = tmp_path / "skills"
    both = _make_skill_dir(root, "both", DESIRED_INSTALLED_MARKER, "host-synced.md")

    pruned = prune_stale_desired_skills(root, [])

    assert pruned == 0
    assert both.is_dir()
    assert (both / DESIRED_INSTALLED_MARKER).exists()
    assert (both / "host-synced.md").exists()


def test_prune_keeps_agent_installed(tmp_path):
    root = tmp_path / "skills"
    both = _make_skill_dir(root, "both", DESIRED_INSTALLED_MARKER, "agent-installed.md")

    pruned = prune_stale_desired_skills(root, [])

    assert pruned == 0
    assert both.is_dir()


def test_prune_ignores_dirs_without_desired_marker(tmp_path):
    root = tmp_path / "skills"
    host_only = _make_skill_dir(root, "host-only", "host-synced.md")

    pruned = prune_stale_desired_skills(root, [])

    assert pruned == 0
    assert host_only.is_dir()


def test_prune_returns_zero_when_root_missing(tmp_path):
    assert prune_stale_desired_skills(tmp_path / "nonexistent", []) == 0


def test_prune_ignores_loose_files(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "loose.md").write_text("not a skill dir\n")
    assert prune_stale_desired_skills(root, []) == 0
    assert (root / "loose.md").exists()


def test_prune_tolerates_rmtree_oserror(tmp_path, monkeypatch, caplog):
    """A dir that fails to rmtree is logged + skipped, not fatal — the
    rest still prune."""
    from puffo_agent.agent.adapters import desired_install as di
    root = tmp_path / "skills"
    bad = _make_skill_dir(root, "bad", DESIRED_INSTALLED_MARKER)
    good = _make_skill_dir(root, "good", DESIRED_INSTALLED_MARKER)

    real_rmtree = di.shutil.rmtree

    def _rmtree(p, *a, **kw):
        if Path(p).name == "bad":
            raise OSError("permission denied")
        return real_rmtree(p, *a, **kw)

    monkeypatch.setattr(di.shutil, "rmtree", _rmtree)
    with caplog.at_level(logging.WARNING):
        pruned = prune_stale_desired_skills(root, [])

    assert pruned == 1          # only "good" removed
    assert bad.is_dir()         # "bad" left in place after the failure
    assert not good.exists()
    assert any("rmtree failed" in r.message for r in caplog.records)


# ─── (a) GC fires from install_desired after the install loop ───────────────


class _FakeHttpEmpty:
    """install_desired hits no templates → exercises only the prune pass."""
    async def get(self, path):
        from puffo_agent.crypto.http_client import HttpError
        raise HttpError(404, "not found")

    async def close(self):
        pass


def test_install_desired_invokes_prune_on_claude(tmp_path):
    agent_home = tmp_path / "agent_home"
    skills = agent_home / ".claude" / "skills"
    stale = _make_skill_dir(skills, "stale", DESIRED_INSTALLED_MARKER)

    asyncio.new_event_loop().run_until_complete(
        install_desired(
            http=_FakeHttpEmpty(),
            agent_home=agent_home,
            workspace_dir=tmp_path / "ws",
            agent_id="t-agent",
            harness_name="claude-code",
            desired_skills=[],
            desired_mcps=[],
        ),
    )

    assert not stale.exists()


def test_install_desired_invokes_prune_on_codex_workspace_path(tmp_path):
    workspace_dir = tmp_path / "ws"
    skills = workspace_dir / ".agents" / "skills"
    stale = _make_skill_dir(skills, "stale", DESIRED_INSTALLED_MARKER)

    asyncio.new_event_loop().run_until_complete(
        install_desired(
            http=_FakeHttpEmpty(),
            agent_home=tmp_path / "agent_home",
            workspace_dir=workspace_dir,
            agent_id="t-agent",
            harness_name="codex",
            desired_skills=[],
            desired_mcps=[],
        ),
    )

    assert not stale.exists()


def test_install_desired_prune_does_not_remove_freshly_installed(tmp_path):
    """Round-trip: id was previously installed, stays in the current
    desired list — the prune pass must not nuke it."""
    agent_home = tmp_path / "agent_home"
    skills = agent_home / ".claude" / "skills"
    kept = _make_skill_dir(skills, "fresh", DESIRED_INSTALLED_MARKER)

    asyncio.new_event_loop().run_until_complete(
        install_desired(
            http=_FakeHttpEmpty(),
            agent_home=agent_home,
            workspace_dir=tmp_path / "ws",
            agent_id="t-agent",
            harness_name="claude-code",
            desired_skills=["fresh"],
            desired_mcps=[],
        ),
    )

    assert kept.is_dir()
    assert (kept / DESIRED_INSTALLED_MARKER).exists()


# ─── (c) hermes harness early-return in install_desired ─────────────────────


def test_install_desired_hermes_early_returns_no_writes(tmp_path, caplog):
    agent_home = tmp_path / "agent_home"
    workspace_dir = tmp_path / "ws"

    class _CrashingHttp:
        async def get(self, path):  # pragma: no cover
            raise AssertionError("hermes branch must short-circuit before HTTP")
        async def close(self):
            pass

    with caplog.at_level(logging.INFO, logger="puffo_agent.agent.adapters.desired_install"):
        extras = asyncio.new_event_loop().run_until_complete(
            install_desired(
                http=_CrashingHttp(),
                agent_home=agent_home,
                workspace_dir=workspace_dir,
                agent_id="t-hermes",
                harness_name="hermes",
                desired_skills=["s1", "s2"],
                desired_mcps=["m1"],
            ),
        )

    assert extras == {}
    assert not (agent_home / ".claude" / "skills").exists()
    assert not (workspace_dir / ".agents" / "skills").exists()
    # Info log carries verbatim id counts so an operator inspecting
    # daemon logs can see why their picker selections were dropped.
    assert any(
        "hermes harness" in r.message and "2 desired_skills" in r.message
        for r in caplog.records
    ), caplog.records


def test_install_desired_hermes_empty_lists_no_log(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="puffo_agent.agent.adapters.desired_install"):
        extras = asyncio.new_event_loop().run_until_complete(
            install_desired(
                http=_FakeHttpEmpty(),
                agent_home=tmp_path / "agent_home",
                workspace_dir=tmp_path / "ws",
                agent_id="t-hermes",
                harness_name="hermes",
                desired_skills=[],
                desired_mcps=[],
            ),
        )

    assert extras == {}
    # Empty list + hermes → silent. No log noise on every spawn.
    assert not any(
        "hermes harness" in r.message for r in caplog.records
    )


# ─── (b) cli-docker reject at worker._build_adapter ─────────────────────────


def _make_agent_cfg(
    *,
    runtime_kind: str,
    desired_skills: list[str] | None = None,
    desired_mcps: list[str] | None = None,
):
    """Minimal stub: only the fields the cli-docker reject gate reads."""
    from types import SimpleNamespace
    runtime = SimpleNamespace(
        kind=runtime_kind,
        harness="claude-code",
        model="",
        permission_mode="bypassPermissions",
        inference_level="",
        docker_image="",
        docker_memory_limit="",
        docker_memory_reservation="",
    )
    puffo_core = SimpleNamespace(
        server_url="",
        slug="",
        device_id="",
        space_id="",
        is_configured=lambda: False,
    )
    return SimpleNamespace(
        id="t-agent",
        runtime=runtime,
        desired_skills=desired_skills or [],
        desired_mcps=desired_mcps or [],
        puffo_core=puffo_core,
        resolve_workspace_dir=lambda: Path("/tmp/ws"),
        resolve_claude_dir=lambda: Path("/tmp/ws/.claude"),
    )


def _make_daemon_cfg():
    from types import SimpleNamespace
    return SimpleNamespace(
        google=SimpleNamespace(api_key=""),
        anthropic=SimpleNamespace(model=""),
        openai=SimpleNamespace(model=""),
        docker_memory_limit="",
        docker_memory_reservation="",
        data_service=SimpleNamespace(port=63388),
        rpc_service=SimpleNamespace(port=63389),
    )


def test_build_adapter_cli_docker_installs_desired_skills(monkeypatch):
    """desired_skills no longer reject on cli-docker — they install into
    the bind-mounted .claude/skills/. The adapter must receive both the
    skills and the puffo_core install wiring."""
    from puffo_agent.portal.worker import build_adapter
    from puffo_agent.agent.adapters import docker_cli as dc
    from puffo_agent.agent import harness

    captured: dict = {}

    class _Stub:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(dc, "DockerCLIAdapter", _Stub)

    class _Harness:
        def name(self) -> str:
            return "claude-code"

    monkeypatch.setattr(harness, "build_harness", lambda _: _Harness())

    agent_cfg = _make_agent_cfg(
        runtime_kind="cli-docker", desired_skills=["s1", "s2"],
    )
    build_adapter(_make_daemon_cfg(), agent_cfg)  # no RuntimeError
    assert captured.get("desired_skills") == ["s1", "s2"]
    assert "puffo_core_keys_dir" in captured


def test_build_adapter_cli_docker_rejects_non_empty_desired_mcps():
    from puffo_agent.portal.worker import build_adapter

    agent_cfg = _make_agent_cfg(
        runtime_kind="cli-docker", desired_mcps=["m1"],
    )
    with pytest.raises(RuntimeError) as ei:
        build_adapter(_make_daemon_cfg(), agent_cfg)
    assert "cli-docker" in str(ei.value)
    assert "desired_mcps" in str(ei.value)


def test_build_adapter_cli_docker_empty_desired_does_not_reject(monkeypatch):
    """Reject gate must not fire when the lists are empty — that's the
    cli-docker happy path operators have today."""
    from puffo_agent.portal.worker import build_adapter
    from puffo_agent.agent.adapters import docker_cli as dc
    from puffo_agent.agent import harness

    captured: dict = {}

    class _Stub:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(dc, "DockerCLIAdapter", _Stub)

    class _Harness:
        def name(self) -> str:
            return "claude-code"

    monkeypatch.setattr(harness, "build_harness", lambda _: _Harness())

    agent_cfg = _make_agent_cfg(runtime_kind="cli-docker")
    build_adapter(_make_daemon_cfg(), agent_cfg)
    # Reaching here without RuntimeError is the assertion. Cheap
    # tail-check that the stub adapter actually saw the agent_id so
    # we know the code path executed past the reject gate.
    assert captured.get("agent_id") == "t-agent"


@pytest.mark.asyncio
async def test_docker_install_desired_skills_passes_skills_only(
    monkeypatch, tmp_path,
):
    """The docker adapter installs skills but never MCPs — MCPs are
    gated out upstream, so it always calls run_spawn_install with an
    empty desired_mcps list."""
    from puffo_agent.agent.adapters import desired_install
    from puffo_agent.agent.adapters.docker_cli import DockerCLIAdapter

    calls: dict = {}

    async def _fake_run(**kw):
        calls.update(kw)
        return {}

    monkeypatch.setattr(desired_install, "run_spawn_install", _fake_run)

    adapter = DockerCLIAdapter(
        agent_id="t",
        model="",
        image="img",
        workspace_dir=str(tmp_path),
        claude_dir=str(tmp_path / ".claude"),
        session_file=str(tmp_path / "s.json"),
        agent_home_dir=str(tmp_path),
        shared_fs_dir=str(tmp_path),
        desired_skills=["s1"],
        puffo_core_server_url="u",
        puffo_core_slug="sl",
        puffo_core_keys_dir=str(tmp_path / "keys"),
    )
    await adapter._install_desired_skills()
    assert calls["desired_skills"] == ["s1"]
    assert calls["desired_mcps"] == []
    # idempotent — a second call is a no-op
    calls.clear()
    await adapter._install_desired_skills()
    assert calls == {}


@pytest.mark.asyncio
async def test_run_spawn_install_tolerates_install_crash(monkeypatch, caplog):
    """A crash inside install_desired is logged + swallowed (spawn
    continues), the http client is closed, and {} is returned."""
    from puffo_agent.agent.adapters import desired_install as di
    import puffo_agent.crypto.http_client as hc
    import puffo_agent.crypto.keystore as ks_mod

    closed = {"v": False}

    class _Http:
        def __init__(self, *a, **kw):
            pass

        async def close(self):
            closed["v"] = True

    class _KS:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(hc, "PuffoCoreHttpClient", _Http)
    monkeypatch.setattr(ks_mod, "KeyStore", _KS)

    async def _boom(**kw):
        raise RuntimeError("install blew up")

    monkeypatch.setattr(di, "install_desired", _boom)

    with caplog.at_level(logging.WARNING):
        out = await di.run_spawn_install(
            agent_id="a",
            agent_home=Path("/x"),
            workspace_dir=Path("/x"),
            harness_name="claude-code",
            desired_skills=["s1"],
            desired_mcps=[],
            server_url="u",
            slug="sl",
            keys_dir="k",
        )

    assert out == {}
    assert closed["v"] is True
    assert any("install pass crashed" in r.message for r in caplog.records)
