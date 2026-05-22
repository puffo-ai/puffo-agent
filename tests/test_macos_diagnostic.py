"""Tests for ``puffo_agent.portal.diagnostic`` — the ``puffo-agent test``
command tree.

Most of the report content depends on real macOS Keychain state, which
we can't have on a CI runner; these tests focus on:

  * Report rendering (markdown shape, verdict propagation).
  * Token redaction (no raw secrets in output).
  * Off-macOS code path (every probe reports SKIPPED cleanly).
  * On-macOS happy path with subprocess / keychain mocked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffo_agent.macos import keychain as cm
from puffo_agent.portal import diagnostic as diag


_BLOB = json.dumps({
    "claudeAiOauth": {
        "accessToken": "sk-ant-AAAA",
        "refreshToken": "rt-BBBB",
        "expiresAt": 9_999_999_000,
    },
})


# ─────────────────────────────────────────────────────────────────────────────
# Report rendering
# ─────────────────────────────────────────────────────────────────────────────

def test_report_overall_verdict_aggregation():
    rpt = diag.ProbeReport(title="t")
    rpt.add("a", diag.VERDICT_OK)
    rpt.add("b", diag.VERDICT_OK)
    assert rpt.overall() == diag.VERDICT_OK

    rpt.add("c", diag.VERDICT_NEEDS_ATTENTION)
    assert rpt.overall() == diag.VERDICT_NEEDS_ATTENTION

    rpt.add("d", diag.VERDICT_FAIL)
    assert rpt.overall() == diag.VERDICT_FAIL


def test_report_all_skipped_overall():
    rpt = diag.ProbeReport(title="t")
    rpt.add("a", diag.VERDICT_SKIPPED)
    rpt.add("b", diag.VERDICT_SKIPPED)
    assert rpt.overall() == diag.VERDICT_SKIPPED


def test_report_renders_markdown():
    rpt = diag.ProbeReport(title="hello")
    rpt.add("step-1", diag.VERDICT_OK, "detail line")
    rpt.summary = "all good"
    md = rpt.render_markdown()
    assert md.startswith("# hello")
    assert "## step-1 — OK" in md
    assert "detail line" in md
    assert "all good" in md


# ─────────────────────────────────────────────────────────────────────────────
# Token redaction — the most important property
# ─────────────────────────────────────────────────────────────────────────────

def test_redact_token_omits_raw_value():
    out = diag._redact_token("sk-ant-supersecret-9999")
    assert "supersecret" not in out
    assert "len=" in out
    assert "sha256_prefix=" in out


def test_summarise_blob_redacts_oauth_tokens():
    summary = diag._summarise_blob(_BLOB)
    assert "AAAA" not in summary
    assert "BBBB" not in summary
    assert "len=" in summary
    assert "claudeAiOauth" in summary


def test_summarise_blob_handles_invalid_json():
    summary = diag._summarise_blob("not json")
    assert "invalid JSON" in summary


# ─────────────────────────────────────────────────────────────────────────────
# Off-macOS: every probe returns SKIPPED cleanly
# ─────────────────────────────────────────────────────────────────────────────

def test_keychain_read_skipped_off_macos(monkeypatch):
    monkeypatch.setattr(cm, "is_macos", lambda: False)
    monkeypatch.setattr(diag, "is_macos", lambda: False)
    rpt = diag.probe_keychain_read()
    assert rpt.overall() == diag.VERDICT_SKIPPED


def test_keychain_write_skipped_off_macos(monkeypatch):
    monkeypatch.setattr(cm, "is_macos", lambda: False)
    monkeypatch.setattr(diag, "is_macos", lambda: False)
    rpt = diag.probe_keychain_write()
    assert rpt.overall() == diag.VERDICT_SKIPPED


def test_refresh_flush_skipped_off_macos(monkeypatch):
    monkeypatch.setattr(cm, "is_macos", lambda: False)
    monkeypatch.setattr(diag, "is_macos", lambda: False)
    rpt = diag.probe_refresh_flush()
    assert rpt.overall() == diag.VERDICT_SKIPPED


def test_keychain_survives_token_env_skipped_off_macos(monkeypatch):
    monkeypatch.setattr(cm, "is_macos", lambda: False)
    monkeypatch.setattr(diag, "is_macos", lambda: False)
    rpt = diag.probe_keychain_survives_token_env()
    assert rpt.overall() == diag.VERDICT_SKIPPED


def test_full_probe_off_macos(monkeypatch):
    monkeypatch.setattr(cm, "is_macos", lambda: False)
    monkeypatch.setattr(diag, "is_macos", lambda: False)
    rpt = diag.probe_full()
    # Sub-probes are skipped, but the env-step is always OK; that's
    # acceptable — the operator sees skipped sub-reports.
    assert rpt.overall() in (diag.VERDICT_OK, diag.VERDICT_SKIPPED)


# ─────────────────────────────────────────────────────────────────────────────
# On-macOS: keychain-read happy path with mocked subprocess
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCompletedProcess:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_keychain_read_success_includes_redacted_blob(monkeypatch):
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    monkeypatch.setattr(diag, "is_macos", lambda: True)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout=_BLOB),
    )
    rpt = diag.probe_keychain_read()
    assert rpt.overall() == diag.VERDICT_OK
    body = rpt.render_markdown()
    assert "AAAA" not in body
    assert "BBBB" not in body


def test_keychain_write_roundtrip_success(monkeypatch):
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    monkeypatch.setattr(diag, "is_macos", lambda: True)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(0, stdout=_BLOB),
    )
    rpt = diag.probe_keychain_write()
    assert rpt.overall() == diag.VERDICT_OK


# ─────────────────────────────────────────────────────────────────────────────
# Forced-expiry helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_force_expiry_mutates_expires_at():
    import time
    out = diag._force_expiry(_BLOB, seconds_in_past=120)
    assert out is not None
    parsed = json.loads(out)
    expires_ms = parsed["claudeAiOauth"]["expiresAt"]
    # expiresAt should now be in the past (within the last few seconds).
    assert expires_ms < int(time.time() * 1000)
    # accessToken and refreshToken should be preserved verbatim — we only
    # touched expiresAt.
    assert parsed["claudeAiOauth"]["accessToken"] == "sk-ant-AAAA"
    assert parsed["claudeAiOauth"]["refreshToken"] == "rt-BBBB"


def test_force_expiry_rejects_malformed_blob():
    assert diag._force_expiry("not json") is None


def test_force_expiry_rejects_missing_oauth_dict():
    assert diag._force_expiry(json.dumps({"unrelated": 1})) is None


def test_access_token_extraction():
    assert diag._access_token(_BLOB) == "sk-ant-AAAA"
    assert diag._access_token("not json") == ""
    assert diag._access_token(json.dumps({})) == ""


# ─────────────────────────────────────────────────────────────────────────────
# refresh-flush-forced subcommand
# ─────────────────────────────────────────────────────────────────────────────

def test_refresh_flush_forced_skipped_off_macos(monkeypatch):
    monkeypatch.setattr(cm, "is_macos", lambda: False)
    monkeypatch.setattr(diag, "is_macos", lambda: False)
    rpt = diag.probe_refresh_flush_forced()
    assert rpt.overall() == diag.VERDICT_SKIPPED


def test_refresh_flush_forced_requires_yes_flag(monkeypatch, capsys):
    """The forced subcommand has destructive side effects; without
    --yes it must refuse and exit non-zero."""
    import argparse
    ns = argparse.Namespace(yes=False)
    code = diag.cmd_test_refresh_flush_forced(ns)
    assert code == 2
    captured = capsys.readouterr()
    assert "rotates" in captured.err.lower()
    assert "--yes" in captured.err


# ─────────────────────────────────────────────────────────────────────────────
# refresh-flush-forced — rc-vs-rotation reconciliation
#
# The probe must decide writeback + verdict from the observed Keychain
# and sandbox-creds state after the run, not from claude's exit code.
# Pre-fix it took an rc-based early return and either dropped a real
# rotation (stranding the user with an Anthropic-invalidated RT) or
# falsely reported "Keychain untouched" when claude had already updated
# the Keychain via its own native write.
# ─────────────────────────────────────────────────────────────────────────────

_BLOB_NEW = json.dumps({
    "claudeAiOauth": {
        "accessToken": "sk-ant-NEW",
        "refreshToken": "rt-NEW",
        "expiresAt": 9_999_999_500,
    },
})


def _force_macos_for_probe(monkeypatch):
    monkeypatch.setattr(cm, "is_macos", lambda: True)
    monkeypatch.setattr(diag, "is_macos", lambda: True)
    monkeypatch.setattr(diag.shutil, "which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setattr(diag, "install_path_shim", lambda home: home / "shim")


def _stub_keychain_reads(monkeypatch, prerequisite_blob, post_blob):
    """Stub ``read_keychain_blob`` so the prerequisite read returns one
    blob and the post-sandbox read returns another."""
    calls = {"n": 0}

    def fake_read():
        calls["n"] += 1
        blob = prerequisite_blob if calls["n"] == 1 else post_blob
        return cm.KeychainReadResult(True, blob, None, None)

    monkeypatch.setattr(diag, "read_keychain_blob", fake_read)


def _stub_subprocess(monkeypatch, *, sandbox_after_blob, rc):
    """Stub ``subprocess.run`` so the sandbox claude appears to write
    the given blob (or nothing, if None) and exits with ``rc``."""
    def fake_run(*args, cwd=None, **kwargs):
        if sandbox_after_blob is not None:
            Path(cwd, ".claude", ".credentials.json").write_text(
                sandbox_after_blob, encoding="utf-8",
            )
        return _FakeCompletedProcess(rc, stdout="", stderr="downstream boom")
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_forced_rc_nonzero_but_sandbox_rotated_triggers_writeback(monkeypatch):
    """claude rotated, wrote the new blob to the sandbox file, then
    exited non-zero. Keychain still holds the original (claude didn't
    rewrite it). Probe MUST writeback so main CLI doesn't get stranded
    on the Anthropic-invalidated RT."""
    _force_macos_for_probe(monkeypatch)
    # Prerequisite read + post read both return the original — claude
    # only updated the sandbox file, not Keychain.
    _stub_keychain_reads(monkeypatch, _BLOB, _BLOB)
    _stub_subprocess(monkeypatch, sandbox_after_blob=_BLOB_NEW, rc=1)

    written = []
    monkeypatch.setattr(
        diag, "writeback_to_keychain",
        lambda blob: (written.append(blob) or (True, None)),
    )

    rpt = diag.probe_refresh_flush_forced()
    body = rpt.render_markdown()

    # Salvaged rotation → overall NEEDS_ATTENTION, not FAIL.
    assert rpt.overall() == diag.VERDICT_NEEDS_ATTENTION, body
    assert "rotation detected" in body
    assert "written back to Keychain" in body
    # Writeback was called with the rotated blob.
    assert len(written) == 1
    assert json.loads(written[0])["claudeAiOauth"]["accessToken"] == "sk-ant-NEW"


def test_forced_rc_nonzero_with_keychain_rotated_skips_writeback(monkeypatch):
    """claude wrote the new blob directly into the real Keychain (HOME
    doesn't isolate Keychain) and then exited non-zero without updating
    the sandbox file. Keychain is already correct; writeback would be a
    no-op at best and a clobber at worst — must NOT be called."""
    _force_macos_for_probe(monkeypatch)
    # Prerequisite read returns original; post read returns rotated.
    _stub_keychain_reads(monkeypatch, _BLOB, _BLOB_NEW)
    # Sandbox file unchanged from the mutated blob.
    _stub_subprocess(monkeypatch, sandbox_after_blob=None, rc=1)

    written = []
    monkeypatch.setattr(
        diag, "writeback_to_keychain",
        lambda blob: (written.append(blob) or (True, None)),
    )

    rpt = diag.probe_refresh_flush_forced()
    body = rpt.render_markdown()

    assert rpt.overall() == diag.VERDICT_NEEDS_ATTENTION, body
    assert "claude wrote it directly" in body or "claude's direct write" in body
    assert written == [], (
        "writeback must NOT be called when Keychain already has the "
        "rotated token — would clobber a fresh blob with stale data"
    )


def test_forced_rc_nonzero_with_no_rotation_is_real_failure(monkeypatch):
    """claude exited non-zero AND neither Keychain nor sandbox file
    shows rotation. Real failure; Keychain unchanged; main CLI safe.
    Must report FAIL without any writeback."""
    _force_macos_for_probe(monkeypatch)
    _stub_keychain_reads(monkeypatch, _BLOB, _BLOB)
    _stub_subprocess(monkeypatch, sandbox_after_blob=None, rc=1)

    written = []
    monkeypatch.setattr(
        diag, "writeback_to_keychain",
        lambda blob: (written.append(blob) or (True, None)),
    )

    rpt = diag.probe_refresh_flush_forced()
    body = rpt.render_markdown()

    assert rpt.overall() == diag.VERDICT_FAIL, body
    assert "did not produce a rotation" in body or "Keychain unchanged" in body
    assert written == []


def test_forced_rc_zero_happy_path_writes_back(monkeypatch):
    """Sanity: the original happy path (claude exits 0, sandbox file
    has a rotated token, Keychain still on the original) still works
    end-to-end with writeback."""
    _force_macos_for_probe(monkeypatch)
    _stub_keychain_reads(monkeypatch, _BLOB, _BLOB)
    _stub_subprocess(monkeypatch, sandbox_after_blob=_BLOB_NEW, rc=0)

    written = []
    monkeypatch.setattr(
        diag, "writeback_to_keychain",
        lambda blob: (written.append(blob) or (True, None)),
    )

    rpt = diag.probe_refresh_flush_forced()
    body = rpt.render_markdown()

    assert rpt.overall() == diag.VERDICT_OK, body
    assert len(written) == 1
    assert json.loads(written[0])["claudeAiOauth"]["accessToken"] == "sk-ant-NEW"
