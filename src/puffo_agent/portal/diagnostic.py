"""``puffo-agent test ...`` — macOS Keychain integration probes.

Designed to be run by a colleague on a real macOS host so we can
verify the assumptions baked into ``puffo_agent.macos.keychain``. Each
subcommand:

  - Returns a structured ``ProbeReport`` (markdown) on stdout.
  - Classifies each step as ``OK`` / ``FAIL`` / ``NEEDS_ATTENTION``.
  - Redacts secrets aggressively — token strings are shown only as
    ``len=NNN sha256_prefix=XXXXXXXX``, never raw.

Cross-platform: every subcommand runs everywhere, but non-Darwin hosts
get a ``skipped: not applicable`` body so Linux/Windows reviewers can
also sanity-check the output format.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..agent.cli_bin import resolve_claude_bin
from ..macos.keychain import (
    KEYCHAIN_SERVICE,
    is_macos,
    read_keychain_blob,
)
from .state import home_dir


# ─────────────────────────────────────────────────────────────────────────────
# Report structure
# ─────────────────────────────────────────────────────────────────────────────

VERDICT_OK = "OK"
VERDICT_FAIL = "FAIL"
VERDICT_NEEDS_ATTENTION = "NEEDS_ATTENTION"
VERDICT_SKIPPED = "SKIPPED"


@dataclass
class ProbeStep:
    name: str
    verdict: str  # one of VERDICT_*
    detail: str = ""


@dataclass
class ProbeReport:
    title: str
    steps: list[ProbeStep] = field(default_factory=list)
    summary: str = ""

    def add(self, name: str, verdict: str, detail: str = "") -> None:
        self.steps.append(ProbeStep(name, verdict, detail))

    def overall(self) -> str:
        if any(s.verdict == VERDICT_FAIL for s in self.steps):
            return VERDICT_FAIL
        if any(s.verdict == VERDICT_NEEDS_ATTENTION for s in self.steps):
            return VERDICT_NEEDS_ATTENTION
        if all(s.verdict == VERDICT_SKIPPED for s in self.steps):
            return VERDICT_SKIPPED
        return VERDICT_OK

    def render_markdown(self) -> str:
        lines = [f"# {self.title}", ""]
        lines.append(f"**Overall**: {self.overall()}")
        lines.append("")
        lines.append(f"- Platform: `{platform.system()} {platform.release()}`")
        lines.append(f"- Python: `{sys.version.split()[0]}`")
        lines.append(f"- claude on PATH: `{shutil.which('claude') or '(none)'}`")
        lines.append("")
        for s in self.steps:
            lines.append(f"## {s.name} — {s.verdict}")
            if s.detail:
                lines.append("")
                lines.append("```")
                lines.append(s.detail)
                lines.append("```")
            lines.append("")
        if self.summary:
            lines.append("---")
            lines.append("")
            lines.append(f"_{self.summary}_")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Secret-safe printing
# ─────────────────────────────────────────────────────────────────────────────

def _redact_token(s: str) -> str:
    """Show length + sha256 prefix, never raw token."""
    if not s:
        return "(empty)"
    digest = hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]
    return f"len={len(s)} sha256_prefix={digest}"


def _summarise_blob(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return f"invalid JSON (len={len(raw)})"
    oauth = data.get("claudeAiOauth") or {}
    access = oauth.get("accessToken") or ""
    refresh = oauth.get("refreshToken") or ""
    expires = oauth.get("expiresAt")
    return (
        f"claudeAiOauth.accessToken: {_redact_token(access)}\n"
        f"claudeAiOauth.refreshToken: {_redact_token(refresh)}\n"
        f"claudeAiOauth.expiresAt: {expires}"
    )


def _access_token(blob: str) -> str:
    try:
        return (
            (json.loads(blob).get("claudeAiOauth") or {}).get("accessToken")
        ) or ""
    except json.JSONDecodeError:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Probes
# ─────────────────────────────────────────────────────────────────────────────

def probe_keychain_read() -> ProbeReport:
    """Step 1: can the daemon read the Keychain entry?"""
    rpt = ProbeReport(title="puffo-agent test keychain-read")
    if not is_macos():
        rpt.add("platform-check", VERDICT_SKIPPED, "not Darwin — probe skipped")
        return rpt
    rpt.add(
        "command",
        VERDICT_OK,
        f"$ security find-generic-password -s {KEYCHAIN_SERVICE!r} -w",
    )
    result = read_keychain_blob()
    if not result.ok:
        rpt.add(
            "read",
            VERDICT_FAIL,
            f"reason: {result.error}\n"
            f"stderr: {result.stderr or '(none)'}\n"
            "→ if 'exit_code=44', the entry doesn't exist; run "
            "`claude` interactively to populate the Keychain.\n"
            "→ if 'timeout', an ACL prompt is pending — accept "
            "'Always Allow' so future calls are non-interactive.",
        )
        rpt.summary = "Keychain read failed — daemon cannot bootstrap."
        return rpt
    rpt.add("read", VERDICT_OK, _summarise_blob(result.blob))
    rpt.summary = "Keychain read succeeded. Bootstrap path will work."
    return rpt


def probe_keychain_write() -> ProbeReport:
    """Step 2: can the daemon round-trip a writeback?

    Writes the existing blob back to itself (so the user's auth state
    is preserved) and re-reads to confirm the upsert worked.
    """
    rpt = ProbeReport(title="puffo-agent test keychain-write")
    if not is_macos():
        rpt.add("platform-check", VERDICT_SKIPPED, "not Darwin — probe skipped")
        return rpt
    from ..macos.keychain import writeback_to_keychain

    pre = read_keychain_blob()
    if not pre.ok:
        rpt.add(
            "prerequisite-read",
            VERDICT_FAIL,
            f"can't read existing blob to roundtrip: {pre.error}",
        )
        return rpt
    rpt.add("prerequisite-read", VERDICT_OK, "captured existing blob")

    ok, reason = writeback_to_keychain(pre.blob)
    if not ok:
        rpt.add("write", VERDICT_FAIL, f"upsert failed: {reason}")
        return rpt
    rpt.add("write", VERDICT_OK, "upsert succeeded (-U)")

    post = read_keychain_blob()
    if not post.ok:
        rpt.add(
            "verify-read",
            VERDICT_FAIL,
            f"re-read after write failed: {post.error}",
        )
        return rpt
    if post.blob != pre.blob:
        rpt.add(
            "verify-read",
            VERDICT_FAIL,
            "post-write blob differs from pre — upsert corrupted the entry",
        )
        return rpt
    rpt.add("verify-read", VERDICT_OK, "post-write blob matches pre")
    rpt.summary = "Keychain write+read roundtrip works."
    return rpt


def probe_refresh_flush() -> ProbeReport:
    """Step 3: does ``claude --print "ok"`` against the real Keychain
    actually flush a refreshed token?

    Mirrors production: real user HOME, no sandbox. claude reads
    Keychain, refreshes if expired, writes the rotated blob back to
    Keychain. We compare the access_token before/after.

    Passive only — when the token is still well within its TTL, claude
    won't rotate (correctly), and we report NEEDS_ATTENTION explaining
    the token is fresh. There's no longer a forced variant: in the
    real-HOME design we can't lie to claude about expiry without
    mutating the user's Keychain entry, and the production refresh
    path is exercised every time the daemon's poll triggers a refresh
    against a naturally-expiring token.
    """
    rpt = ProbeReport(title="puffo-agent test refresh-flush")
    if not is_macos():
        rpt.add("platform-check", VERDICT_SKIPPED, "not Darwin — probe skipped")
        return rpt
    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        rpt.add(
            "prerequisite-claude",
            VERDICT_FAIL,
            "`claude` not resolvable via $PUFFO_CLAUDE_BIN, PATH, or "
            "known bundle paths — install Claude Code first.",
        )
        return rpt

    pre = read_keychain_blob()
    if not pre.ok:
        rpt.add(
            "prerequisite-keychain",
            VERDICT_FAIL,
            f"keychain read failed: {pre.error}",
        )
        return rpt
    rpt.add("prerequisite-keychain", VERDICT_OK, "Keychain entry exists pre-probe")
    old_token = _access_token(pre.blob)

    host_home = Path.home()
    env = {**os.environ, "HOME": str(host_home)}
    started = time.time()
    try:
        proc = subprocess.run(
            [
                claude_bin, "--dangerously-skip-permissions",
                "--print", "--max-turns", "1",
                "--output-format", "stream-json", "--verbose",
                "ok",
            ],
            env=env, cwd=str(host_home),
            capture_output=True, text=True, timeout=90,
        )
        code = proc.returncode
        stderr_tail = (proc.stderr or "")[-500:]
    except subprocess.TimeoutExpired:
        rpt.add("claude-oneshot", VERDICT_FAIL, "claude oneshot timed out after 90s")
        return rpt
    elapsed_ms = int((time.time() - started) * 1000)

    if code != 0:
        rpt.add(
            "claude-oneshot",
            VERDICT_FAIL,
            f"exit={code}, elapsed_ms={elapsed_ms}\n"
            f"stderr (last 500): {stderr_tail}",
        )
        return rpt
    rpt.add("claude-oneshot", VERDICT_OK, f"exit=0, elapsed_ms={elapsed_ms}")

    post = read_keychain_blob()
    if not post.ok:
        rpt.add(
            "keychain-after",
            VERDICT_FAIL,
            f"re-read after claude oneshot failed: {post.error}",
        )
        return rpt
    new_token = _access_token(post.blob)
    rotated = bool(new_token) and old_token != new_token
    rpt.add(
        "keychain-after",
        VERDICT_OK if rotated else VERDICT_NEEDS_ATTENTION,
        "token rotated: {}\nold: {}\nnew: {}".format(
            rotated, _redact_token(old_token), _redact_token(new_token),
        ),
    )

    if rotated:
        rpt.summary = (
            "Refresh flush works — claude rotated the token and the new "
            "value is live in Keychain."
        )
    else:
        rpt.summary = (
            "claude exited OK but the token wasn't rotated — current "
            "token is still valid, so claude correctly skipped the "
            "OAuth round-trip. The production refresh path is "
            "exercised whenever the daemon's poll hits a "
            "naturally-expiring token (within ~10 min of expiresAt)."
        )
    return rpt


def probe_full() -> ProbeReport:
    """Run every probe end-to-end and return a single combined report."""
    rpt = ProbeReport(title="puffo-agent test full-probe")
    rpt.add(
        "environment",
        VERDICT_OK,
        f"home_dir: {home_dir()}\nclaude: {shutil.which('claude')}",
    )

    sub_reports = [
        probe_keychain_read(),
        probe_keychain_write(),
        probe_refresh_flush(),
    ]
    for sub in sub_reports:
        verdict = sub.overall()
        rpt.add(
            sub.title,
            verdict,
            "\n".join(f"  [{s.verdict}] {s.name}" for s in sub.steps)
            + (f"\n\nsummary: {sub.summary}" if sub.summary else ""),
        )

    overall = rpt.overall()
    if overall == VERDICT_OK:
        rpt.summary = "All probes green."
    elif overall == VERDICT_NEEDS_ATTENTION:
        rpt.summary = (
            "Some probes flagged NEEDS_ATTENTION — review each item."
        )
    else:
        rpt.summary = "At least one probe FAILED."
    return rpt


# ─────────────────────────────────────────────────────────────────────────────
# CLI plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _print_report(rpt: ProbeReport, *, save_to: Optional[Path] = None) -> int:
    body = rpt.render_markdown()
    print(body)
    if save_to is not None:
        try:
            save_to.parent.mkdir(parents=True, exist_ok=True)
            save_to.write_text(body, encoding="utf-8")
            print(f"\n(report also saved to {save_to})")
        except OSError as exc:
            print(f"\n(warning: could not save report to {save_to}: {exc})")
    return 0 if rpt.overall() in (VERDICT_OK, VERDICT_SKIPPED) else 1


def cmd_test_keychain_read(args: argparse.Namespace) -> int:
    return _print_report(probe_keychain_read())


def cmd_test_keychain_write(args: argparse.Namespace) -> int:
    return _print_report(probe_keychain_write())


def cmd_test_refresh_flush(args: argparse.Namespace) -> int:
    return _print_report(probe_refresh_flush())


def cmd_test_full_probe(args: argparse.Namespace) -> int:
    save_to = home_dir() / "probe-report.md"
    return _print_report(probe_full(), save_to=save_to)


def register_test_subcommands(sub) -> None:
    """Wire the ``test`` command tree onto an argparse subparsers
    object. Called from ``portal/cli.py``'s ``build_parser``.
    """
    test = sub.add_parser(
        "test",
        help="Diagnostic probes for macOS Keychain credential management",
        description=(
            "Run probes to validate the assumptions used by the "
            "macOS-side claude credential manager. Designed to be run "
            "by macOS users on a real host; non-Darwin platforms get "
            "SKIPPED for each probe."
        ),
    )
    test_sub = test.add_subparsers(dest="test_cmd", required=True)

    test_sub.add_parser(
        "keychain-read",
        help="Check that `security find-generic-password` succeeds.",
    ).set_defaults(func=cmd_test_keychain_read)

    test_sub.add_parser(
        "keychain-write",
        help="Check `security add-generic-password -U` upsert works.",
    ).set_defaults(func=cmd_test_keychain_write)

    test_sub.add_parser(
        "refresh-flush",
        help="Run `claude --print` against the real Keychain and check "
        "whether the OAuth token rotates on the way out (passive; "
        "expects current token to still be valid).",
    ).set_defaults(func=cmd_test_refresh_flush)

    test_sub.add_parser(
        "full-probe",
        help="Run every probe end-to-end and write a single "
        "report to ~/.puffo-agent/probe-report.md.",
    ).set_defaults(func=cmd_test_full_probe)
