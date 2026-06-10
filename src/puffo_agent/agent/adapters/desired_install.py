"""Spawn-time install of operator-picked skill + MCP templates.
Runs once per worker spawn, after host-sync. Both harnesses install:

  * claude skills → ``<agent_home>/.claude/skills/<id>/SKILL.md``
  * codex  skills → ``<workspace>/.agents/skills/<id>/SKILL.md``
                    (body has ``mcp__puffo__`` prefix stripped)
  * claude MCPs   → ``<agent_home>/.claude.json#mcpServers[<id>]``
  * codex  MCPs   → cached on the adapter so ``_ensure_codex_session``
                    folds them into ``[mcp_servers.*]`` config.toml.

Catalog 404 / fetch error logs + continues — never blocks spawn."""
from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from ...crypto.http_client import HttpError, PuffoCoreHttpClient
from ..shared_content import _strip_puffo_mcp_prefix_for_codex

logger = logging.getLogger(__name__)


# Skill ids that pass the host_tools regex; tightens the picker
# wire to the same charset the on-disk installer accepts so a
# malformed id can't escape into a stray directory write.
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MCP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


# Provenance marker dropped next to SKILL.md for desired-installs.
# Distinct from ``host-synced.md`` / ``agent-installed.md`` so the
# host-sync pruner doesn't sweep these and operators can tell which
# layer wrote what.
DESIRED_INSTALLED_MARKER = "desired-installed.md"
# Stronger-provenance markers the desired pruner must never sweep.
AGENT_INSTALLED_MARKER = "agent-installed.md"
HOST_SYNCED_MARKER = "host-synced.md"
_DESIRED_INSTALLED_BODY = (
    "Installed at spawn time from a puffo-server template selected "
    "by the operator. See PUF-268.\n"
)


async def _fetch_template(
    http: PuffoCoreHttpClient,
    kind: str,
    template_id: str,
) -> dict[str, Any] | None:
    """Fetch one catalog template (``kind`` ∈ ``{"skill", "mcp"}``).

    Returns the parsed body, or None on 404 / HTTP error / network
    error / non-object body. Each failure surface is logged once with
    ``kind`` + ``template_id`` so operators can tell missing-from-
    catalog from upstream-server-error at a glance.
    """
    path = f"/v2/{kind}-templates/{template_id}"
    try:
        data = await http.get(path)
    except HttpError as exc:
        if exc.status == 404:
            logger.warning(
                "desired %s %r missing from catalog (404) — skipping",
                kind, template_id,
            )
            return None
        logger.warning(
            "desired %s %r fetch failed (HTTP %d): %s — skipping",
            kind, template_id, exc.status, exc.body[:200],
        )
        return None
    except Exception as exc:
        logger.warning(
            "desired %s %r fetch failed: %s — skipping",
            kind, template_id, exc,
        )
        return None
    if not isinstance(data, dict):
        logger.warning(
            "desired %s %r: server returned non-object body — skipping",
            kind, template_id,
        )
        return None
    return data


async def fetch_skill_template(
    http: PuffoCoreHttpClient, template_id: str,
) -> dict[str, Any] | None:
    """Return the skill template body or None on 404 / fetch error."""
    return await _fetch_template(http, "skill", template_id)


async def fetch_mcp_template(
    http: PuffoCoreHttpClient, template_id: str,
) -> dict[str, Any] | None:
    """Return the MCP template body or None on 404 / fetch error."""
    return await _fetch_template(http, "mcp", template_id)


def _write_skill_to_dir(
    skill_dir: Path, template_id: str, body: str,
) -> str:
    """Idempotent write of a SKILL.md + provenance marker to ``skill_dir``.
    Returns ``"installed"`` / ``"already-present"`` / ``"invalid"``."""
    if not _SKILL_ID_RE.match(template_id):
        logger.warning("desired skill %r: invalid id — skipping", template_id)
        return "invalid"
    if not body or not body.strip():
        logger.warning(
            "desired skill %r: empty body from server — skipping", template_id,
        )
        return "invalid"
    if (skill_dir / "SKILL.md").exists():
        return "already-present"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    (skill_dir / DESIRED_INSTALLED_MARKER).write_text(
        _DESIRED_INSTALLED_BODY, encoding="utf-8",
    )
    return "installed"


def write_desired_skill(
    agent_home: Path, template_id: str, body: str,
) -> str:
    """Write to ``<agent_home>/.claude/skills/<id>/SKILL.md`` — claude-code path."""
    return _write_skill_to_dir(
        agent_home / ".claude" / "skills" / template_id, template_id, body,
    )


