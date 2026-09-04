"""Puffo extension profile on the ACP path: model presets + error classes.

ACP v1 cannot express model selection and gives errors as free text plus
one structured auth code. The profile rule for both: supplement natively
where a verified entry point exists, otherwise say so explicitly —
``spec.model`` must never be silently dropped, and a failed prompt must
carry a classified ``error_code`` and the agent's diagnostic text instead
of a bare ``acp_prompt_failed`` with the detail thrown away.
"""

import pytest
from acp.exceptions import RequestError

from puffo_agent.agent.harness.drivers.acp import AcpDriver, HarnessEventType
from puffo_agent.agent.harness.drivers.acp import (
    model_launch_args,
    operator_pinned_model,
)
from puffo_agent.agent.harness.driver import RuntimeSpec, TurnRef


def test_preset_supplies_verified_flag_only():
    assert model_launch_args("gemini", "gemini-2.5-pro") == ("-m", "gemini-2.5-pro")
    assert model_launch_args("/opt/bin/kimi", "k2") == ("-m", "k2")
    assert model_launch_args("GEMINI.EXE", "g") == ("-m", "g")
    # No verified row -> no guessing.
    assert model_launch_args("opencode", "some-model") == ()
    assert model_launch_args("gemini", "") == ()


def test_operator_pinned_model_spellings():
    assert operator_pinned_model(["-m", "x"])
    assert operator_pinned_model(["--model", "x"])
    assert operator_pinned_model(["--model=x"])
    assert not operator_pinned_model(["--verbose"])
    assert not operator_pinned_model([])


def _spec(**kwargs) -> RuntimeSpec:
    defaults = dict(workspace_dir="/tmp/w", executable="gemini")
    defaults.update(kwargs)
    return RuntimeSpec(**defaults)


class _CapturingFactory:
    def __init__(self):
        self.command = None

    def __call__(self, command, spec):
        self.command = command
        return object()


async def _spawn(driver: AcpDriver, spec: RuntimeSpec) -> None:
    await driver._spawn(driver._validate_launch_plan(spec))


@pytest.mark.asyncio
async def test_spawn_appends_preset_and_declares_it():
    factory = _CapturingFactory()
    driver = AcpDriver(factory)
    await _spawn(driver, _spec(model="gemini-2.5-pro", launch_args=("--acp",)))
    assert factory.command[-2:] == ("-m", "gemini-2.5-pro")
    assert driver._model_selection == "launch_preset"
    assert driver._spawn_warnings == ()


@pytest.mark.asyncio
async def test_spawn_without_preset_warns_instead_of_silently_dropping():
    factory = _CapturingFactory()
    driver = AcpDriver(factory)
    await _spawn(driver, _spec(executable="opencode", model="some-model"))
    assert "-m" not in factory.command
    assert driver._model_selection == ""
    assert len(driver._spawn_warnings) == 1
    assert "not supported" in driver._spawn_warnings[0]
    assert "some-model" in driver._spawn_warnings[0]


@pytest.mark.asyncio
async def test_spawn_defers_to_operator_pinned_model_with_warning():
    factory = _CapturingFactory()
    driver = AcpDriver(factory)
    await _spawn(
        driver,
        _spec(model="gemini-2.5-pro", launch_args=("-m", "gemini-2.0-flash")),
    )
    # Explicit config wins; the supplement must not add a second flag.
    assert factory.command.count("-m") == 1
    assert driver._model_selection == "operator_launch_args"
    assert "not applied" in driver._spawn_warnings[0]


@pytest.mark.asyncio
async def test_spawn_with_no_model_is_silent():
    factory = _CapturingFactory()
    driver = AcpDriver(factory)
    await _spawn(driver, _spec())
    assert driver._model_selection == ""
    assert driver._spawn_warnings == ()


class _RaisingConn:
    def __init__(self, exc):
        self._exc = exc

    async def prompt(self, **kwargs):
        raise self._exc


async def _failed_turn_data(exc):
    driver = AcpDriver(lambda command, spec: object())
    driver._conn = _RaisingConn(exc)
    driver._native_session_id = "s"
    finishes = []

    async def capture(turn, event_type, data):
        finishes.append((event_type, data))

    driver._finish_turn = capture
    await driver._run_prompt(TurnRef("t"), "hello")
    (event_type, data), = finishes
    assert event_type is HarnessEventType.TURN_ABANDONED
    return data


@pytest.mark.asyncio
async def test_auth_required_maps_to_authentication_not_free_text():
    data = await _failed_turn_data(RequestError.auth_required())
    assert data["error_code"] == "authentication"
    assert data["retryable"] is False
    assert "Authentication required" in data["diagnostic"]


@pytest.mark.asyncio
async def test_free_text_errors_go_through_the_shared_classifier():
    data = await _failed_turn_data(
        RequestError(-32603, "rate limit exceeded, retry later")
    )
    assert data["error_code"] == "rate_limit"
    assert data["retryable"] is True

    data = await _failed_turn_data(RequestError(-32603, "something broke"))
    assert data["error_code"] == "provider_error"
    assert data["retryable"] is False
    assert data["diagnostic"] == "something broke"


@pytest.mark.asyncio
async def test_non_request_errors_keep_generic_code_but_gain_diagnostic():
    data = await _failed_turn_data(ValueError("wat"))
    assert data["error_code"] == "acp_prompt_failed"
    assert data["retryable"] is True
    assert "ValueError" in data["diagnostic"]
