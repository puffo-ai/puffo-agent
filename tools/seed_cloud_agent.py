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
    2. ./tools/seed_cloud_agent.py --from desk --repo <fleet> --push
    3. ./tools/seed_cloud_agent.py --verify <cloud-slug>

``--verify`` answers "is this agent actually able to serve?" with ten checks,
each of which failed at least once during the subscription build-out. It needs
the E2B SDK and ``E2B_API_KEY`` — install with ``pip install 'puffo-agent[cloud]'``
or run it from an environment that already has them. Everything else here is
stdlib and works anywhere.

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


# --------------------------------------------------------------------------
# --verify
#
# Collection needs a sandbox; evaluation does not. The split keeps the checks
# unit-testable without credentials, and lets a server-side caller reuse them
# by gathering the same facts another way.


def _require_e2b():
    try:
        from e2b import Sandbox  # noqa: PLC0415
    except ImportError:
        raise SeedError(
            "--verify needs the E2B SDK. Install with: pip install 'puffo-agent[cloud]'"
        ) from None
    import os

    if not os.environ.get("E2B_API_KEY"):
        raise SeedError("--verify needs E2B_API_KEY in the environment")
    return Sandbox


_PROBE = r"""
A=$(ls -d /home/user/.puffo-agent/agents/*/ 2>/dev/null | head -1)
[ -z "$A" ] && { echo "no-agent-dir"; exit 0; }
echo "slug=$(basename $A)"
echo "auth_mode=$(awk -F': *' '/^ *auth_mode:/{v=$2; gsub(/[\"'\'' ]/,"",v); print v; exit}' $A/agent.yml 2>/dev/null)"
echo "api_key=$(awk -F': *' '/^ *api_key:/{v=$2; gsub(/[\"'\'' ]/,"",v); print v; exit}' $A/agent.yml 2>/dev/null)"
echo "profile_bytes=$(wc -c < $A/profile.md 2>/dev/null || echo 0)"
echo "profile_has_soul=$(grep -ciE '^#{1,6} +(soul|description|about|summary) *$' $A/profile.md 2>/dev/null || echo 0)"
echo "memory_files=$(find $A/memory -type f -not -path '*/.git/*' 2>/dev/null | wc -l | tr -d ' ')"
C=$(pgrep -f '^/usr/bin/claude' | head -1)
if [ -n "$C" ]; then
  for V in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_API_KEY; do
    tr '\0' '\n' < /proc/$C/environ 2>/dev/null | grep -q "^$V=" && echo "child_$V=1" || echo "child_$V=0"
  done
else echo "child_absent=1"; fi
echo "conns_anthropic=$(ss -tn 2>/dev/null | grep -c '160.79.104')"
G=$(getent hosts litellm-staging.puffo.ai 2>/dev/null | awk '{print $1}' | head -1)
echo "conns_gateway=$([ -n "$G" ] && ss -tn 2>/dev/null | grep -c "$G" || echo 0)"
for E in 'credential view-sync incomplete' 'CLI exited' 'provider_error'; do
  N=$(grep -hc "$E" /home/user/.aim/logs/*.log 2>/dev/null | paste -sd+ - | sed 's/+/ /g' | awk '{s=0;for(i=1;i<=NF;i++)s+=$i;print s}')
  [ "${N:-0}" -gt 0 ] && echo "log_error=$E|$N"
done
echo "bad_frame_count=$(grep -hc BAD_FRAME /home/user/.aim/logs/*.log 2>/dev/null | paste -sd+ - | sed 's/+/ /g' | awk '{s=0;for(i=1;i<=NF;i++)s+=$i;print s}')"
echo "bridge_connected=$(ss -tn 2>/dev/null | grep -cE 'ESTAB' )"
"""


def collect(sandbox, *, expected_template="", expected_auth_mode="") -> dict:
    """Gather the facts `cloud_verify.evaluate` needs from a live sandbox.

    A fact that cannot be read is simply absent — never guessed — so an
    unreadable sandbox reports "undetermined" rather than a clean bill.
    """
    facts: dict = {"state": "running"}
    if expected_template:
        facts["expected_template"] = expected_template
    if expected_auth_mode:
        facts["expected_auth_mode"] = expected_auth_mode
    facts["template_id"] = getattr(sandbox, "template_id", None) or UNSET

    out = sandbox.commands.run(_PROBE, timeout=90).stdout or ""
    errors: dict[str, int] = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k == "log_error":
            name, _, n = v.partition("|")
            errors[name] = int(n or 0)
        elif k in ("profile_bytes", "memory_files", "conns_anthropic", "conns_gateway", "bad_frame_count"):
            facts[k] = int(v or 0)
        elif k == "profile_has_soul":
            facts[k] = int(v or 0) > 0
        elif k.startswith("child_CLAUDE_CODE_OAUTH_TOKEN"):
            facts["child_has_token"] = v == "1"
        elif k.startswith("child_ANTHROPIC_BASE_URL"):
            facts["child_has_base_url"] = v == "1"
        elif k.startswith("child_ANTHROPIC_API_KEY"):
            facts["child_has_api_key"] = v == "1"
        elif k == "bridge_connected":
            facts[k] = int(v or 0) > 0
        elif k in ("auth_mode", "api_key", "slug"):
            facts[k] = v.strip()
    facts["log_errors"] = errors
    return {k: v for k, v in facts.items() if v is not UNSET}


UNSET = object()


def verify(slug: str, *, expected_template: str = "", expected_auth_mode: str = "") -> int:
    """Find the sandbox running ``slug`` and report its readiness."""
    from puffo_agent.agent.cloud_verify import evaluate, summarize

    Sandbox = _require_e2b()
    from e2b import SandboxQuery, SandboxState  # noqa: PLC0415

    pager = Sandbox.list(query=SandboxQuery(state=[SandboxState.RUNNING]))
    running = []
    while pager.has_next:
        running.extend(pager.next_items())
    if not running:
        print("no running sandboxes", file=sys.stderr)
        return 2

    for info in running:
        sb = Sandbox.connect(info.sandbox_id)
        probe = sb.commands.run(
            f"test -d /home/user/.puffo-agent/agents/{slug} && echo yes || echo no",
            timeout=30,
        )
        if "yes" not in (probe.stdout or ""):
            continue
        facts = collect(
            sb, expected_template=expected_template, expected_auth_mode=expected_auth_mode
        )
        facts["template_id"] = info.template_id
        results = evaluate(facts)
        print(f"{slug}  (sandbox {info.sandbox_id}, template {info.template_id})\n")
        for r in results:
            print(f"  [{r.mark:>4}] {r.name:<28} {r.detail}")
            if r.ok is not True:
                print(f"         catches: {r.catches}")
        passed, failed, unknown = summarize(results)
        print(f"\n  {passed} passed, {failed} failed, {unknown} undetermined")
        return 1 if failed else 0

    print(f"no running sandbox hosts agent {slug!r}", file=sys.stderr)
    return 2


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
    ap.add_argument("--verify", metavar="CLOUD_SLUG", help="check a live cloud agent is ready to serve")
    ap.add_argument("--expect-template", default="", help="--verify: template id the agent should have booted")
    ap.add_argument("--expect-auth-mode", default="", help="--verify: api-gateway | subscription")
    args = ap.parse_args(argv)

    if args.verify:
        try:
            return verify(
                args.verify,
                expected_template=args.expect_template,
                expected_auth_mode=args.expect_auth_mode,
            )
        except SeedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if not args.all and not args.source:
        ap.error("give --from <agent>, --all, or --verify <cloud-slug>")
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
