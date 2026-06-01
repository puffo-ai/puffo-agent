"""Display-name resolvers backed by the on-disk profile / space /
channel caches the worker writes."""
from __future__ import annotations

from ...agent import disk_cache
from ..state import AgentConfig, discover_agents


def slug_to_display_name() -> dict[str, str]:
    """Merge the worker-fetched profile cache with locally defined
    agents so peers without a fresh server lookup still render by
    name when the operator runs both sides on this device."""
    out: dict[str, str] = {}
    for slug, entry in disk_cache.load_all_profiles().items():
        name = (entry.get("display_name") or "").strip()
        if name:
            out[slug] = name
    for aid in discover_agents():
        try:
            cfg = AgentConfig.load(aid)
        except Exception:
            continue
        if cfg.puffo_core.slug:
            out.setdefault(cfg.puffo_core.slug, cfg.display_name or aid)
    return out


def resolve_display_name(slug: str) -> str:
    if not slug:
        return ""
    entry = disk_cache.load_profile(slug)
    if entry:
        return (entry.get("display_name") or "").strip()
    return ""


def space_id_to_name() -> dict[str, str]:
    return {
        sid: (entry.get("name") or "").strip() or sid
        for sid, entry in disk_cache.load_all_spaces().items()
    }


def channel_id_to_name() -> dict[str, str]:
    return {
        cid: (entry.get("name") or "").strip() or cid
        for cid, entry in disk_cache.load_all_channels().items()
    }