def prune_stale_desired_skills(
    skills_root: Path, current_desired: list[str],
) -> int:
    """Remove skill dirs that only carry the desired-installed marker
    for ids no longer in the current desired list. host-synced and
    agent-installed entries are left alone — those lifecycles are
    owned elsewhere. Returns the count of dirs removed.

    Idempotent; spawn-time install calls this after the install loop
    so a freshly-installed skill is never pruned in the same pass.
    """
    if not skills_root.is_dir():
        return 0
    current_set = set(current_desired)
    pruned = 0
    for entry in skills_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in current_set:
            continue
        if not (entry / DESIRED_INSTALLED_MARKER).exists():
            continue
        # Stronger provenance wins — host-sync or agent-install
        # signals the skill should stay even after operator drops it
        # from the desired list.
        if (entry / AGENT_INSTALLED_MARKER).exists():
            continue
        if (entry / HOST_SYNCED_MARKER).exists():
            continue
        try:
            shutil.rmtree(entry)
            pruned += 1
        except OSError as exc:
            logger.warning(
                "desired-install prune %r: rmtree failed: %s — skipping",
                entry.name, exc,
            )
    return pruned


def write_desired_skill_codex(
    workspace_dir: Path, template_id: str, body: str,
) -> str:
    """Write to ``<workspace>/.agents/skills/<id>/SKILL.md``. Strips the
    ``mcp__puffo__`` prefix so tool refs match codex's bare-name router."""
    return _write_skill_to_dir(
        workspace_dir / ".agents" / "skills" / template_id,
        template_id,
        _strip_puffo_mcp_prefix_for_codex(body),
    )


