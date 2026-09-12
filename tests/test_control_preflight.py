"""Create-time Pi readiness checks stay fast, structured, and source-aligned."""

from __future__ import annotations

from pathlib import Path

import pytest

from puffo_agent.agent.pi_auth import PiAuthResult
from puffo_agent.portal.control import preflight
from puffo_agent.portal.control.provision import ProvisionError
from puffo_agent.portal.state import RuntimeConfig


def _runtime(
    *,
    provider: str = "anthropic",
    harness: str = "pi",
    model: str = "claude-sonnet-4-6",
):
    return RuntimeConfig(
        kind="cli-local",
        provider=provider,
        harness=harness,
        model=model,
    )


@pytest.mark.asyncio
async def test_pi_preflight_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(preflight, "resolve_pi_bin", lambda: None)

    with pytest.raises(ProvisionError) as caught:
        await preflight.preflight_runtime(_runtime())

    assert caught.value.reason == "Pi is not installed"
    assert caught.value.fields == {
        "error_code": "harness_not_ready",
        "harness": "pi",
        "reason": "not_installed",
    }


@pytest.mark.asyncio
async def test_pi_preflight_uses_qualified_model_as_authoritative_target(
    tmp_path, monkeypatch,
):
    seen = {}
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(preflight, "resolve_pi_bin", lambda: "/opt/bin/pi")

    def fake_check(executable, **kwargs):
        seen.update(executable=executable, **kwargs)
        return PiAuthResult(status="ready", provider="openai")

    monkeypatch.setattr(preflight, "check_pi_auth", fake_check)

    await preflight.preflight_runtime(
        _runtime(provider="anthropic", model="openai/gpt-5.5")
    )

    assert seen == {
        "executable": "/opt/bin/pi",
        "provider": "",
        "model": "openai/gpt-5.5",
        "config_dir": tmp_path / ".pi" / "agent",
    }


