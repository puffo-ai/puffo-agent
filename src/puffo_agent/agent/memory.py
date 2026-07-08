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

from .memory_errors import BriefingCompileError, MemoryStoreError

logger = logging.getLogger(__name__)

# Re-exported so ``from .memory import MemoryStoreError`` /
# ``BriefingCompileError`` keeps working now that the classes live in
# the leaf ``memory_errors`` module.
__all__ = ["BriefingCompileError", "MemoryStoreError"]

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


def request_prompt_refresh(workspace_dir: str | Path, reason: str) -> bool:
    """Drop ``refresh_agent.flag`` so the worker rebuilds the prompt
    artifacts on the next batch. Best-effort; same payload shape as
    ``profile_sync.write_refresh_agent_flag``. No-op without a
    workspace dir. Returns True iff the flag was written (the M3
    tools layer maps this to the ``provider_reload`` post-effect)."""
    if not workspace_dir:
        return False
    from ..portal.state import refresh_agent_flag_path

    flag_path = refresh_agent_flag_path(Path(workspace_dir))
    try:
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.write_text(
            json.dumps({
                "version": 1,
                "requested_at": int(time.time()),
                "reason": reason,
            }) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(
            "refresh_agent.flag write failed (%s): %s", reason, exc,
        )
        return False
    return True


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


def _resolves_within_root(path: Path, memory_root: Path) -> bool:
    """True iff ``path`` resolves to a location inside ``memory_root``.

    The host-side compile / migration paths read the filesystem
    directly (not through the M2 store), so a symlink under the memory
    tree could otherwise pull in content from outside it. Resolving
    both sides collapses symlinks in either the leaf or an ancestor
    dir; a resolution failure is treated as "not contained"."""
    try:
        return path.resolve().is_relative_to(memory_root.resolve())
    except OSError:
        return False


def _is_briefing_topic(path: Path) -> bool:
    return (
        path.is_file()
        # ``is_file()`` follows symlinks; a symlinked briefing/<t>.md
        # would inject its target's bytes, so exclude symlinks outright.
        and not path.is_symlink()
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
    memory_root = Path(memory_root)
    briefing = memory_root / BRIEFING_DIR
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
        # ``_is_briefing_topic`` already dropped leaf symlinks; this
        # catches a topic that resolves outside the root through a
        # symlinked ancestor dir (e.g. a symlinked briefing/).
        if not _resolves_within_root(path, memory_root):
            logger.warning(
                "memory briefing: skipping %s — resolves outside the "
                "memory root", path,
            )
            continue
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
    flat = []
    for p in sorted(memory_root.glob("*.md")):
        if not p.is_file() or p.name == "README.md" or p.name.startswith("."):
            continue
        # A symlinked flat file (or one resolving outside the root)
        # must never be read or moved into the tree — that would fold
        # arbitrary host content into the agent's memory.
        if p.is_symlink() or not _resolves_within_root(p, memory_root):
            logger.warning(
                "memory migrate: skipping %s — symlink or resolves "
                "outside the memory root", p,
            )
            continue
        flat.append(p)
    if not flat:
        return []

    briefing_dir = memory_root / BRIEFING_DIR
    notes_dir = memory_root / NOTES_DIR
    pointer_path = briefing_dir / f"{MIGRATED_NOTES_TOPIC}.md"

    from .memory_store import MemoryStore

    store = MemoryStore(memory_root)

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
        moved.append(f"{NOTES_DIR}/{dest.name}")
        pointer_line = (
            f"- {NOTES_DIR}/{dest.name} — migrated from flat memory\n"
        )
        existing = ""
        if pointer_path.exists():
            existing = pointer_path.read_text(encoding="utf-8")
        elif not entry_map.get(MIGRATED_NOTES_TOPIC):
            existing = "# Migrated notes\n\n"
        new_body = existing + pointer_line
        try:
            # Route the pointer through the store: same atomic write and
            # per-file/total budget validation as any briefing topic, so
            # a migration can never leave a briefing that fails the next
            # compile.
            store.put_memory_file(
                f"{BRIEFING_DIR}/{MIGRATED_NOTES_TOPIC}.md", new_body,
            )
        except BriefingCompileError as exc:
            # The note itself already migrated to notes/; only its
            # pointer line is dropped so the briefing stays compilable.
            logger.warning(
                "memory migrate: pointer to %s skipped (%s) — the note "
                "migrated but the briefing budget is full",
                dest.name, exc.code,
            )
            continue
        entry_map[MIGRATED_NOTES_TOPIC] = new_body.strip()
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
    else:
        new_text = block
    from .memory_store import MemoryStore

    MemoryStore(memory_root).put_memory_file(
        f"{BRIEFING_DIR}/{PROFILE_BRIEFING_NAME}", new_text,
    )
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
        from .memory_store import MemoryStore

        safe_topic = topic.replace(" ", "_").replace("/", "-")
        # Aware-UTC with ``Z`` suffix (``datetime.utcnow`` is
        # deprecated in 3.12+).
        updated = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        body = f"---\ntopic: {topic}\nupdated: {updated}\n---\n\n{content}\n"
        # The store validates sizes (per-file + would-be compiled
        # total, raising BriefingCompileError) before any write, so an
        # oversized save never leaves partial state behind. The
        # refresh flag stays owned by save() — the store gets no
        # workspace_dir — so its reason keeps the M1 shape.
        MemoryStore(self.memory_dir).put_memory_file(
            f"{BRIEFING_DIR}/{safe_topic}.md", body,
        )
        self._request_refresh(topic)

    def _request_refresh(self, topic: str) -> None:
        request_prompt_refresh(self.workspace_dir, f"memory.save:{topic}")
