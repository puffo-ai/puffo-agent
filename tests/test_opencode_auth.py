"""OpenCode model access is the credential-aware readiness authority."""

from __future__ import annotations

import subprocess

import pytest

from puffo_agent.agent.opencode_auth import (
    OpenCodeModel,
    OpenCodeProbeError,
    list_opencode_model_catalog,
    list_opencode_models,
    opencode_model_is_available,
    opencode_model_status,
)


def _completed(*, code: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout=stdout, stderr=stderr,
    )


def test_native_model_probe_scrubs_ambient_keys(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return _completed(
            code=0,
            stdout="opencode/big-pickle\ndeepseek/deepseek-v4-pro\n",
        )

    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-reach-probe")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert list_opencode_models("/opt/bin/opencode") == (
        "opencode/big-pickle",
        "deepseek/deepseek-v4-pro",
    )
    assert seen["command"] == ["/opt/bin/opencode", "models"]
    assert "DEEPSEEK_API_KEY" not in seen["kwargs"]["env"]
    assert seen["kwargs"]["timeout"] <= 5


def test_missing_provider_is_a_clean_not_ready_result(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            code=1,
            stderr="\x1b[91mError: \x1b[0mProvider not found: deepseek\n",
        ),
    )

    assert list_opencode_models(
        "/opt/bin/opencode", provider="deepseek",
    ) == ()


def test_verbose_model_probe_parses_native_variants(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return _completed(
            code=0,
            stdout=(
                "opencode/no-variants\n"
                '{"id":"no-variants","variants":{}}\n'
                "openai/gpt-5\n"
                '{"id":"gpt-5","variants":{'
                '"minimal":{"reasoningEffort":"minimal"},'
                '"high":{"reasoningEffort":"high"}}}\n'
            ),
        )

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-probe")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert list_opencode_model_catalog("/opt/bin/opencode") == (
        OpenCodeModel("opencode/no-variants"),
        OpenCodeModel("openai/gpt-5", ("minimal", "high")),
    )
    assert seen["command"] == [
        "/opt/bin/opencode", "models", "--verbose",
    ]
    assert "OPENAI_API_KEY" not in seen["kwargs"]["env"]


def test_verbose_probe_recovers_after_malformed_metadata_without_phantom_models(
    monkeypatch,
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            code=0,
            stdout=(
                "opencode/big-pickle\n"
                '{"id":"a","api":{"npm":"@ai-sdk/openai-compatible"\n'
                "opencode/second-model\n"
                '{"variants":{"low":{}}}\n'
            ),
        ),
    )

    assert list_opencode_model_catalog("/opt/bin/opencode") == (
        OpenCodeModel("opencode/big-pickle"),
        OpenCodeModel("opencode/second-model", ("low",)),
    )


def test_verbose_probe_falls_back_to_non_verbose_models(monkeypatch):
    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        if "--verbose" in command:
            return _completed(code=1, stderr="Unknown option: --verbose")
        return _completed(
            code=0,
            stdout="opencode/big-pickle\ndeepseek/deepseek-v4-pro\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert list_opencode_model_catalog("/opt/bin/opencode") == (
        OpenCodeModel("opencode/big-pickle"),
        OpenCodeModel("deepseek/deepseek-v4-pro"),
    )
    assert seen == [
        ["/opt/bin/opencode", "models", "--verbose"],
        ["/opt/bin/opencode", "models"],
    ]


def test_selected_model_uses_provider_filtered_native_catalog(monkeypatch):
    seen = []

    def fake_list(executable, *, provider="", timeout_seconds=5.0):
        seen.append((executable, provider))
        return ("deepseek/deepseek-v4-pro",)

    monkeypatch.setattr(
        "puffo_agent.agent.opencode_auth.list_opencode_models", fake_list,
    )

    assert opencode_model_is_available(
        "/opt/bin/opencode", "deepseek/deepseek-v4-pro",
    ) is True
    assert seen == [("/opt/bin/opencode", "deepseek")]


def test_model_status_distinguishes_login_from_missing_model(monkeypatch):
    monkeypatch.setattr(
        "puffo_agent.agent.opencode_auth.list_opencode_models",
        lambda executable, *, provider="", timeout_seconds=5.0: (),
    )
    assert opencode_model_status(
        "/opt/bin/opencode", "deepseek/deepseek-v4-pro",
    ) == "need_login"

    monkeypatch.setattr(
        "puffo_agent.agent.opencode_auth.list_opencode_models",
        lambda executable, *, provider="", timeout_seconds=5.0: (
            "deepseek/deepseek-chat",
        ),
    )
    assert opencode_model_status(
        "/opt/bin/opencode", "deepseek/deepseek-v4-pro",
    ) == "model_not_available"


def test_unexpected_native_failure_is_not_misreported_as_logged_out(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _completed(code=2, stderr="database corrupt"),
    )

    with pytest.raises(OpenCodeProbeError):
        list_opencode_models("/opt/bin/opencode", provider="deepseek")
