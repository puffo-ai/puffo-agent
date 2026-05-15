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

from puffo_agent.macos import credential_manager as cm
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
