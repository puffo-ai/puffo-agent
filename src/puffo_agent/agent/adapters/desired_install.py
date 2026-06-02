"""PUF-268 spawn-time install of operator-picked skills + MCP
templates from puffo-server.

Runs once per worker spawn, after the host-sync block in
``local_cli._verify``. Fetches each template id by GET, dedupes
against host-synced state, and writes:

  * skills  → ``<agent_home>/.claude/skills/<id>/SKILL.md`` (body
    verbatim, so server-side frontmatter survives)
  * claude  MCPs → ``<agent_home>/.claude.json#mcpServers[<id>]``
  * codex   MCPs → cached on the adapter so
    ``_ensure_codex_session`` can fold them into ``extra_servers``
    on the same write that emits host MCPs.

404 from the catalog (template removed between picker time and
spawn time) logs a warning and continues; any other fetch failure
likewise non-fatal — a broken catalog must NOT block agent spawn.

The synchronous catalog fetch reuses ``PuffoCoreHttpClient.get`` —
which already owns subkey rotation, retry-on-401, and the signed
request shape. The single dependency is an event loop; the caller
threads this in via the adapter's existing async hooks.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ...crypto.http_client import HttpError, PuffoCoreHttpClient

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


def write_desired_skill(
    agent_home: Path, template_id: str, body: str,
) -> str:
    """Write a fetched skill body to ``<agent_home>/.claude/skills/<id>/SKILL.md``.

    Returns one of ``"installed"`` / ``"already-present"`` / ``"invalid"``.
    Idempotent: an existing SKILL.md at the path is left untouched
    (host-sync or a prior desired-install owns it).
    """
    if not _SKILL_ID_RE.match(template_id):
        logger.warning(
            "desired skill %r: invalid id — skipping", template_id,
        )
        return "invalid"
    if not body or not body.strip():
        logger.warning(
            "desired skill %r: empty body from server — skipping",
            template_id,
        )
        return "invalid"
    dst = agent_home / ".claude" / "skills" / template_id
    if (dst / "SKILL.md").exists():
        return "already-present"
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "SKILL.md").write_text(body, encoding="utf-8")
    (dst / DESIRED_INSTALLED_MARKER).write_text(
        _DESIRED_INSTALLED_BODY, encoding="utf-8",
    )
    return "installed"


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


async def install_desired(
    *,
    http: PuffoCoreHttpClient,
    agent_home: Path,
    agent_id: str,
    harness_name: str,
    desired_skills: list[str],
    desired_mcps: list[str],
) -> dict[str, dict[str, Any]]:
    """Run the spawn-time install pass.

    Returns ``codex_extra_servers`` — a ``{id: {command, args, env}}``
    map for any stdio MCPs the codex harness should fold into its
    ``[mcp_servers.*]`` config.toml. Always ``{}`` for claude.
    """
    is_codex = harness_name == "codex"

    if desired_skills:
        if is_codex:
            logger.warning(
                "agent %s: codex has no skills surface — skipping %d "
                "desired_skills (ids=%s). install via codex's own "
                "mechanism if/when available.",
                agent_id, len(desired_skills), desired_skills,
            )
        else:
            for sid in desired_skills:
                tpl = await fetch_skill_template(http, sid)
                if tpl is None:
                    continue
                body = tpl.get("body")
                if not isinstance(body, str):
                    logger.warning(
                        "desired skill %r: no body in template — skipping",
                        sid,
                    )
                    continue
                result = write_desired_skill(agent_home, sid, body)
                if result == "installed":
                    logger.info(
                        "agent %s: installed desired skill %r",
                        agent_id, sid,
                    )
                elif result == "already-present":
                    logger.info(
                        "agent %s: desired skill %r already present — left untouched",
                        agent_id, sid,
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
            if spec["type"] != "stdio":
                logger.warning(
                    "agent %s: desired mcp %r is %s — codex is stdio-only, skipping",
                    agent_id, mid, spec["type"],
                )
                continue
            codex_extras[mid] = {
                "command": spec["command"],
                "args": spec["args"],
                "env": spec["env"],
            }
            logger.info(
                "agent %s: queued desired mcp %r for codex config.toml",
                agent_id, mid,
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
