"""OpenCode model access is the credential-aware readiness authority."""

from __future__ import annotations

import subprocess

import pytest

from puffo_agent.agent.opencode_auth import (
    OpenCodeModel,
    OpenCodeProbeError,
    _is_model_id,
    list_opencode_model_catalog,
    list_opencode_models,
    opencode_model_is_available,
    opencode_model_status,
)


def _completed(*, code: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout=stdout, stderr=stderr,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("opencode/gpt-5.6", True),
        ("openrouter/qwen/qwen3-max", True),
        ('{"id":"a/b"}', False),
        ('"npm":"@ai-sdk/openai-compatible"', False),
        (" opencode/indented", False),
        ("opencode/x,y", False),
    ],
)
def test_model_id_requires_a_bare_non_json_line(value, expected):
    assert _is_model_id(value) is expected


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


def test_verbose_probe_keeps_blockless_models_after_malformed_metadata(
    monkeypatch,
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            code=0,
            stdout=(
                "opencode/a\n"
                '{"id":"a","api":{"npm":"x"\n'
                "opencode/b\n"
                "opencode/c\n"
                '{"variants":{"low":{}}}\n'
            ),
        ),
    )

    assert list_opencode_model_catalog("/opt/bin/opencode") == (
        OpenCodeModel("opencode/a"),
        OpenCodeModel("opencode/b"),
        OpenCodeModel("opencode/c", ("low",)),
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


@pytest.mark.parametrize("outcome", ["success", "error", "timeout"])
def test_probe_removes_its_temporary_files_without_touching_parent(
    monkeypatch, tmp_path, outcome,
):
    """Every probe must reclaim CLI extraction files, including failed probes."""
    from pathlib import Path
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(tmp_path))
    sentinel = tmp_path / "unrelated.so"
    sentinel.write_bytes(b"keep")
    directories = []

    def fake_run(command, **kwargs):
        directory = Path(kwargs["env"]["TMPDIR"])
        directories.append(directory)
        (directory / "extracted.so").write_bytes(b"library")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 5)
        return _completed(code=1 if outcome == "error" else 0, stdout="a/b\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    if outcome == "success":
        assert list_opencode_models("opencode") == ("a/b",)
    else:
        with pytest.raises(OpenCodeProbeError):
            list_opencode_models("opencode")
    assert directories and all(not path.exists() for path in directories)
    assert sentinel.read_bytes() == b"keep"
    assert list(tmp_path.iterdir()) == [sentinel]


def test_discovery_reuses_probe_but_preflight_stays_fresh(monkeypatch):
    """Heartbeat readiness and picker must share one probe; admission is live."""
    from puffo_agent.agent import cli_bin, model_catalog

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(code=0, stdout="a/b\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(cli_bin, "resolve_opencode_bin", lambda: "/test/discovery")
    monkeypatch.setattr(model_catalog, "_cache", {})
    for _ in range(3):
        assert cli_bin.opencode_has_accessible_models()
        assert model_catalog.provider_models("opencode", fetch=True)[1].id == "a/b"
    assert calls == [["/test/discovery", "models", "--verbose"]]
    assert opencode_model_status("/test/discovery", "a/b") == "ready"
    assert calls[-1] == ["/test/discovery", "models", "a"]
    assert len(calls) == 2


def test_discovery_refreshes_on_expiry_auth_edit_and_failure(monkeypatch, tmp_path):
    """Long-lived discovery must see login changes and back off failed probes."""
    from puffo_agent.agent import opencode_auth as auth

    monkeypatch.setattr(auth, "_discovery_cache", {})
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    now = [0.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    calls = []
    fail = [False]

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(code=2 if fail[0] else 0, stdout="a/b\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    auth.discover_opencode_models("test")
    now[0] = 299
    auth.discover_opencode_models("test")
    assert len(calls) == 1
    now[0] = 300
    auth.discover_opencode_models("test")
    assert len(calls) == 2
    path = tmp_path / "opencode/auth.json"
    path.parent.mkdir()
    path.write_text("{}")
    auth.discover_opencode_models("test")
    assert len(calls) == 3
    path.unlink()
    fail[0] = True
    now[0] = 301
    for _ in range(3):
        with pytest.raises(OpenCodeProbeError):
            auth.discover_opencode_models("test")
    assert len(calls) == 5  # verbose, then compatibility fallback, once
    now[0] = 332
    fail[0] = False
    assert auth.discover_opencode_models("test") == (OpenCodeModel("a/b"),)
    assert len(calls) == 6


def test_simultaneous_discovery_shares_one_probe(monkeypatch):
    """Concurrent UI/heartbeat refreshes cannot duplicate an expensive probe."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from puffo_agent.agent import opencode_auth as auth

    monkeypatch.setattr(auth, "_discovery_cache", {})
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        entered.set()
        assert release.wait(5)
        return _completed(code=0, stdout="a/b\n")

    def second():
        second_started.set()
        return auth.discover_opencode_models("concurrent")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(auth.discover_opencode_models, "concurrent")
        assert entered.wait(5)
        other = pool.submit(second)
        assert second_started.wait(5)
        release.set()
        assert first.result() == other.result() == (OpenCodeModel("a/b"),)
    assert len(calls) == 1


@pytest.mark.parametrize("relative", [
    "config/opencode/config.json", "config/opencode/config",
    "config/opencode/opencode.jsonc", "project/.opencode/opencode.json",
    "project/.opencode/opencode.jsonc", "home/.opencode/opencode.json",
    "managed/opencode.json", "managed/opencode.jsonc", "custom.json",
    "custom-dir/opencode.jsonc",
])
def test_discovery_invalidates_native_config_create_edit_delete(monkeypatch, tmp_path, relative):
    """Native config edits must not leave readiness on the pre-edit model set."""
    from puffo_agent.agent import opencode_auth as auth

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(auth, "_discovery_cache", {})
    monkeypatch.setattr(auth, "build_child_environment", lambda: {
        "HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "OPENCODE_TEST_MANAGED_CONFIG_DIR": str(tmp_path / "managed"),
        "OPENCODE_CONFIG": str(tmp_path / "custom.json"),
        "OPENCODE_CONFIG_DIR": str(tmp_path / "custom-dir"),
    })
    path = tmp_path / relative
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        model = path.read_text() if path.exists() else "a/missing"
        return _completed(code=0, stdout=model + "\n")

    monkeypatch.setattr(subprocess, "run", probe)
    assert auth.discover_opencode_models("test") == (OpenCodeModel("a/missing"),)
    path.parent.mkdir(parents=True, exist_ok=True)
    for value in ("a/created", "a/edited-longer"):
        path.write_text(value)
        assert auth.discover_opencode_models("test") == (OpenCodeModel(value),)
    path.unlink()
    assert auth.discover_opencode_models("test") == (OpenCodeModel("a/missing"),)
    assert len(calls) == 4
