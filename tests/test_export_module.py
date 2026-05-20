"""Unit tests for portal.export — pack/unpack roundtrip + AES-GCM
auth + sanitize + missing-agent errors."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from _bridge_support import isolated_home, write_test_agent


@pytest.fixture(autouse=True)
def fresh_home():
    home = isolated_home()
    yield home


def _seed(home: str, agent_id: str, extra_files: dict[str, str] | None = None) -> Path:
    workspace = write_test_agent(home, agent_id)
    adir = Path(home) / "agents" / agent_id
    for rel, contents in (extra_files or {}).items():
        target = adir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents.encode("utf-8"))
    return adir


def test_pack_requires_agents():
    from puffo_agent.portal import export as exp

    with pytest.raises(exp.ExportError):
        exp.pack([], password="hunter2")


def test_pack_requires_password():
    from puffo_agent.portal import export as exp

    _seed(os.environ["PUFFO_AGENT_HOME"], "alpha")
    with pytest.raises(exp.ExportError):
        exp.pack(["alpha"], password="")


def test_pack_missing_agent_lists_them():
    from puffo_agent.portal import export as exp

    _seed(os.environ["PUFFO_AGENT_HOME"], "alpha")
    with pytest.raises(exp.ExportError, match="ghost"):
        exp.pack(["alpha", "ghost"], password="hunter2")


def test_pack_unpack_roundtrip_single():
    from puffo_agent.portal import export as exp

    _seed(
        os.environ["PUFFO_AGENT_HOME"],
        "alpha",
        extra_files={"memory/notes.md": "hello-memory"},
    )
    blob = exp.pack(["alpha"], password="hunter2", exported_by_slug="op-1")
    assert blob.startswith(exp.MAGIC)
    bundle = exp.unpack(blob, password="hunter2")
    assert bundle.manifest["format_version"] == 1
    assert bundle.manifest["exported_by_slug"] == "op-1"
    assert [e["id"] for e in bundle.manifest["agents"]] == ["alpha"]
    assert "agent.yml" in bundle.agents["alpha"]
    assert bundle.agents["alpha"]["memory/notes.md"] == b"hello-memory"


def test_pack_unpack_roundtrip_multi():
    from puffo_agent.portal import export as exp

    _seed(os.environ["PUFFO_AGENT_HOME"], "alpha")
    _seed(os.environ["PUFFO_AGENT_HOME"], "beta")
    blob = exp.pack(["alpha", "beta"], password="hunter2")
    bundle = exp.unpack(blob, password="hunter2")
    assert set(bundle.agents.keys()) == {"alpha", "beta"}


def test_unpack_wrong_password():
    from puffo_agent.portal import export as exp

    _seed(os.environ["PUFFO_AGENT_HOME"], "alpha")
    blob = exp.pack(["alpha"], password="hunter2")
    with pytest.raises(exp.ImportPackError, match="decryption"):
        exp.unpack(blob, password="wrong")


def test_unpack_bad_magic():
    from puffo_agent.portal import export as exp

    with pytest.raises(exp.ImportPackError, match="too short|bad magic"):
        exp.unpack(b"not-a-puffo-bundle", password="hunter2")
    junk = b"X" * (len(exp.MAGIC) + exp.SALT_LEN + exp.NONCE_LEN + 32)
    with pytest.raises(exp.ImportPackError, match="bad magic"):
        exp.unpack(junk, password="hunter2")


def test_unpack_tampered_header():
    from puffo_agent.portal import export as exp

    _seed(os.environ["PUFFO_AGENT_HOME"], "alpha")
    blob = bytearray(exp.pack(["alpha"], password="hunter2"))
    # Flip a byte in the salt — AEAD AAD covers it, so decrypt fails.
    blob[len(exp.MAGIC)] ^= 0xFF
    with pytest.raises(exp.ImportPackError):
        exp.unpack(bytes(blob), password="hunter2")


def test_sanitize_drops_device_bound_files():
    from puffo_agent.portal import export as exp

    home = os.environ["PUFFO_AGENT_HOME"]
    adir = _seed(home, "alpha", extra_files={
        "runtime.json": '{"status":"running"}',
        "cli_session.json": '{"id":"x"}',
        "messages.db": "binary-blob",
        ".puffo-agent/restart.flag": "requested",
        "workspace/.claude/.credentials.json": "{}",
        "workspace/skills/keep.md": "skill",
    })
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "stage"
        stage.mkdir()
        for src in adir.rglob("*"):
            if src.is_file():
                rel = src.relative_to(adir)
                target = stage / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(src.read_bytes())
        exp.sanitize_staged_agent(stage)
        assert not (stage / "runtime.json").exists()
        assert not (stage / "cli_session.json").exists()
        assert not (stage / "messages.db").exists()
        assert not (stage / ".puffo-agent" / "restart.flag").exists()
        assert not (stage / "workspace" / ".claude" / ".credentials.json").exists()
        assert (stage / "workspace" / "skills" / "keep.md").exists()
        assert (stage / "agent.yml").exists()


def test_unpack_rejects_missing_agent_yml():
    from puffo_agent.portal import export as exp

    home = os.environ["PUFFO_AGENT_HOME"]
    _seed(home, "alpha")
    # Tamper: delete agent.yml after seeding, before packing.
    (Path(home) / "agents" / "alpha" / "agent.yml").unlink()
    with pytest.raises(exp.ExportError):
        exp.pack(["alpha"], password="hunter2")
