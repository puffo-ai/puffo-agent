#!/usr/bin/env python3
"""Seed a cloud agent's memory from a local one.

**This is not a migration.** It moves no identity: nothing is exported, rotated,
or revoked, and the local agent is only ever read. The tool that does migrate is
``puffo_agent.portal.import_agents``, and it is a different, harder operation
guarded by a different decision.

Memory is uploaded into the **agent store** (S3), on a prefix of its own::

    agents/<cloud-slug>/memory/…

via ``PUT /agents/{slug}/memory`` on the AIM control API — not to S3 directly.
Handing an agent's owner S3 access would be handing them a key to the bucket
every other agent's config lives in.

The usual flow is:

    1. create the cloud agent in the Hub, choosing its billing mode
    2. ./tools/seed_cloud_agent.py --from desk --dry-run
    3. ./tools/seed_cloud_agent.py --from desk --to <cloud-slug>
    4. ./tools/seed_cloud_agent.py --verify <cloud-slug>

**Memory only.** ``profile.md`` is read for the report but never uploaded: the
agent store owns the persona and ``_deliver_pending_config`` restores its copy
on resume, so a profile written anywhere else is reverted the first time the
agent idles. Set one where the store keeps it — the create dialog's PROFILE
field, or ``PUT /agents/{slug}``.

**Stored, not delivered.** The upload lands in S3. A *running* agent keeps the
memory it already has; the store is read only when an agent boots into a FRESH
sandbox, and only when its memory tree is empty. Seed before the first message —
an unseeded agent answers from an empty brain, which reads like a memory bug
rather than a missing step. Re-uploading does not update a live agent.

**Nothing writes memory back.** What an agent learns lives on the sandbox disk
only; it is never returned to the store. A sandbox that is recreated comes back
with what was uploaded and nothing since.

Deliberately not carried: ``keys/`` (identity — carrying it would make this a
migration), ``agent.yml`` (identity plus a gateway credential), ``workspace/``
(reproducible, and up to 109 MB), ``Library/`` (leaked host state), and
``messages.db`` (the server holds the history).

There is no ``--token``. The subscription credential is a property of the
deployment, not of an agent: it reaches a sandbox as an environment variable set
once by the provisioner.

**Operator-only for now.** Uploading needs ``AIM_CONTROL_URL`` +
``AIM_CONTROL_TOKEN``, and ``--verify`` needs ``E2B_API_KEY`` — both PLATFORM
credentials. An agent's own owner cannot hold either. A user-facing upload has
to go through puffo-server, which the user is already authenticated to; that
route does not exist yet.

Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
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


def collect_memory(agent_dir: Path) -> tuple[dict[str, bytes], dict]:
    """The memory to upload, as ``{relative path: bytes}``, plus a summary.

    Memory only. ``profile.md`` is read for the report but **not** uploaded: the
    agent store owns the persona and restores its copy on resume, so a profile
    has to be set where the store keeps it (the create dialog's PROFILE field or
    ``PUT /agents/{slug}``). Writing it here would be reverted on the first idle.
    """
    profile = (agent_dir / "profile.md").read_text(encoding="utf-8")
    files: dict[str, bytes] = {}
    mem = agent_dir / "memory"
    if mem.is_dir():
        root = mem.resolve()
        for f in sorted(mem.rglob("*")):
            # Symlinks are refused, not followed. `is_file()` is true for a link
            # to a file, and `read_bytes()` would read the TARGET — so a link
            # under memory/ pointing at ~/.ssh/id_ed25519 would upload the key
            # as a "note". The resolve check also catches a symlinked parent
            # directory, which rglob may have descended into.
            if f.is_symlink() or not f.is_file():
                continue
            try:
                f.resolve().relative_to(root)
            except ValueError:
                continue  # resolves outside the memory tree
            rel = f.relative_to(mem)
            if any(part in _NOT_CONTENT for part in rel.parts) or f.name.startswith("._"):
                continue
            # The whole tree, the managed briefing included. It names the SOURCE
            # agent when it lands, but the worker regenerates its managed block
            # for the new identity at every start (`refresh_managed_briefing`)
            # and keeps any user text outside the markers — so seeding it is
            # how a user's briefing notes travel, and the stale id never
            # survives a boot.
            files[rel.as_posix()] = f.read_bytes()

    body = soul_body(profile)
    return files, {
        "name": agent_name(agent_dir.name),
        "slug": agent_dir.name,
        "profile_bytes": len(profile.encode()),
        "soul_chars": len(body),
        "memory_files": len(files),
        "warnings": [] if body else ["profile has no soul-like heading"],
    }


def _control(path: str, payload: dict, method: str = "PUT") -> dict:
    """One authenticated call to the AIM control API. Stdlib only.

    Needs ``AIM_CONTROL_URL`` + ``AIM_CONTROL_TOKEN``. That token is a PLATFORM
    credential, not a per-user one, which is why this step is operator-only
    today — the same limit ``--verify`` has with ``E2B_API_KEY``. A user-facing
    upload has to go through puffo-server, which the user is already
    authenticated to; that route does not exist yet.
    """
    base = os.environ.get("AIM_CONTROL_URL", "").strip().rstrip("/")
    token = os.environ.get("AIM_CONTROL_TOKEN", "").strip()
    if not base or not token:
        raise SeedError(
            "uploading needs AIM_CONTROL_URL and AIM_CONTROL_TOKEN in the environment"
        )
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Operator": os.environ.get("USER", "seed-cli"),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"").decode("utf-8", "replace")[:300]
        raise SeedError(f"control API said {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise SeedError(f"could not reach {base}: {exc.reason}") from None


def set_profile(cloud_slug: str, agent_dir: Path) -> int:
    """PUT the local agent's ``profile.md`` as the cloud agent's store-owned profile.

    The Hub's PROFILE field expects a *Soul body*; pasting a whole profile.md
    there gets wrapped under the Hub's own ``# Soul`` and the extractor keeps
    only the first three lines — the briefing then has no real persona. Sending
    the file verbatim through ``PUT /agents/{slug}`` (which replaces profile.md,
    pushes it, and reloads the worker) makes the cloud profile byte-identical
    to the local one and the regenerated briefing carries the full Soul.
    """
    text = (agent_dir / "profile.md").read_text(encoding="utf-8")
    _control(f"/agents/{cloud_slug}", {"soul": text}, method="PUT")
    return len(text.encode())


def deliver(cloud_slug: str) -> dict:
    """Push the STORED memory into the agent's RUNNING sandbox — once, if empty.

    An upload is "stored, not delivered": a sandbox is seeded only when it
    boots, so memory uploaded after the agent was created sits in the store
    while the agent answers from an empty brain. A reboot is not the answer —
    a cloud agent's identity keys live only in its sandbox, so recreating it
    mints a new identity and breaks the agent in the Hub.

    This asks AIM to seed the live sandbox instead. The seed-once rule holds:
    if the sandbox already has any memory, nothing is written and the server
    says so. The agent reads its memory dir per turn, so delivered files are
    live on its next message.
    """
    ack = _control(f"/agents/{cloud_slug}/memory/deliver", {}, method="POST")
    return {"delivered": int(ack.get("delivered", 0)), "reason": str(ack.get("reason", ""))}


def upload(cloud_slug: str, files: dict[str, bytes]) -> int:
    """PUT the memory into the agent store. Returns the count the server wrote.

    Base64 because notes are not guaranteed UTF-8 and JSON cannot carry raw
    bytes. The server stores it; it does **not** deliver it to a running agent —
    memory is seeded into a sandbox only on a fresh boot.
    """
    if not files:
        print("  nothing to upload (no memory files)")
        return 0
    body = {"files": {k: base64.b64encode(v).decode() for k, v in files.items()}}
    ack = _control(f"/agents/{cloud_slug}/memory", body)
    stored = int(ack.get("files", 0))
    if stored != len(files):
        # The server reports what it actually wrote. Reporting that number as
        # success while it differs from what was sent turns a partial upload
        # into a green run — the agent boots with a subset of its brain and
        # nothing says so.
        raise SeedError(
            f"server stored {stored} of {len(files)} memory files — upload incomplete, "
            "nothing to rely on; re-run after checking the server log"
        )
    return stored


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
echo "auth_mode=$(awk -F': *' '/^ *auth_mode:/{v=$2; gsub(/["'"'"' ]/,"",v); print v; exit}' $A/agent.yml 2>/dev/null)"
# Presence, never the value: this travels back over the command channel, and a
# debug print of the collected facts would otherwise dump a live gateway key.
echo "api_key_present=$(awk '/^ *api_key:/{v=$0; sub(/.*: */,"",v); gsub(/["'"'"' ]/,"",v); print (v==""?0:1); exit}' $A/agent.yml 2>/dev/null)"
echo "profile_bytes=$(wc -c < $A/profile.md 2>/dev/null || echo 0)"
echo "profile_has_soul=$(grep -ciE '^#{1,6} +(soul|description|about|summary) *$' $A/profile.md 2>/dev/null || echo 0)"
echo "memory_files=$(find $A/memory -type f -not -path '*/.git/*' 2>/dev/null | wc -l | tr -d ' ')"
C=$(pgrep -f '^/usr/bin/claude' | head -1)
if [ -n "$C" ]; then
  for V in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_API_KEY; do
    tr '\0' '\n' < /proc/$C/environ 2>/dev/null | grep -q "^$V=" && echo "child_$V=1" || echo "child_$V=0"
  done
else echo "child_absent=1"; fi
# Resolve each host and count sockets against ITS addresses. A hardcoded IPv4
# prefix reported zero for a healthy agent: api.anthropic.com answers on IPv6.
count_conns() {
  ips=$(getent ahosts "$1" 2>/dev/null | awk '{print $1}' | sort -u)
  if [ -z "$ips" ]; then echo -1; else ss -tn 2>/dev/null | grep -cFf <(echo "$ips"); fi
}
echo "conns_anthropic=$(count_conns api.anthropic.com)"
echo "conns_gateway=$(count_conns litellm-staging.puffo.ai)"
# The relay specifically — counting every ESTAB socket reported "connected" for
# an agent with a live LLM connection and a dead bridge, which is the exact
# failure this is here to catch.
RELAY=$(sed -n 's|.*server_url:.*//\([^/"'"'"' ]*\).*|\1|p' $A/agent.yml 2>/dev/null | head -1)
echo "conns_relay=$([ -n "$RELAY" ] && count_conns "$RELAY" || echo -1)"
for E in 'credential view-sync incomplete' 'CLI exited' 'provider_error'; do
  N=$(grep -hc "$E" /home/user/.aim/logs/*.log 2>/dev/null | awk '{s+=$1} END{print s+0}')
  [ "${N:-0}" -gt 0 ] && echo "log_error=$E|$N"
done
echo "bad_frame_count=$(grep -hc BAD_FRAME /home/user/.aim/logs/*.log 2>/dev/null | awk '{s+=$1} END{print s+0}')"
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
        elif k in ("profile_bytes", "memory_files", "conns_anthropic", "conns_gateway",
                   "conns_relay", "bad_frame_count"):
            facts[k] = int(v or 0)
        elif k == "profile_has_soul":
            facts[k] = int(v or 0) > 0
        elif k.startswith("child_CLAUDE_CODE_OAUTH_TOKEN"):
            facts["child_has_token"] = v == "1"
        elif k.startswith("child_ANTHROPIC_BASE_URL"):
            facts["child_has_base_url"] = v == "1"
        elif k.startswith("child_ANTHROPIC_API_KEY"):
            facts["child_has_api_key"] = v == "1"
        elif k == "api_key_present":
            # Presence only — the value never leaves the sandbox.
            facts["api_key"] = "<present>" if v == "1" else ""
        elif k in ("auth_mode", "slug"):
            facts[k] = v.strip()
    facts["log_errors"] = errors
    return {k: v for k, v in facts.items() if v is not UNSET}


UNSET = object()


def verify(slug: str, *, expected_template: str = "", expected_auth_mode: str = "") -> int:
    """Find the sandbox running ``slug`` and report its readiness."""
    try:
        from puffo_agent.agent.cloud_verify import evaluate, summarize
    except ModuleNotFoundError:
        # A fresh checkout has not `pip install -e .`'d and has no PYTHONPATH.
        # The tool lives at tools/, the package at src/ — locate it ourselves
        # rather than hand a new user a traceback for a path they never set.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
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
    ap.add_argument("--to", metavar="CLOUD_SLUG", help="cloud agent to upload the memory to")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument(
        "--profile",
        action="store_true",
        help="also set the cloud agent's profile to the local profile.md, verbatim "
        "(PUT /agents/{slug}); makes the persona byte-identical to local",
    )
    ap.add_argument(
        "--deliver",
        action="store_true",
        help="after upload (or alone with --to), seed the RUNNING sandbox from the store "
        "— only if its memory tree is empty; no reboot",
    )
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

    if args.deliver and not args.source and not args.all:
        # Deliver-only: the memory is already in the store (an earlier upload).
        if not args.to:
            ap.error("--deliver needs --to <cloud-slug>")
        try:
            out = deliver(args.to)
        except SeedError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"delivered {out['delivered']} memory file(s) to {args.to}: {out['reason']}")
        return 0 if out["delivered"] or "not empty" in out["reason"] else 1

    if not args.all and not args.source:
        ap.error("give --from <agent>, --all, --verify <cloud-slug>, or --deliver --to <cloud-slug>")
    if not args.dry_run and not args.to:
        ap.error("--to <cloud-slug> is required unless --dry-run")
    if args.all and args.to:
        # One upload targets one cloud agent. `--all --to X` would pile every
        # local agent's notes into X's memory, which reads as a bulk migration
        # and is actually a merge.
        ap.error("--all cannot be combined with --to; upload one agent at a time")

    try:
        agents = (
            discover(args.fleet) if args.all else [resolve_one(args.source, args.fleet)]
        )
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"would upload memory for {len(agents)} agent(s) from {args.fleet}:")
        for d in agents:
            _, summary = collect_memory(d)
            _report([summary])
        print("\nprofile.md is NOT uploaded — the agent store owns it.")
        print("Set it in the create dialog's PROFILE field, or PUT /agents/{slug}.")
        return 0

    agent_dir = agents[0]
    try:
        files, summary = collect_memory(agent_dir)
        _report([summary])
        if args.profile:
            print(f"profile set from local profile.md ({set_profile(args.to, agent_dir)} bytes)")
        n = upload(args.to, files)
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nuploaded {n} memory file(s) to {args.to}")
    if args.deliver:
        try:
            out = deliver(args.to)
        except SeedError as exc:
            print(f"error: upload succeeded but delivery failed: {exc}", file=sys.stderr)
            return 1
        print(f"delivered {out['delivered']} memory file(s) into the running sandbox: {out['reason']}")
    else:
        print("Stored, not delivered: a running agent keeps the memory it has.")
        print("Memory is seeded into a sandbox only on a FRESH boot — add --deliver to seed it now.")
    if summary["profile_bytes"]:
        print(
            f"\nprofile.md ({summary['profile_bytes']}B) was NOT uploaded — the agent "
            "store owns it.\nPaste it into the create dialog's PROFILE field, or "
            "PUT /agents/{slug}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
