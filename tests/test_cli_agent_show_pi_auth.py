"""``agent show``: Pi credential projection surface."""

from __future__ import annotations

import base64
import json
import time

import pytest

from puffo_agent.portal.cli import main
from puffo_agent.portal.host_assets import sync_host_pi_auth_view
from puffo_agent.portal.state import agent_dir


def _access_token(exp: int) -> str:
    def segment(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment(b'{}')}.{segment(json.dumps({'exp': exp}).encode())}.sig"


def _create(agent_id: str, harness: str, provider: str = "openai") -> None:
    assert main([
        "agent", "create", "--id", agent_id,
        "--provider", provider, "--harness", harness,
    ]) == 0


def _project(tmp_path, agent_id: str, exp: int) -> None:
    host = tmp_path / "host"
    codex = host / ".codex" / "auth.json"
    codex.parent.mkdir(parents=True)
    codex.write_text(
        json.dumps({"tokens": {"access_token": _access_token(exp)}}),
        encoding="utf-8",
    )
    assert sync_host_pi_auth_view(
        host, agent_dir(agent_id) / ".pi" / "agent"
    ) == "view"


def test_show_reports_a_live_projection_with_its_expiry(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    _create("pi-live", "pi")
    exp = int(time.time()) + 3600
    _project(tmp_path, "pi-live", exp)

    assert main(["agent", "show", "pi-live"]) == 0

    out = capsys.readouterr().out
    assert "  pi_auth:" in out
    assert "    projection:  view" in out
    assert "size=" in out and "mtime=" in out
    assert time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) in out
    assert "(expired)" not in out
    assert _access_token(exp) not in out


def test_show_flags_an_elapsed_projected_credential(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    _create("pi-stale", "pi")
    _project(tmp_path, "pi-stale", int(time.time()) - 60)

    assert main(["agent", "show", "pi-stale"]) == 0
    assert "(expired)" in capsys.readouterr().out


def test_show_reports_an_operator_owned_target_without_an_expiry(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    _create("pi-operator", "pi")
    pi_home = agent_dir("pi-operator") / ".pi" / "agent"
    pi_home.mkdir(parents=True)
    (pi_home / "auth.json").write_text('{"openai-codex":{"type":"oauth"}}')

    assert main(["agent", "show", "pi-operator"]) == 0

    out = capsys.readouterr().out
    assert "    projection:  operator-owned" in out
    assert "    expires:     unknown" in out


def test_show_reports_an_unprojected_pi_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    _create("pi-bare", "pi")

    assert main(["agent", "show", "pi-bare"]) == 0

    out = capsys.readouterr().out
    assert "    projection:  not-projected" in out
    assert "    credential:  not present" in out
    assert "    expires:     unknown" in out


@pytest.mark.parametrize(
    ("harness", "provider"), [("codex", "openai"), ("claude-code", "anthropic")]
)
def test_show_omits_the_pi_block_for_other_harnesses(
    tmp_path, monkeypatch, capsys, harness, provider,
):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    _create(f"other-{harness}", harness, provider)

    assert main(["agent", "show", f"other-{harness}"]) == 0
    assert "pi_auth" not in capsys.readouterr().out
