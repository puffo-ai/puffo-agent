"""``puffo-agent test ...`` — macOS Keychain integration probes.

Designed to be run by a colleague on a real macOS host so we can
verify the assumptions baked into ``puffo_agent.macos.credential_manager``
*before* promoting v0.9.0a* from TestPyPI to PyPI. Each subcommand:

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
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..macos.credential_manager import (
    KEYCHAIN_SERVICE,
    CredentialCache,
    install_path_shim,
    is_macos,
    read_keychain_blob,
    shim_dir,
    writeback_to_keychain,
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
    """Pretty-print Claude Code's credential blob with token fields
    redacted. Output goes to stdout so it MUST be safe to paste."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return f"(invalid JSON, length={len(raw)})"
    oauth = (data.get("claudeAiOauth") or {}).copy()
    if "accessToken" in oauth:
        oauth["accessToken"] = _redact_token(oauth["accessToken"])
    if "refreshToken" in oauth:
        oauth["refreshToken"] = _redact_token(oauth["refreshToken"])
    other_keys = sorted(k for k in data.keys() if k != "claudeAiOauth")
    pretty = {"claudeAiOauth": oauth, "other_keys": other_keys}
    return json.dumps(pretty, indent=2, sort_keys=True)


# ─────────────────────────────────────────────────────────────────────────────
# Probes
# ─────────────────────────────────────────────────────────────────────────────

def probe_keychain_read() -> ProbeReport:
    """Step 1: can we read the Keychain entry?"""
    rpt = ProbeReport(title="puffo-agent test keychain-read")
    if not is_macos():
        rpt.add("platform-check", VERDICT_SKIPPED, "not Darwin — probe skipped")
        return rpt

    rpt.add(
        "command",
        VERDICT_OK,
        f"security find-generic-password -s '{KEYCHAIN_SERVICE}' -w",
    )

    started = time.time()
    result = read_keychain_blob()
    elapsed_ms = int((time.time() - started) * 1000)

    if not result.ok:
        # First-time ACL grant: stderr typically contains "user
        # interaction is not allowed" if running under a non-TTY parent,
        # or the result is just slow when the dialog is up.
        rpt.add(
            "read",
            VERDICT_FAIL,
            f"reason: {result.error}\nstderr: {result.stderr or '(none)'}\n"
            f"elapsed_ms: {elapsed_ms}",
        )
        rpt.summary = (
            "Reading Keychain failed. If this is the first time, you "
            "may need to click 'Always Allow' on a system dialog."
        )
        return rpt

    rpt.add(
        "read",
        VERDICT_OK,
        f"got blob: {_summarise_blob(result.blob)}\nelapsed_ms: {elapsed_ms}",
    )
    rpt.summary = (
        "Keychain read succeeded. Bootstrap path will work."
    )
    return rpt


def probe_keychain_write() -> ProbeReport:
    """Step 2: can we *write* to the Keychain (preserving content)?"""
    rpt = ProbeReport(title="puffo-agent test keychain-write")
    if not is_macos():
        rpt.add("platform-check", VERDICT_SKIPPED, "not Darwin — probe skipped")
        return rpt

    # Read existing.
    read_result = read_keychain_blob()
    if not read_result.ok:
        rpt.add(
            "prerequisite-read",
            VERDICT_FAIL,
            f"can't read keychain to back up: {read_result.error}; "
            "run `puffo-agent test keychain-read` first.",
        )
        return rpt
    rpt.add("prerequisite-read", VERDICT_OK, "backup captured in memory")

    # Write the same value back via -U upsert.
    started = time.time()
    write_ok, write_reason = writeback_to_keychain(read_result.blob)
    elapsed_ms = int((time.time() - started) * 1000)

    if not write_ok:
        rpt.add(
            "write",
            VERDICT_NEEDS_ATTENTION,
            f"writeback failed: {write_reason}\nelapsed_ms: {elapsed_ms}\n"
            "Daemon will still work — writeback is best-effort; this just "
            "means user's main CLI won't see refreshed tokens.",
        )
        rpt.summary = (
            "Keychain write was rejected. Operation will degrade "
            "gracefully — agents still authenticated, but main CLI may "
            "need re-login after long sessions."
        )
        return rpt
    rpt.add("write", VERDICT_OK, f"elapsed_ms: {elapsed_ms}")

    # Re-read to confirm Keychain entry survives.
    reread = read_keychain_blob()
    if not reread.ok:
        rpt.add(
            "verify-read",
            VERDICT_FAIL,
            f"after write, Keychain read FAILED: {reread.error}",
        )
        rpt.summary = "DANGER: write may have corrupted Keychain entry."
        return rpt
    if reread.blob == read_result.blob:
        rpt.add("verify-read", VERDICT_OK, "post-write blob matches pre-write blob")
        rpt.summary = "Keychain write+read roundtrip works."
    else:
        rpt.add(
            "verify-read",
            VERDICT_NEEDS_ATTENTION,
            "post-write blob DIFFERS from pre-write blob — could be a "
            "concurrent refresh by your main CLI.",
        )
        rpt.summary = "Roundtrip OK but content changed — likely benign."
    return rpt