@pytest.mark.asyncio
async def test_pi_preflight_returns_machine_readable_login_reason(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(preflight, "resolve_pi_bin", lambda: "/opt/bin/pi")
    monkeypatch.setattr(
        preflight,
        "check_pi_auth",
        lambda *args, **kwargs: PiAuthResult(
            status="not_ready",
            provider="anthropic",
            reason="credentials_not_configured",
        ),
    )

    with pytest.raises(ProvisionError) as caught:
        await preflight.preflight_runtime(_runtime())

    assert caught.value.reason == "Pi sign-in required"
    assert caught.value.fields == {
        "error_code": "harness_not_ready",
        "harness": "pi",
        "reason": "need_login",
        "native_reason": "credentials_not_configured",
    }


@pytest.mark.asyncio
async def test_pi_preflight_bounds_native_reason_from_harness(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(preflight, "resolve_pi_bin", lambda: "/opt/bin/pi")
    monkeypatch.setattr(
        preflight,
        "check_pi_auth",
        lambda *args, **kwargs: PiAuthResult(
            status="error",
            provider="anthropic",
            reason="x" * 201,
        ),
    )

    with pytest.raises(ProvisionError) as caught:
        await preflight.preflight_runtime(_runtime())

    assert caught.value.fields["native_reason"] == "x" * 200


@pytest.mark.asyncio
async def test_opencode_preflight_rejects_model_missing_from_native_catalog(
    monkeypatch,
):
    """A stale private model choice must fail before agent files are written."""
    monkeypatch.setattr(
        preflight, "resolve_opencode_bin", lambda: "/opt/bin/opencode",
        raising=False,
    )
    monkeypatch.setattr(
        preflight,
        "opencode_model_status",
        lambda executable, model: "model_not_available",
        raising=False,
    )

    with pytest.raises(ProvisionError) as caught:
        await preflight.preflight_runtime(
            _runtime(
                provider="deepseek",
                harness="opencode",
                model="deepseek/deepseek-v4-pro",
            )
        )

    assert caught.value.fields == {
        "error_code": "harness_not_ready",
        "harness": "opencode",
        "reason": "model_not_available",
    }
    assert str(caught.value) == (
        "OpenCode cannot access the selected model; refresh the model list "
        "or choose another model"
    )


@pytest.mark.asyncio
async def test_opencode_preflight_accepts_model_in_native_catalog(monkeypatch):
    monkeypatch.setattr(
        preflight, "resolve_opencode_bin", lambda: "/opt/bin/opencode",
    )
    monkeypatch.setattr(
        preflight,
        "opencode_model_status",
        lambda executable, model: "ready",
    )

    await preflight.preflight_runtime(
        _runtime(
            provider="deepseek",
            harness="opencode",
            model="deepseek/deepseek-v4-pro",
        )
    )


@pytest.mark.asyncio
async def test_opencode_preflight_reports_missing_provider_as_login(monkeypatch):
    monkeypatch.setattr(
        preflight, "resolve_opencode_bin", lambda: "/opt/bin/opencode",
    )
    monkeypatch.setattr(
        preflight,
        "opencode_model_status",
        lambda executable, model: "need_login",
    )

    with pytest.raises(ProvisionError) as caught:
        await preflight.preflight_runtime(
            _runtime(
                provider="deepseek",
                harness="opencode",
                model="deepseek/deepseek-v4-pro",
            )
        )

    assert caught.value.fields["reason"] == "need_login"
    assert str(caught.value) == "OpenCode sign-in required"


@pytest.mark.asyncio
async def test_docker_preflight_rejects_missing_binary(monkeypatch):
    monkeypatch.setattr(preflight, "resolve_docker_bin", lambda: None)
    runtime = RuntimeConfig(kind="cli-docker", harness="claude-code")

    with pytest.raises(ProvisionError) as caught:
        await preflight.preflight_runtime(runtime, agent_id="docker-agent")

    assert caught.value.fields == {
        "error_code": "runtime_not_ready",
        "runtime_kind": "cli-docker",
        "reason": "not_installed",
    }


@pytest.mark.asyncio
async def test_docker_preflight_rejects_unavailable_daemon(monkeypatch):
    seen = {}
    monkeypatch.setattr(preflight, "resolve_docker_bin", lambda: "/opt/docker")

    async def unavailable(docker_bin, container_name):
        seen.update(docker_bin=docker_bin, container_name=container_name)
        return None

    monkeypatch.setattr(preflight, "container_state", unavailable)
    runtime = RuntimeConfig(kind="cli-docker", harness="claude-code")

    with pytest.raises(ProvisionError) as caught:
        await preflight.preflight_runtime(runtime, agent_id="docker-agent")

    assert caught.value.fields["reason"] == "daemon_unavailable"
    assert seen == {
        "docker_bin": "/opt/docker",
        "container_name": "puffo-runtime-preflight-docker-agent",
    }


@pytest.mark.asyncio
async def test_docker_preflight_reuses_container_probe(monkeypatch):
    monkeypatch.setattr(preflight, "resolve_docker_bin", lambda: "/opt/docker")

    async def available(_docker_bin, _container_name):
        return ""

    monkeypatch.setattr(preflight, "container_state", available)

    await preflight.preflight_runtime(
        RuntimeConfig(kind="cli-docker", harness="claude-code"),
        agent_id="docker-agent",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('harness', ['pi', 'opencode'])
@pytest.mark.parametrize('model,default', [('', 'unavailable'), ('explicit', 'unavailable'), ('', '')])
async def test_preflight_checks_same_effective_model_as_preparer(
    monkeypatch, tmp_path, harness, model, default,
):
    """An unavailable daemon default must fail before identity materialization."""
    from types import SimpleNamespace
    from puffo_agent.portal.state import DaemonConfig
    from puffo_agent.agent.harness.runtime.local_runtime import LocalRuntimePreparer

    cfg = DaemonConfig()
    cfg.anthropic.model = default
    monkeypatch.setattr(DaemonConfig, 'load', classmethod(lambda cls: cfg))
    monkeypatch.setattr(Path, 'home', staticmethod(lambda: tmp_path))
    monkeypatch.setattr(preflight, 'resolve_pi_bin', lambda: 'synthetic-pi')
    monkeypatch.setattr(preflight, 'resolve_opencode_bin', lambda: 'synthetic-opencode')
    seen = []

    def opencode_status(executable, selected):
        seen.append(selected)
        return 'model_not_available' if selected == 'unavailable' else 'ready'

    def pi_status(executable, **kwargs):
        seen.append(kwargs['model'])
        return PiAuthResult(
            status='not_ready' if kwargs['model'] == 'unavailable' else 'ready',
            provider='anthropic',
        )

    monkeypatch.setattr(preflight, 'opencode_model_status', opencode_status)
    monkeypatch.setattr(preflight, 'check_pi_auth', pi_status)
    runtime = _runtime(harness=harness, model=model)
    preparer = object.__new__(LocalRuntimePreparer)
    preparer.agent_cfg = SimpleNamespace(runtime=runtime)
    preparer.daemon_cfg = cfg
    preparer.provider = 'anthropic'
    preparer.harness_name = harness
    expected = preparer._resolve_model()
    if expected == 'unavailable':
        with pytest.raises(ProvisionError):
            await preflight.preflight_runtime(runtime)
    else:
        await preflight.preflight_runtime(runtime)
    assert seen == [expected]
    assert runtime.model == model