def normalize_mcp_spec(template: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce a raw MCP template dict into the ``.claude.json#mcpServers``
    entry shape. ``None`` on unrecognised transport or missing fields.

    Wire field is ``type`` (not ``transport``); see PR-A spec.
    """
    transport = str(template.get("type") or "").lower()
    args = template.get("args") or []
    env = template.get("env") or {}
    args_list = [str(a) for a in args] if isinstance(args, list) else []
    env_map = (
        {str(k): str(v) for k, v in env.items()}
        if isinstance(env, dict) else {}
    )
    if transport == "stdio":
        command = template.get("command")
        if not isinstance(command, str) or not command:
            return None
        return {
            "type": "stdio",
            "command": command,
            "args": args_list,
            "env": env_map,
        }
    if transport in ("sse", "http"):
        url = template.get("url")
        if not isinstance(url, str) or not url:
            return None
        return {"type": transport, "url": url, "env": env_map}
    return None


def install_claude_mcp(
    agent_home: Path, template_id: str, spec: dict[str, Any],
) -> str:
    """Merge a desired MCP into ``<agent_home>/.claude.json#mcpServers``.

    Returns one of ``"installed"`` / ``"already-present"`` /
    ``"invalid"`` / ``"skipped"``. Pre-existing entries (host-sync,
    prior desired-install) are left untouched so operator-side
    overrides win; an unreadable or non-object ``.claude.json``
    returns ``"skipped"`` instead of silently resetting the file.
    """
    if not _MCP_ID_RE.match(template_id):
        logger.warning(
            "desired mcp %r: invalid id — skipping", template_id,
        )
        return "invalid"
    claude_json = agent_home / ".claude.json"
    data: dict[str, Any] = {}
    if claude_json.exists():
        try:
            raw = claude_json.read_text(encoding="utf-8")
            if raw.strip():
                data = json.loads(raw)
        except (OSError, ValueError) as exc:
            # Bail rather than reset: an unreadable .claude.json may
            # be a transient corruption during host-sync write, or a
            # legitimate user-authored file we can't parse. Resetting
            # to ``{}`` and rewriting would silently drop the user's
            # ``userID`` / history / etc. Mirrors host-sync's same
            # decision in state.py::sync_host_mcp_servers.
            logger.warning(
                "desired mcp %r: cannot read .claude.json (%s) — "
                "skipping to avoid clobbering operator-authored state",
                template_id, exc,
            )
            return "skipped"
    if not isinstance(data, dict):
        logger.warning(
            "desired mcp %r: .claude.json is not a JSON object — skipping",
            template_id,
        )
        return "skipped"
    servers = dict(data.get("mcpServers") or {})
    if template_id in servers:
        return "already-present"
    servers[template_id] = spec
    data["mcpServers"] = servers
    claude_json.parent.mkdir(parents=True, exist_ok=True)
    tmp = claude_json.with_suffix(claude_json.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(claude_json)
    return "installed"


def _codex_extras_entry(spec: dict[str, Any]) -> dict[str, Any]:
    """Project normalized spec → ``[mcp_servers.<name>]`` block shape."""
    if spec["type"] == "stdio":
        return {
            "command": spec["command"],
            "args": spec["args"],
            "env": spec["env"],
        }
    return {"url": spec["url"], "env": spec["env"]}


async def run_spawn_install(
    *,
    agent_id: str,
    agent_home: Path,
    workspace_dir: Path,
    harness_name: str,
    desired_skills: list[str],
    desired_mcps: list[str],
    server_url: str,
    slug: str,
    keys_dir: str,
) -> dict[str, dict[str, Any]]:
    """Build the puffo-core client from spawn wiring and run
    ``install_desired``, tolerating fetch / crash errors. Shared by the
    cli-local and cli-docker adapters. Returns ``codex_extra_servers``
    (``{}`` for claude, or when there's nothing to install).
    """
    if not desired_skills and not desired_mcps:
        return {}
    if not (server_url and slug and keys_dir):
        logger.warning(
            "agent %s: desired_skills/desired_mcps configured but "
            "puffo_core wiring is incomplete — skipping spawn-time "
            "install (server_url=%r slug=%r keys_dir=%r)",
            agent_id, server_url, slug, keys_dir,
        )
        return {}
    from ...crypto.http_client import PuffoCoreHttpClient
    from ...crypto.keystore import KeyStore
    http = PuffoCoreHttpClient(server_url, KeyStore(keys_dir), slug)
    try:
        return await install_desired(
            http=http,
            agent_home=agent_home,
            workspace_dir=workspace_dir,
            agent_id=agent_id,
            harness_name=harness_name,
            desired_skills=desired_skills,
            desired_mcps=desired_mcps,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "agent %s: desired install pass crashed: %s — continuing spawn",
            agent_id, exc,
        )
        return {}
    finally:
        await http.close()


async def install_desired(
    *,
    http: PuffoCoreHttpClient,
    agent_home: Path,
    workspace_dir: Path,
    agent_id: str,
    harness_name: str,
    desired_skills: list[str],
    desired_mcps: list[str],
) -> dict[str, dict[str, Any]]:
    """Run the spawn-time install pass for both harnesses.

    Returns ``codex_extra_servers`` — a ``{id: spec}`` map for codex to
    fold into ``[mcp_servers.*]`` config.toml. Always ``{}`` for claude.
    """
    # hermes is one-shot per turn and has no skills / MCP surface; the
    # picker still accepts ids for an hermes agent today, so bail
    # explicitly rather than write into ``.claude/`` for an agent that
    # won't read from it.
    if harness_name == "hermes":
        if desired_skills or desired_mcps:
            logger.info(
                "agent %s: hermes harness — skipping %d desired_skills + "
                "%d desired_mcps (no skills/MCP surface in hermes v1)",
                agent_id, len(desired_skills), len(desired_mcps),
            )
        return {}

    is_codex = harness_name == "codex"

    for sid in desired_skills:
        tpl = await fetch_skill_template(http, sid)
        if tpl is None:
            continue
        body = tpl.get("body")
        if not isinstance(body, str):
            logger.warning("desired skill %r: no body in template — skipping", sid)
            continue
        result = (
            write_desired_skill_codex(workspace_dir, sid, body) if is_codex
            else write_desired_skill(agent_home, sid, body)
        )
        if result == "installed":
            logger.info("agent %s: installed desired skill %r", agent_id, sid)
        elif result == "already-present":
            logger.info(
                "agent %s: desired skill %r already present — left untouched",
                agent_id, sid,
            )

    # Prune skill dirs whose only provenance is a now-stale
    # desired-installed marker. Runs after the install loop so
    # freshly-added ids in the current list aren't candidates.
    skills_root = (
        workspace_dir / ".agents" / "skills" if is_codex
        else agent_home / ".claude" / "skills"
    )
    pruned = prune_stale_desired_skills(skills_root, desired_skills)
    if pruned:
        logger.info(
            "agent %s: pruned %d stale desired-installed skill dir(s)",
            agent_id, pruned,
        )

    codex_extras: dict[str, dict[str, Any]] = {}
    for mid in desired_mcps:
        tpl = await fetch_mcp_template(http, mid)
        if tpl is None:
            continue
        spec = normalize_mcp_spec(tpl)
        if spec is None:
            logger.warning(
                "agent %s: desired mcp %r unsupported transport %r or "
                "missing required field — skipping",
                agent_id, mid, tpl.get("type"),
            )
            continue
        if is_codex:
            codex_extras[mid] = _codex_extras_entry(spec)
            logger.info(
                "agent %s: queued desired mcp %r (%s) for codex config.toml",
                agent_id, mid, spec["type"],
            )
        else:
            result = install_claude_mcp(agent_home, mid, spec)
            if result == "installed":
                logger.info(
                    "agent %s: installed desired mcp %r (%s)",
                    agent_id, mid, spec["type"],
                )
            elif result == "already-present":
                logger.info(
                    "agent %s: desired mcp %r already present — left untouched",
                    agent_id, mid,
                )
    return codex_extras