def probe_refresh_flush() -> ProbeReport:
    """Step 3: does a sandboxed ``claude --print`` actually flush a
    refreshed token to its ``.credentials.json``?

    This validates the core mechanism of ``refresh_via_oneshot``. If
    Claude has changed its flush behaviour in some new version, this
    probe will catch it.
    """
    rpt = ProbeReport(title="puffo-agent test refresh-flush")
    if not is_macos():
        rpt.add("platform-check", VERDICT_SKIPPED, "not Darwin — probe skipped")
        return rpt
    if shutil.which("claude") is None:
        rpt.add(
            "prerequisite-claude",
            VERDICT_FAIL,
            "`claude` not on PATH — install Claude Code first.",
        )
        return rpt

    read_result = read_keychain_blob()
    if not read_result.ok:
        rpt.add(
            "prerequisite-keychain",
            VERDICT_FAIL,
            f"keychain read failed: {read_result.error}",
        )
        return rpt
    rpt.add("prerequisite-keychain", VERDICT_OK, "blob in memory")

    home = home_dir()
    shim_path = install_path_shim(home)

    with tempfile.TemporaryDirectory(prefix="puffo-agent-test-refresh-") as sandbox:
        sandbox_path = Path(sandbox)
        sandbox_claude_dir = sandbox_path / ".claude"
        sandbox_claude_dir.mkdir(parents=True, exist_ok=True)
        sandbox_creds = sandbox_claude_dir / ".credentials.json"
        sandbox_creds.write_text(read_result.blob, encoding="utf-8")
        pre_mtime = sandbox_creds.stat().st_mtime
        pre_size = sandbox_creds.stat().st_size

        env = {
            **os.environ,
            "HOME": str(sandbox_path),
            "USERPROFILE": str(sandbox_path),
            "CLAUDE_CONFIG_DIR": str(sandbox_claude_dir),
            "PATH": f"{shim_path}{os.pathsep}{os.environ.get('PATH', '')}",
        }

        started = time.time()
        try:
            proc = subprocess.run(
                [
                    "claude", "--dangerously-skip-permissions",
                    "--print", "--max-turns", "1",
                    "--output-format", "stream-json", "--verbose",
                    "ok",
                ],
                env=env, cwd=str(sandbox_path),
                capture_output=True, text=True, timeout=90,
            )
        except subprocess.TimeoutExpired:
            rpt.add("claude-oneshot", VERDICT_FAIL, "timeout after 90s")
            return rpt
        elapsed_ms = int((time.time() - started) * 1000)

        if proc.returncode != 0:
            rpt.add(
                "claude-oneshot",
                VERDICT_FAIL,
                f"exit={proc.returncode}, elapsed_ms={elapsed_ms}\n"
                f"stderr (last 500): {(proc.stderr or '')[-500:]}",
            )
            return rpt
        rpt.add(
            "claude-oneshot",
            VERDICT_OK,
            f"exit=0, elapsed_ms={elapsed_ms}",
        )

        try:
            refreshed = sandbox_creds.read_text(encoding="utf-8")
        except FileNotFoundError:
            rpt.add(
                "credentials-file",
                VERDICT_FAIL,
                "sandbox .credentials.json was DELETED — flush failed",
            )
            return rpt

        post_mtime = sandbox_creds.stat().st_mtime
        post_size = sandbox_creds.stat().st_size

        try:
            new_token = (
                (json.loads(refreshed).get("claudeAiOauth") or {}).get("accessToken")
            ) or ""
        except json.JSONDecodeError:
            rpt.add(
                "credentials-file",
                VERDICT_FAIL,
                "sandbox .credentials.json wrote but is no longer valid JSON",
            )
            return rpt

        try:
            old_token = (
                (json.loads(read_result.blob).get("claudeAiOauth") or {}).get(
                    "accessToken"
                )
            ) or ""
        except json.JSONDecodeError:
            old_token = ""

        rotated = bool(new_token) and old_token != new_token
        rpt.add(
            "credentials-file",
            VERDICT_OK if rotated else VERDICT_NEEDS_ATTENTION,
            "mtime delta: {:+.2f}s\nsize delta: {:+d} bytes\ntoken rotated: {}\n"
            "old: {}\nnew: {}".format(
                post_mtime - pre_mtime,
                post_size - pre_size,
                rotated,
                _redact_token(old_token),
                _redact_token(new_token),
            ),
        )

    if rotated:
        rpt.summary = "Refresh flush works — sandbox flush mechanism confirmed."
    else:
        rpt.summary = (
            "Sandbox claude exited OK but the token wasn't rotated. "
            "Likely just means the current token is still valid; rerun "
            "again after token expiry to confirm refresh-on-expiry."
        )
    return rpt


