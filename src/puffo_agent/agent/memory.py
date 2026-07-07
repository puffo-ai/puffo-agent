"""Agent memory tree (M1): bounded briefing compile + notes.

Layout under an agent's memory root::

    memory/
      briefing/           # always-loaded; compiled (bounded) into the
        profile.md        #   provider prompt artifacts. profile.md is
        <topic>.md        #   identity framing, synced from agent.yml.
      notes/              # durable detail; searchable, never injected
      recollection/       # reserved (M4)
      imports/index.md    # reserved; provenance of imported content

The compile is deterministic (profile first, then sorted filenames)
and fails closed: an over-limit briefing raises
``BriefingCompileError`` rather than truncating.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BRIEFING_DIR = "briefing"
NOTES_DIR = "notes"
RECOLLECTION_DIR = "recollection"
IMPORTS_DIR = "imports"

# Bounded-briefing budget (M1 ships the top of the design's ranges;
# M2 may tighten). Byte sizes of the UTF-8 encoded content.
PER_FILE_LIMIT = 16 * 1024
TOTAL_LIMIT = 64 * 1024

PROFILE_BRIEFING_NAME = "profile.md"
MIGRATED_NOTES_TOPIC = "migrated-notes"

_IMPORTS_INDEX_SEED = """\
# Imports index

