"""Identity MCP tool registration."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..agent.message_projection import CONTEXT_VERSION, identity_projection
from .puffo_core_tools import _read_profiles, _ts_to_iso

logger = logging.getLogger(__name__)


def register_identity_tools(mcp: FastMCP, cfg: Any) -> None:
    @mcp.tool()
    async def whoami() -> dict[str, Any]:
        """Return a structured self-identity and runtime summary."""
        profile: dict[str, Any] = {}
        try:
            data = await _read_profiles(cfg, cfg.slug)
            profiles = data.get("profiles", []) if isinstance(data, dict) else []
            if profiles and isinstance(profiles[0], dict):
                profile = profiles[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("whoami: failed to fetch own profile: %s", exc)

        projected_identity = identity_projection(
            cfg.slug,
            display_name=profile.get("display_name"),
            identity_type="agent",
            owner_slug=profile.get("owner_slug"),
            role=profile.get("role"),
            self_identity=True,
            bio=profile.get("bio") or None,
            avatar_url=profile.get("avatar_url") or None,
        )
        if cfg.keyless:
            return {
                "context_version": CONTEXT_VERSION,
                "identity": projected_identity,
                "runtime": {
                    "device_id": cfg.device_id,
                    "server_url": cfg.http_client.server_url,
                    "transport": "keyless",
                    "subkey_management": "server",
                    "subkey_id": None,
                    "subkey_expires_at": None,
                },
            }

        identity = cfg.keystore.load_identity(cfg.slug)
        subkey_id: str | None = None
        subkey_expires_at: str | None = None
        try:
            sess = cfg.keystore.load_session(cfg.slug)
            subkey_id = sess.subkey_id
            subkey_expires_at = _ts_to_iso(sess.expires_at)
        except FileNotFoundError:
            pass
        return {
            "context_version": CONTEXT_VERSION,
            "identity": projected_identity,
            "runtime": {
                "device_id": identity.device_id,
                "server_url": identity.server_url,
                "transport": "signed",
                "subkey_management": "local",
                "subkey_id": subkey_id,
                "subkey_expires_at": subkey_expires_at,
            },
        }