def probe_keychain_survives_token_env() -> ProbeReport:
    """Step 4: does Claude Code still delete the Keychain entry when
    ``CLAUDE_CODE_OAUTH_TOKEN`` is set? Reproduces github issue #37512.

    If this comes back FAIL, the daemon MUST use the PATH shim to
    block the deletion call (which it does by default).
    """
    rpt = ProbeReport(title="puffo-agent test keychain-survives-token-env")
    if not is_macos():
        rpt.add("platform-check", VERDICT_SKIPPED, "not Darwin — probe skipped")
        return rpt
    if shutil.which("claude") is None:
        rpt.add(
            "prerequisite-claude",
            VERDICT_FAIL,
            "`claude` not on PATH — install Claude Code first.",
        )
        return rpt

    pre = read_keychain_blob()
    if not pre.ok:
        rpt.add(
            "prerequisite-keychain",
            VERDICT_FAIL,
            f"keychain read failed before probe: {pre.error}",
        )
        return rpt
    rpt.add("prerequisite-keychain", VERDICT_OK, "Keychain entry exists pre-probe")

    try:
        access_token = (
            (json.loads(pre.blob).get("claudeAiOauth") or {}).get("accessToken")
        ) or ""
    except json.JSONDecodeError:
        rpt.add(
            "prerequisite-keychain",
            VERDICT_FAIL,
            "Keychain blob is invalid JSON",
        )
        return rpt

    home = home_dir()
    shim_path = install_path_shim(home)

    # First sub-probe: WITHOUT shim (baseline — should reproduce bug).
    rpt.add(
        "approach",
        VERDICT_OK,
        "Will run two claude oneshots back-to-back:\n"
        "  A) PATH=$PATH (no shim), CLAUDE_CODE_OAUTH_TOKEN set\n"
        "  B) PATH=<shim>:$PATH, CLAUDE_CODE_OAUTH_TOKEN set\n"
        "Re-reads keychain after each. We expect A to delete (or be a\n"
        "no-op if Anthropic fixed #37512), and B to never delete.",
    )

    def _run_oneshot(path_value: str) -> tuple[int, str]:
        env = {
            **os.environ,
            "CLAUDE_CODE_OAUTH_TOKEN": access_token,
            "PATH": path_value,
        }
        try:
            proc = subprocess.run(
                [
                    "claude", "--dangerously-skip-permissions",
                    "--print", "--max-turns", "1",
                    "--output-format", "stream-json", "--verbose",
                    "ok",
                ],
                env=env, capture_output=True, text=True, timeout=60,
            )
            return (proc.returncode, (proc.stderr or "")[-300:])
        except subprocess.TimeoutExpired:
            return (-1, "timeout")

    # A: without shim
    code_a, stderr_a = _run_oneshot(os.environ.get("PATH", ""))
    post_a = read_keychain_blob()
    if post_a.ok:
        rpt.add(
            "without-shim",
            VERDICT_OK,
            f"claude exit={code_a}; Keychain entry STILL PRESENT — "
            "Anthropic may have fixed #37512, OR the deletion path "
            "didn't trigger in this particular invocation.",
        )
    else:
        rpt.add(
            "without-shim",
            VERDICT_NEEDS_ATTENTION,
            f"claude exit={code_a}; Keychain entry DELETED after run "
            f"(reason: {post_a.error})\nstderr tail: {stderr_a}\n"
            "This reproduces #37512 — restoring entry before next "
            "sub-probe.",
        )
        # Restore so the user doesn't lose their main-CLI auth.
        writeback_to_keychain(pre.blob)

    # B: with shim
    code_b, stderr_b = _run_oneshot(
        f"{shim_path}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    post_b = read_keychain_blob()
    if post_b.ok:
        rpt.add(
            "with-shim",
            VERDICT_OK,
            f"claude exit={code_b}; Keychain entry preserved. "
            "Shim is doing its job.",
        )
    else:
        rpt.add(
            "with-shim",
            VERDICT_FAIL,
            f"claude exit={code_b}; Keychain entry DELETED EVEN WITH "
            f"SHIM (reason: {post_b.error})\nstderr tail: {stderr_b}\n"
            "The shim is NOT being honoured — check PATH ordering.",
        )
        writeback_to_keychain(pre.blob)

    # Verdict summary.
    pre_lost = not post_a.ok and not post_b.ok
    if pre_lost:
        rpt.summary = (
            "Critical: shim did not protect Keychain. Investigate before "
            "shipping."
        )
    elif not post_a.ok:
        rpt.summary = (
            "Bug #37512 still present in this Claude Code version; "
            "shim protects against it. Ship-ready."
        )
    else:
        rpt.summary = (
            "Could not reproduce #37512 in this run. Shim is harmless "
            "to leave in place; we keep it for safety against re-"
            "regression."
        )
    return rpt


def probe_full() -> ProbeReport:
    """Run every probe end-to-end and return a single combined report."""
    rpt = ProbeReport(title="puffo-agent test full-probe")
    rpt.add(
        "environment",
        VERDICT_OK,
        f"home_dir: {home_dir()}\nclaude: {shutil.which('claude')}\n"
        f"shim_dir: {shim_dir(home_dir())}",
    )

    sub_reports = [
        probe_keychain_read(),
        probe_keychain_write(),
        probe_refresh_flush(),
        probe_keychain_survives_token_env(),
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
        rpt.summary = "All probes green. Ship 0.9.0 to PyPI."
    elif overall == VERDICT_NEEDS_ATTENTION:
        rpt.summary = (
            "Some probes flagged NEEDS_ATTENTION — review each item "
            "before promoting from TestPyPI."
        )
    else:
        rpt.summary = "At least one probe FAILED. Do not promote."
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


def cmd_test_keychain_survives_token_env(
    args: argparse.Namespace,
) -> int:
    return _print_report(probe_keychain_survives_token_env())


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
        help="Run a sandboxed `claude --print` and check that the "
        "OAuth token is rotated on the way out.",
    ).set_defaults(func=cmd_test_refresh_flush)

    test_sub.add_parser(
        "keychain-survives-token-env",
        help="Reproduce GitHub issue #37512: does setting "
        "CLAUDE_CODE_OAUTH_TOKEN delete the Keychain entry? Verify the "
        "shim protects against it.",
    ).set_defaults(func=cmd_test_keychain_survives_token_env)

    test_sub.add_parser(
        "full-probe",
        help="Run every probe end-to-end and write a single "
        "report to ~/.puffo-agent/probe-report.md.",
    ).set_defaults(func=cmd_test_full_probe)