Provenance of content imported into this agent's memory. Reserved —
maintained by the platform; one entry per import.
"""

PROFILE_MANAGED_BEGIN = "<!-- puffo:managed-profile -->"
PROFILE_MANAGED_END = "<!-- /puffo:managed-profile -->"

_MANAGED_BLOCK_RE = re.compile(
    re.escape(PROFILE_MANAGED_BEGIN) + r".*?" + re.escape(PROFILE_MANAGED_END),
    re.DOTALL,
)


class BriefingCompileError(Exception):
    """A briefing violates the compile budget. Fail closed — callers
    get no truncated/partial output. ``code`` is one of
    ``memory_file_too_large`` / ``memory_briefing_too_large``
    (M3-aligned names)."""

    def __init__(
        self,
        code: str,
        *,
        path: str,
        size: int,
        limit: int,
        suggestion: str,
    ):
        self.code = code
        self.path = path
        self.size = size
        self.limit = limit
        self.suggestion = suggestion
        super().__init__(
            f"{code}: {path} is {size} bytes (limit {limit}). {suggestion}"
        )

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "path": self.path,
            "size": self.size,
            "limit": self.limit,
            "suggestion": self.suggestion,
        }


def ensure_memory_tree(memory_root: Path) -> None:
    """Create the M1 memory tree under ``memory_root``. Idempotent;
    never touches existing content. All paths are built by joining
    fixed names under the root — nothing is written outside it."""
    memory_root = Path(memory_root)
    for sub in (BRIEFING_DIR, NOTES_DIR, RECOLLECTION_DIR, IMPORTS_DIR):
        (memory_root / sub).mkdir(parents=True, exist_ok=True)
    index = memory_root / IMPORTS_DIR / "index.md"
    if not index.exists():
        index.write_text(_IMPORTS_INDEX_SEED, encoding="utf-8")


def _byte_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _is_briefing_topic(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix == ".md"
        and not path.name.startswith(".")
    )


def _read_briefing_entries(
    memory_root: Path, *, enforce_per_file: bool = True,
) -> tuple[str, list[tuple[str, str]]]:
    """``(profile_body, [(stem, body), ...])`` in compile order:
    profile first, remaining topics by sorted filename. Empty bodies
    are dropped. Raises ``BriefingCompileError`` on a per-file limit
    violation unless ``enforce_per_file`` is off."""
    briefing = Path(memory_root) / BRIEFING_DIR
    profile_body = ""
    entries: list[tuple[str, str]] = []
    if not briefing.is_dir():
        return profile_body, entries
    profile_path = briefing / PROFILE_BRIEFING_NAME
    paths = [p for p in sorted(briefing.glob("*.md")) if _is_briefing_topic(p)]
    if profile_path in paths:
        paths.remove(profile_path)
        paths.insert(0, profile_path)
    for path in paths:
        body = path.read_text(encoding="utf-8")
        if enforce_per_file and _byte_size(body) > PER_FILE_LIMIT:
            raise BriefingCompileError(
                "memory_file_too_large",
                path=str(path),
                size=_byte_size(body),
                limit=PER_FILE_LIMIT,
                suggestion=(
                    "Trim this briefing topic; move detail to "
                    f"memory/{NOTES_DIR}/ (searchable, not injected)."
                ),
            )
        body = body.strip()
        if not body:
            continue
        if path == profile_path:
            profile_body = body
        else:
            entries.append((path.stem, body))
    return profile_body, entries


def _compose_briefing(profile_body: str, entries: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    if profile_body:
        parts.append(profile_body)
    for stem, body in entries:
        parts.append(f"### {stem}\n\n{body}")
    return "\n\n".join(parts)


def compile_briefing(memory_root: Path) -> str:
    """Deterministically compile ``briefing/`` into one prompt block:
    profile.md body first (verbatim), then every other ``*.md`` as a
    ``### <stem>`` section in sorted filename order. Enforces
    ``PER_FILE_LIMIT`` per file and ``TOTAL_LIMIT`` on the joined
    output; raises ``BriefingCompileError`` instead of truncating.
    Returns ``""`` for an empty/missing briefing."""
    memory_root = Path(memory_root)
    profile_body, entries = _read_briefing_entries(memory_root)
    compiled = _compose_briefing(profile_body, entries)
    total = _byte_size(compiled)
    if total > TOTAL_LIMIT:
        raise BriefingCompileError(
            "memory_briefing_too_large",
            path=str(memory_root / BRIEFING_DIR),
            size=total,
            limit=TOTAL_LIMIT,
            suggestion=(
                "The compiled briefing exceeds the total budget; move "
                f"topics to memory/{NOTES_DIR}/ or trim them."
            ),
        )
    return compiled


def migrate_flat_memory(memory_root: Path) -> list[str]:
    """Migrate legacy flat ``memory/*.md`` files into the tree.

    Deterministic rule: files are processed in sorted filename order
    (``README.md`` and dotfiles excluded). A file moves to
    ``briefing/<name>`` iff it is ≤ ``PER_FILE_LIMIT`` bytes, the
    briefing slot is free, and the would-be compiled briefing
    (current briefing + already-migrated files + this file) stays
    ≤ ``TOTAL_LIMIT``; otherwise it moves to ``notes/<name>`` (or
    ``notes/<stem>-migrated.md`` on collision) and a pointer line is
    appended to ``briefing/migrated-notes.md``. Idempotent: a pass
    leaves no flat ``*.md`` behind, so re-runs are no-ops.

    Returns the migrated files' new memory-root-relative paths.
    """
    memory_root = Path(memory_root)
    if not memory_root.is_dir():
        return []
    ensure_memory_tree(memory_root)
    flat = [
        p for p in sorted(memory_root.glob("*.md"))
        if p.is_file() and p.name != "README.md" and not p.name.startswith(".")
    ]
    if not flat:
        return []

    briefing_dir = memory_root / BRIEFING_DIR
    notes_dir = memory_root / NOTES_DIR
    pointer_path = briefing_dir / f"{MIGRATED_NOTES_TOPIC}.md"

    # Live simulation state: recomposing from these on every candidate
    # keeps the fit check exact (section headers + join separators
    # count toward the total, and so do appended pointer lines).
    profile_body, entries = _read_briefing_entries(
        memory_root, enforce_per_file=False,
    )
    entry_map: dict[str, str] = dict(entries)

    def compiled_size_with(stem: str, body: str) -> int:
        candidate = dict(entry_map)
        candidate[stem] = body.strip()
        composed = _compose_briefing(
            profile_body, sorted(candidate.items()),
        )
        return _byte_size(composed)

    moved: list[str] = []
    for path in flat:
        body = path.read_text(encoding="utf-8")
        fits_briefing = (
            _byte_size(body) <= PER_FILE_LIMIT
            and path.name != PROFILE_BRIEFING_NAME
            and not (briefing_dir / path.name).exists()
            and compiled_size_with(path.stem, body) <= TOTAL_LIMIT
        )
        if fits_briefing:
            dest = briefing_dir / path.name
            path.rename(dest)
            entry_map[path.stem] = body.strip()
            moved.append(f"{BRIEFING_DIR}/{dest.name}")
            continue
        dest = notes_dir / path.name
        if dest.exists():
            dest = notes_dir / f"{path.stem}-migrated.md"
            n = 2
            while dest.exists():
                dest = notes_dir / f"{path.stem}-migrated-{n}.md"
                n += 1
        path.rename(dest)
        pointer_line = (
            f"- {NOTES_DIR}/{dest.name} — migrated from flat memory\n"
        )
        existing = ""
        if pointer_path.exists():
            existing = pointer_path.read_text(encoding="utf-8")
        elif not entry_map.get(MIGRATED_NOTES_TOPIC):
            existing = "# Migrated notes\n\n"
        pointer_path.write_text(existing + pointer_line, encoding="utf-8")
        entry_map[MIGRATED_NOTES_TOPIC] = (existing + pointer_line).strip()
        moved.append(f"{NOTES_DIR}/{dest.name}")
    return moved


def render_profile_briefing(
    *,
    agent_id: str = "",
    display_name: str = "",
    role: str = "",
    role_short: str = "",
    soul: str = "",
) -> str:
    """Identity framing for ``briefing/profile.md``: display name,
    role lines, soul body. Deliberately NO ``## Instructions`` and no
    runtime-behavior sections — those live in the agent-root
    profile.md / primer. Missing fields degrade to a minimal identity
    line derived from agent id + display name."""
    name = display_name or agent_id or "agent"
    identity = f"You are {name}"
    if agent_id:
        identity += f" (agent `{agent_id}`)"
    identity += "."
    lines = [f"# {name}", "", identity]
    if role:
        lines += ["", f"Role: {role}"]
    if role_short:
        lines.append(f"Role (short): {role_short}")
    if soul.strip():
        lines += ["", "## Soul", "", soul.strip()]
    return "\n".join(lines) + "\n"


def sync_profile_briefing(
    memory_root: Path,
    *,
    agent_id: str = "",
    display_name: str = "",
    role: str = "",
    role_short: str = "",
    soul: str = "",
) -> Path:
    """(Re)write the managed block of ``briefing/profile.md`` from the
    native profile surfaces (agent.yml identity fields + the ``# Soul``
    body of agent-root profile.md). Content between the
    ``puffo:managed-profile`` markers is regenerated on every managed
    rebuild; user-authored text outside the markers is preserved."""
    memory_root = Path(memory_root)
    ensure_memory_tree(memory_root)
    path = memory_root / BRIEFING_DIR / PROFILE_BRIEFING_NAME
    rendered = render_profile_briefing(
        agent_id=agent_id,
        display_name=display_name,
        role=role,
        role_short=role_short,
        soul=soul,
    )
    block = f"{PROFILE_MANAGED_BEGIN}\n{rendered}{PROFILE_MANAGED_END}\n"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if _MANAGED_BLOCK_RE.search(text):
            new_text = _MANAGED_BLOCK_RE.sub(
                lambda _m: block.rstrip("\n"), text, count=1,
            )
        else:
            # Pre-existing user file without markers: identity framing
            # leads, user content follows untouched.
            new_text = block + "\n" + text
        path.write_text(new_text, encoding="utf-8")
    else:
        path.write_text(block, encoding="utf-8")
    return path


class MemoryManager:
    """Compat shim over the memory tree. ``save()`` keeps its historic
    name/signature but writes a briefing topic (bounded, fail closed)
    and marks the prompt artifacts for rebuild; ``get_context()``
    returns the compiled briefing instead of a full join."""

    def __init__(self, memory_dir: str, workspace_dir: str = ""):
        self.memory_dir = memory_dir
        self.workspace_dir = workspace_dir
        ensure_memory_tree(Path(memory_dir))

    def get_context(self) -> str:
        return compile_briefing(Path(self.memory_dir))

    def save(self, topic: str, content: str):
        safe_topic = topic.replace(" ", "_").replace("/", "-")
        path = Path(self.memory_dir) / BRIEFING_DIR / f"{safe_topic}.md"
        # Aware-UTC with ``Z`` suffix (``datetime.utcnow`` is
        # deprecated in 3.12+).
        updated = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        body = f"---\ntopic: {topic}\nupdated: {updated}\n---\n\n{content}\n"
        size = _byte_size(body)
        if size > PER_FILE_LIMIT:
            raise BriefingCompileError(
                "memory_file_too_large",
                path=str(path),
                size=size,
                limit=PER_FILE_LIMIT,
                suggestion=(
                    "Save a shorter briefing topic; put the detail in "
                    f"memory/{NOTES_DIR}/ instead."
                ),
            )
        # Pre-validate the would-be compiled total so an oversized
        # save never leaves partial state behind.
        profile_body, entries = _read_briefing_entries(
            Path(self.memory_dir), enforce_per_file=False,
        )
        entry_map = dict(entries)
        if safe_topic == "profile":
            profile_body = body.strip()
        else:
            entry_map[safe_topic] = body.strip()
        total = _byte_size(
            _compose_briefing(profile_body, sorted(entry_map.items()))
        )
        if total > TOTAL_LIMIT:
            raise BriefingCompileError(
                "memory_briefing_too_large",
                path=str(path),
                size=total,
                limit=TOTAL_LIMIT,
                suggestion=(
                    "This save would push the compiled briefing over "
                    f"budget; move topics to memory/{NOTES_DIR}/ first."
                ),
            )
        path.write_text(body, encoding="utf-8")
        self._request_refresh(topic)

    def _request_refresh(self, topic: str) -> None:
        """Drop ``refresh_agent.flag`` so the worker rebuilds the
        prompt artifacts on the next batch. Best-effort; same payload
        shape as ``profile_sync.write_refresh_agent_flag``."""
        if not self.workspace_dir:
            return
        from ..portal.state import refresh_agent_flag_path

        flag_path = refresh_agent_flag_path(Path(self.workspace_dir))
        try:
            flag_path.parent.mkdir(parents=True, exist_ok=True)
            flag_path.write_text(
                json.dumps({
                    "version": 1,
                    "requested_at": int(time.time()),
                    "reason": f"memory.save:{topic}",
                }) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "refresh_agent.flag write failed after memory save (%s): %s",
                topic, exc,
            )
