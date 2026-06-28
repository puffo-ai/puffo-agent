"""api-puffo bundle ingestion: validates schema + materialises into
the standard agent_dir layout."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.agent.api_puffo.bundle import (
    ApiPuffoBundle,
    ingest_bundle,
    install_dir,
    materialise_agent_dir,
    sweep_install_dir,
)
from puffo_agent.agent.api_puffo.keystore import ApiPuffoKeystore
from puffo_agent.portal.state import AgentConfig, agent_dir, agent_yml_path


def _isolated_home() -> str:
    home = tempfile.mkdtemp(prefix="puffo-api-puffo-")
    os.environ["PUFFO_AGENT_HOME"] = home
    os.environ["PUFFO_HOME"] = home
    Path(home, "agents").mkdir(parents=True, exist_ok=True)
    return home


def _valid_bundle_dict(slug: str = "cloud-bot-1234") -> dict:
    return {
        "agent_slug": slug,
        "operator_slug": "user-5678",
        "device_id": "dev_cloud_xyz",
        "kem_secret_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "kem_cert": {
            "type": "device_cert",
            "version": 1,
            "device_id": "dev_cloud_xyz",
        },
        "session_token": "tok_abcdef123456",
        "puffo_cloud_server_url": "https://cloud.puffo.ai",
        "display_name": "Cloud Bot",
        "role": "Cloud-hosted helper",
        "role_short": "helper",
        "soul": "I help test the api-puffo runtime.",
        "avatar_url": "",
        "api_key": "sk-test-llm-key",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    }


def test_bundle_from_dict_round_trips():
    raw = _valid_bundle_dict()
    bundle = ApiPuffoBundle.from_dict(raw)
    assert bundle.agent_slug == "cloud-bot-1234"
    assert bundle.puffo_cloud_server_url == "https://cloud.puffo.ai"
    assert bundle.session_token == "tok_abcdef123456"
    assert bundle.provider == "anthropic"


def test_bundle_rejects_missing_fields():
    raw = _valid_bundle_dict()
    del raw["session_token"]
    with pytest.raises(ValueError, match="session_token"):
        ApiPuffoBundle.from_dict(raw)


def test_bundle_rejects_invalid_slug():
    raw = _valid_bundle_dict(slug="bad slug with spaces")
    with pytest.raises(ValueError, match="invalid agent_slug"):
        ApiPuffoBundle.from_dict(raw)


def test_materialise_writes_full_agent_dir():
    _isolated_home()
    bundle = ApiPuffoBundle.from_dict(_valid_bundle_dict())
    adir = materialise_agent_dir(bundle)

    # agent.yml round-trips through AgentConfig.load.
    cfg = AgentConfig.load(bundle.agent_slug)
    assert cfg.display_name == "Cloud Bot"
    assert cfg.role == "Cloud-hosted helper"
    assert cfg.runtime.kind == "api-puffo"
    assert cfg.runtime.provider == "anthropic"
    assert cfg.runtime.model == "claude-sonnet-4-6"
    assert cfg.puffo_core.server_url == "https://cloud.puffo.ai"
    assert cfg.puffo_core.slug == bundle.agent_slug
    assert cfg.puffo_core.operator_slug == "user-5678"

    # profile.md has the soul body inside a # Soul section.
    profile_md = (adir / "profile.md").read_text(encoding="utf-8")
    assert "# Soul" in profile_md
    assert "I help test the api-puffo runtime." in profile_md
    # extract_soul_body round-trips faithfully.
    from puffo_agent.portal.profile_sync import extract_soul_body
    assert extract_soul_body(profile_md) == "I help test the api-puffo runtime."

    # keystore loads back via ApiPuffoKeystore.
    ks = ApiPuffoKeystore.for_agent(bundle.agent_slug)
    assert ks.slug == bundle.agent_slug
    assert ks.session_token == "tok_abcdef123456"
    assert ks.puffo_cloud_server_url == "https://cloud.puffo.ai"
    assert ks.kem_cert["device_id"] == "dev_cloud_xyz"


def test_ingest_archives_bundle_after_provision():
    home = _isolated_home()
    bundle_path = install_dir() / "cloud-bot-1234.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(
        json.dumps(_valid_bundle_dict()), encoding="utf-8",
    )

    ok, msg = ingest_bundle(bundle_path)
    assert ok, msg
    assert msg.startswith("provisioned:")

    # Original bundle moved to archived/, agent_dir created.
    assert not bundle_path.exists()
    assert agent_yml_path("cloud-bot-1234").exists()
    archived = list((install_dir() / "archived").iterdir())
    assert len(archived) == 1
    assert archived[0].name.endswith("cloud-bot-1234.json")


def test_ingest_skips_when_agent_already_provisioned():
    _isolated_home()
    # Pre-provision.
    bundle = ApiPuffoBundle.from_dict(_valid_bundle_dict())
    materialise_agent_dir(bundle)

    # Drop a fresh bundle for the same slug; ingest should NOT clobber.
    profile_md_before = (
        agent_dir(bundle.agent_slug) / "profile.md"
    ).read_text(encoding="utf-8")

    bundle_path = install_dir() / "cloud-bot-1234.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    raw2 = _valid_bundle_dict()
    raw2["soul"] = "OVERWRITTEN SOUL"
    bundle_path.write_text(json.dumps(raw2), encoding="utf-8")

    ok, msg = ingest_bundle(bundle_path)
    assert ok, msg
    assert msg.startswith("already provisioned")

    profile_md_after = (
        agent_dir(bundle.agent_slug) / "profile.md"
    ).read_text(encoding="utf-8")
    assert profile_md_before == profile_md_after  # untouched
    assert not bundle_path.exists()  # archived


def test_sweep_install_dir_processes_all_pending():
    _isolated_home()
    install_dir().mkdir(parents=True, exist_ok=True)

    for i in range(3):
        slug = f"sweep-bot-{i:04d}"
        raw = _valid_bundle_dict(slug=slug)
        raw["device_id"] = f"dev_{i}"
        (install_dir() / f"{slug}.json").write_text(
            json.dumps(raw), encoding="utf-8",
        )

    n = sweep_install_dir()
    assert n == 3
    for i in range(3):
        assert agent_yml_path(f"sweep-bot-{i:04d}").exists()
