#!/usr/bin/env python3
"""Seed a cloud agent from a local one — persona and memory, nothing else.

**This is not a migration.** It moves no identity: nothing is exported,
rotated, or revoked, and the local agent is only ever read. The tool that does
migrate is ``puffo_agent.portal.import_agents``, and it is a different, harder
operation guarded by a different decision.

What this does is publish a local agent's `profile.md` and `memory/` into the
fleet memory repo, under the name a human uses::

    <repo>/<name>/profile.md
    <repo>/<name>/memory/…

A cloud agent whose config carries ``memory_remote`` clones that on its first
boot (see ``agent/memory_seed.py``), so the usual flow is:

    1. create the cloud agent in the Hub, choosing its billing mode
    2. ./tools/seed_cloud_agent.py --from desk --push
    3. ./tools/seed_cloud_agent.py --verify <cloud-slug>     (optional, remote)

**Seed before the first message.** An unseeded agent answers from an empty
brain, which reads like a memory bug rather than a missing step.

Deliberately not carried: ``keys/`` (identity — carrying it would make this a
migration), ``agent.yml`` (identity plus a gateway credential), ``workspace/``
(reproducible, and up to 109 MB), ``Library/`` (leaked host state), and
``messages.db`` (the server holds the history).

There is no ``--token``. The subscription credential is a property of the
deployment, not of an agent: it reaches a sandbox as an environment variable set
once by the provisioner. A per-agent flag would imply per-agent tokens — more
copies of a credential, more places to revoke — and would have this CLI write a
secret into a sandbox, which is worse than leaving it where it is.

Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_FLEET = Path.home() / ".puffo-agent" / "agents"

#: Headings `portal.profile_sync` treats as the persona section.
_SOUL_HEADING = re.compile(r"^(#{1,6})\s+(soul|description|about|summary)\s*$", re.I)

#: Names that are scaffolding, not memory content.
_NOT_CONTENT = {".git", ".gitkeep", ".DS_Store"}


class SeedError(RuntimeError):
    """A directory could not be seeded; the reason is the message."""


def soul_body(profile_md: str) -> str:
    """The persona section, or "" when the profile has no such heading.

    Same rule as ``portal.profile_sync``: the body runs to the next heading of
    the same or higher level, so a `# Soul` keeps its `##` subsections.
    """
    lines = profile_md.splitlines()
    start = level = None
    for i, line in enumerate(lines):
        m = _SOUL_HEADING.match(line.strip())
        if m:
            start, level = i, len(m.group(1))
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s+\S", lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    return "\n".join(lines[start + 1 : end]).strip()


def agent_name(slug: str) -> str:
    """`desk-6332-b73d5e96` -> `desk`; `planner-2d35d73e` -> `planner`."""
    name = re.sub(r"-\d{3,}-[0-9a-f]{6,}$", "", slug)
    name = re.sub(r"-[0-9a-f]{8,}$", "", name)
    return name or slug


def discover(fleet: Path) -> list[Path]:
    """Agent directories worth seeding, in name order.

    An agent needs **both** `profile.md` and `agent.yml`. Half a real fleet is
    empty scratch directories; a bare glob would publish dozens of them.
    """
    if not fleet.is_dir():
        raise SeedError(f"no fleet directory at {fleet}")
    return sorted(
        d
        for d in fleet.iterdir()
        if d.is_dir() and (d / "profile.md").is_file() and (d / "agent.yml").is_file()
    )


def resolve_one(target: str, fleet: Path) -> Path:
    """A path, or a slug prefix matched against the fleet (must be unique)."""
    p = Path(target).expanduser()
    if p.is_dir():
        return p
    matches = [d for d in discover(fleet) if d.name.startswith(target)]
    if not matches:
        raise SeedError(f"no agent matching {target!r} in {fleet}")
    if len(matches) > 1:
        raise SeedError(f"{target!r} is ambiguous: {', '.join(d.name for d in matches)}")
    return matches[0]


def stage(agent_dir: Path, dest: Path) -> dict:
    """Copy the portable parts into ``dest``; return a summary."""
    dest.mkdir(parents=True, exist_ok=True)
    profile = (agent_dir / "profile.md").read_text(encoding="utf-8")
    (dest / "profile.md").write_text(profile, encoding="utf-8")

    mem_src, mem_dst = agent_dir / "memory", dest / "memory"
    if mem_dst.exists():
        shutil.rmtree(mem_dst)
    notes = 0
    if mem_src.is_dir():
        shutil.copytree(
            mem_src, mem_dst, ignore=shutil.ignore_patterns(*_NOT_CONTENT, "._*")
        )
        notes = sum(1 for f in mem_dst.rglob("*") if f.is_file())
    else:
        mem_dst.mkdir()
        (mem_dst / ".gitkeep").touch()

    body = soul_body(profile)
    return {
        "name": dest.name,
        "slug": agent_dir.name,
        "profile_bytes": len(profile.encode()),
        "soul_chars": len(body),
        "memory_files": notes,
        "warnings": [] if body else ["profile has no soul-like heading"],
    }


def _report(results: list[dict]) -> None:
    for r in results:
        flag = "  ! " + "; ".join(r["warnings"]) if r["warnings"] else ""
        print(
            f"  {r['name']:<16} profile={r['profile_bytes']}B "
            f"soul={r['soul_chars']}c memory={r['memory_files']} files{flag}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Seed BEFORE the agent's first message: an unseeded agent "
        "answers from an empty brain.",
    )
    ap.add_argument("--from", dest="source", help="local agent slug prefix or path")
    ap.add_argument("--all", action="store_true", help="seed every agent found")
    ap.add_argument("--fleet", type=Path, default=DEFAULT_FLEET)
    ap.add_argument("--repo", help="fleet memory repo (git URL or path)")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--push", action="store_true", help="commit and push the result")
    args = ap.parse_args(argv)

    if not args.all and not args.source:
        ap.error("give --from <agent>, or --all")
    if not args.dry_run and not args.repo:
        ap.error("--repo is required unless --dry-run")

    try:
        agents = (
            discover(args.fleet) if args.all else [resolve_one(args.source, args.fleet)]
        )
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"would seed {len(agents)} agent(s) from {args.fleet}:")
        for d in agents:
            mem = d / "memory"
            n = (
                sum(
                    1
                    for f in mem.rglob("*")
                    if f.is_file() and ".git" not in f.parts
                )
                if mem.is_dir()
                else 0
            )
            print(f"  {agent_name(d.name):<16} {d.name:<30} memory={n} files")
        return 0

    work = Path(tempfile.mkdtemp(prefix="puffo-fleet-memory-"))
    try:
        subprocess.run(
            ["git", "clone", "-q", args.repo, str(work)], check=True, timeout=120
        )
        results = [stage(d, work / agent_name(d.name)) for d in agents]
        _report(results)

        if args.push:
            names = ", ".join(r["name"] for r in results)
            subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", str(work), "commit", "-q", "-m", f"seed: {names}"],
                check=False,  # nothing to commit is success, not failure
            )
            subprocess.run(["git", "-C", str(work), "push", "-q"], check=True)
            print(f"\npushed {len(results)} agent(s)")
        else:
            print(f"\nstaged in {work} (add --push to publish)")
            return 0
    except subprocess.CalledProcessError as exc:
        print(f"error: git failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.push:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
