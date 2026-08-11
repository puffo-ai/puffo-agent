"""is_encrypted threads through the read path + MCP env, so the agent can
always tell an E2EE message from a plaintext one."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.mcp.data_client import _msg_from_dict
from puffo_agent.mcp.puffo_core_tools import _enc_tag
from puffo_agent.mcp.config import puffo_core_mcp_env
from puffo_agent.portal.data_service import _msg_to_dict


def test_msg_from_dict_reads_is_encrypted():
    assert _msg_from_dict({"is_encrypted": False}).is_encrypted is False
    assert _msg_from_dict({"is_encrypted": True}).is_encrypted is True


def test_msg_from_dict_defaults_true_when_absent():
    assert _msg_from_dict({}).is_encrypted is True


def test_msg_to_dict_surfaces_is_encrypted():
    m = SimpleNamespace(
        envelope_id="e", envelope_kind="channel", sender_slug="s",
        channel_id=None, space_id=None, recipient_slug=None,
        content_type="text/plain", content="hi", sent_at=1, received_at=1,
        thread_root_id=None, reply_to_id=None, is_encrypted=False,
    )
    assert _msg_to_dict(m)["is_encrypted"] is False


def test_enc_tag():
    assert _enc_tag(SimpleNamespace(is_encrypted=True)) == "[encrypted]"
    assert _enc_tag(SimpleNamespace(is_encrypted=False)) == "[plaintext]"
    # Legacy object with no attribute → treated as encrypted.
    assert _enc_tag(SimpleNamespace()) == "[encrypted]"


def _env(**over):
    args = dict(slug="s", device_id="d", server_url="u", keystore_dir="k", workspace="w")
    args.update(over)
    return puffo_core_mcp_env(**args)


def test_mcp_env_pins_package_and_forwards_pythonpath(tmp_path, monkeypatch):
    inherited = tmp_path / "editable-src"
    monkeypatch.setenv("PYTHONPATH", str(inherited))
    entries = _env()["PYTHONPATH"].split(os.pathsep)
    assert entries[-1] == str(inherited)
    assert any(Path(entry).name == "src" for entry in entries)


def test_mcp_env_skips_pythonpath_for_docker(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "C:/host/src")
    assert "PYTHONPATH" not in _env(runtime_kind="cli-docker")


def test_mcp_env_pins_package_path_when_unset(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert Path(_env()["PYTHONPATH"]).name == "src"
