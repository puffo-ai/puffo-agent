"""PUF-395: create-time provisioning of claude-code sub-agents.

A sub-agent is a markdown expert-context (YAML frontmatter — name / description /
tools / model — plus a system-prompt body) the claude-code harness auto-loads
from ``<workspace>/.claude/agents/<name>.md`` at Task-tool spawn. Unlike skills
and MCPs (operator-picked template ids the daemon fetches from the catalog at
worker spawn), a sub-agent's ``.md`` arrives inline in the create request — same
trust tier as the ``profile.md`` / ``avatarBytes`` already there — so it is
validated and written at create time. claude-code only; codex has no sub-agent
surface and a non-empty list on a codex create is a hard error.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

# kebab-case, mirroring the skill-id charset: the name becomes the
# ``<name>.md`` filename, so this also blocks path-escape.
_SUBAGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _frontmatter_name(content: str) -> str | None:
    """The ``name`` from a leading ``---`` YAML frontmatter block, or None if
    there is no parseable block or no string ``name`` in it."""
    text = content.lstrip()
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)  # ['', '<frontmatter>', '<body>']
    if len(parts) < 3:
        return None
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    name = meta.get("name")
    return name if isinstance(name, str) else None


def validate_desired_subagents(raw: object, *, harness: str) -> list[dict]:
    """Validate a create request's ``desired_subagents`` and return a
    normalized ``[{name, content}]`` list.

    Raises ``ValueError`` (message enumerates every problem) on failure so the
    caller can surface a single 400. Rules:

    - codex harness + non-empty → rejected (no sub-agent surface)
    - each item is ``{name: str, content: str}`` with a kebab-case name
    - content has parseable YAML frontmatter whose ``name`` matches the item
    - no duplicate names in the request
    """
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("must be a list of {name, content} objects")
    if not raw:
        return []
    if harness == "codex":
        raise ValueError("subagents not supported on codex harness")

    errors: list[str] = []
    normalized: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"subagent[{i}]: must be an object with name + content")
            continue
        name = item.get("name")
        content = item.get("content")
        if not isinstance(name, str) or not name:
            errors.append(f"subagent[{i}]: missing non-empty 'name'")
            continue
        if not isinstance(content, str) or not content.strip():
            errors.append(f"subagent {name!r}: missing non-empty 'content'")
            continue
        if not _SUBAGENT_NAME_RE.match(name):
            errors.append(
                f"subagent {name!r}: name must be kebab-case [a-z0-9-], <=64 chars"
            )
            continue
        if name in seen:
            errors.append(f"subagent {name!r}: duplicate name in request")
            continue
        seen.add(name)
        fm_name = _frontmatter_name(content)
        if fm_name is None:
            errors.append(
                f"subagent {name!r}: content has no parseable YAML frontmatter 'name'"
            )
            continue
        if fm_name != name:
            errors.append(
                f"subagent {name!r}: frontmatter name {fm_name!r} does not match "
                "request name"
            )
            continue
        normalized.append({"name": name, "content": content})

    if errors:
        raise ValueError("; ".join(errors))
    return normalized


def write_subagents(claude_dir: Path, subagents: list[dict]) -> list[dict]:
    """Write each normalized ``{name, content}`` to
    ``<claude_dir>/agents/<name>.md`` (dir created on demand) and return the
    materialized ``[{name, sha256}]`` for byte-faithful client verification."""
    written: list[dict] = []
    if not subagents:
        return written
    agents_dir = claude_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for sa in subagents:
        path = agents_dir / f"{sa['name']}.md"
        # Never write through a pre-existing symlink — a planted link could
        # escape the workspace. The create path uses a fresh dir so this can't
        # trigger there, but the helper must not assume its caller does.
        if path.is_symlink():
            raise ValueError(f"subagent {sa['name']!r}: refusing to write through a symlink")
        # write_bytes (not write_text) so the on-disk file is byte-identical to
        # the sha256 we report — no newline translation on any platform.
        data = sa["content"].encode("utf-8")
        path.write_bytes(data)
        written.append({"name": sa["name"], "sha256": hashlib.sha256(data).hexdigest()})
    return written
