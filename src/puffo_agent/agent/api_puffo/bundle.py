"""Install-bundle ingestion for api-puffo agents.

A bundle is a one-shot JSON file (placed by an installer / web-side
flow) that contains everything a cloud-hosted agent needs.

**The thin model.** The runtime holds NO key material. Server-side
``puffo-server/cloud_agent`` loads the agent's KMS-sealed keystore
once per WS connection and drives all seal/open. Per
``BRIDGE-WIRE-PROTOCOL.md`` the runtime only needs:

  - identity:    agent_slug, operator_slug
  - auth:        sandbox_token (bearer for the WS upgrade; in E2B
                 the egress proxy injects it, in local dev we set
                 it ourselves)
  - cloud URL:   puffo_cloud_server_url
  - profile:     display_name, role, role_short, soul, avatar_url
  - runtime:     api_key, provider, model (LLM HTTP — separate from
                 the bridge, scope unchanged)

On daemon startup we sweep ``~/.puffo-agent/api-puffo-install/`` for
``<slug>.json`` files. Each is materialised into the standard
agent_dir layout (agent.yml + profile.md + keys/<slug>.json) so the
existing reconcile loop picks it up.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...portal.state import (
    agent_dir,
    agent_yml_path,
    home_dir,
    is_valid_agent_id,
)

logger = logging.getLogger(__name__)


_BUNDLE_REQUIRED_FIELDS = (
    "agent_slug",
    "operator_slug",
    "sandbox_token",
    "puffo_cloud_server_url",
    "display_name",
    "soul",
    "api_key",
    "provider",
    "model",
)


@dataclass
class ApiPuffoBundle:
    agent_slug: str
    operator_slug: str
    sandbox_token: str
    puffo_cloud_server_url: str
    display_name: str
    role: str
    role_short: str
    soul: str
    avatar_url: str
    api_key: str
    provider: str
    model: str

    @classmethod
    def from_dict(cls, raw: dict) -> "ApiPuffoBundle":
        missing = [f for f in _BUNDLE_REQUIRED_FIELDS if f not in raw]
        if missing:
            raise ValueError(f"bundle missing required fields: {missing}")
        if not is_valid_agent_id(raw["agent_slug"]):
            raise ValueError(f"invalid agent_slug: {raw['agent_slug']!r}")
        return cls(
            agent_slug=raw["agent_slug"],
            operator_slug=raw["operator_slug"],
            sandbox_token=raw["sandbox_token"],
            puffo_cloud_server_url=raw["puffo_cloud_server_url"].rstrip("/"),
            display_name=raw["display_name"],
            role=raw.get("role", ""),
            role_short=raw.get("role_short", ""),
            soul=raw["soul"],
            avatar_url=raw.get("avatar_url", ""),
            api_key=raw["api_key"],
            provider=raw["provider"],
            model=raw["model"],
        )


def install_dir() -> Path:
    return home_dir() / "api-puffo-install"


def archive_dir() -> Path:
    return install_dir() / "archived"


def discover_bundles() -> list[Path]:
    d = install_dir()
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix == ".json")


def materialise_agent_dir(bundle: ApiPuffoBundle) -> Path:
    """Write agent.yml + profile.md + keys/<slug>.json. Returns the
    agent directory path."""
    adir = agent_dir(bundle.agent_slug)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "workspace").mkdir(parents=True, exist_ok=True)
    (adir / "memory").mkdir(parents=True, exist_ok=True)
    keys_dir = adir / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)

    # profile.md — bundle's full soul into a single # Soul section so
    # the standard extract_soul_body / _profile_summary readers work.
    if bundle.role:
        profile_md = f"# {bundle.display_name}\n\n{bundle.role}\n\n"
    else:
        profile_md = f"# {bundle.display_name}\n\n"
    profile_md += f"# Soul\n\n{bundle.soul.strip()}\n"
    (adir / "profile.md").write_text(profile_md, encoding="utf-8")

    # keys/<slug>.json — only auth state. Server resolves identity
    # from sandbox_token on every WS upgrade.
    keys = {
        "slug": bundle.agent_slug,
        "sandbox_token": bundle.sandbox_token,
        "puffo_cloud_server_url": bundle.puffo_cloud_server_url,
    }
    keys_path = keys_dir / f"{bundle.agent_slug}.json"
    tmp = keys_path.with_suffix(keys_path.suffix + ".tmp")
    tmp.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    tmp.replace(keys_path)

    # agent.yml — minimal shape consumed by AgentConfig.load.
    cfg: dict[str, Any] = {
        "id": bundle.agent_slug,
        "state": "running",
        "display_name": bundle.display_name,
        "avatar_url": bundle.avatar_url,
        "role": bundle.role,
        "role_short": bundle.role_short,
        "created_at": int(time.time()),
        "puffo_core": {
            "server_url": bundle.puffo_cloud_server_url,
            "slug": bundle.agent_slug,
            "device_id": "",
            "space_id": "",
            "operator_slug": bundle.operator_slug,
        },
        "runtime": {
            "kind": "api-puffo",
            "provider": bundle.provider,
            "model": bundle.model,
            "api_key": bundle.api_key,
            "harness": "",
            "permission_mode": "bypassPermissions",
            "max_turns": 10,
        },
        "triggers": {
            "on_mention": True,
            "on_dm": True,
        },
    }
    import yaml
    (adir / "agent.yml").write_text(
        yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8",
    )
    return adir


def ingest_bundle(bundle_path: Path) -> tuple[bool, str]:
    """Read, validate, materialise, archive. Returns (ok, msg)."""
    try:
        raw = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"unreadable bundle: {exc}"
    try:
        bundle = ApiPuffoBundle.from_dict(raw)
    except ValueError as exc:
        return False, f"invalid bundle: {exc}"
    if agent_yml_path(bundle.agent_slug).exists():
        _archive(bundle_path)
        return True, f"already provisioned: {bundle.agent_slug}"
    try:
        materialise_agent_dir(bundle)
    except Exception as exc:  # noqa: BLE001
        return False, f"materialise failed: {exc}"
    _archive(bundle_path)
    return True, f"provisioned: {bundle.agent_slug}"


def _archive(bundle_path: Path) -> None:
    arc = archive_dir()
    arc.mkdir(parents=True, exist_ok=True)
    dest = arc / f"{int(time.time())}-{bundle_path.name}"
    try:
        shutil.move(str(bundle_path), str(dest))
    except OSError as exc:
        logger.warning("bundle archive failed for %s: %s", bundle_path, exc)


def sweep_install_dir() -> int:
    """Ingest every pending bundle. Returns the count provisioned."""
    bundles = discover_bundles()
    provisioned = 0
    for path in bundles:
        ok, msg = ingest_bundle(path)
        level = logger.info if ok else logger.warning
        level("api-puffo install: %s — %s", path.name, msg)
        if ok and msg.startswith("provisioned:"):
            provisioned += 1
    return provisioned
