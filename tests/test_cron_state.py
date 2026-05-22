"""PUF-239: per-agent cron state persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from _bridge_support import isolated_home, write_test_agent
from puffo_agent.portal.cron_state import (
    CronSchedule,
    crons_path,
    disable_cron,
    load_crons,
    new_cron_id,
    save_crons,
    upsert_cron,
    validate_schedule,
)


# ────────────────────────────────────────────────────────────────────
# validate_schedule
# ────────────────────────────────────────────────────────────────────


def test_validate_schedule_accepts_5_field_crontab():
    ok, reason = validate_schedule("0 9 * * *")
    assert ok is True
    assert reason == ""


def test_validate_schedule_accepts_complex_expression():
    ok, _ = validate_schedule("*/15 8-18 * * 1-5")
    assert ok is True


def test_validate_schedule_rejects_empty_string():
    ok, reason = validate_schedule("")
    assert ok is False
    assert "non-empty" in reason


def test_validate_schedule_rejects_non_string():
    ok, reason = validate_schedule(None)  # type: ignore[arg-type]
    assert ok is False


def test_validate_schedule_rejects_bad_expression():
    ok, reason = validate_schedule("not a cron")
    assert ok is False
    assert "invalid" in reason.lower()


def test_validate_schedule_rejects_out_of_range_field():
    ok, _ = validate_schedule("0 25 * * *")  # hour 25 doesn't exist
    assert ok is False


# ────────────────────────────────────────────────────────────────────
# new_cron_id
# ────────────────────────────────────────────────────────────────────


def test_new_cron_id_has_prefix_and_uniqueness():
    a = new_cron_id()
    b = new_cron_id()
    assert a.startswith("cron_")
    assert b.startswith("cron_")
    assert a != b
    # Short-form: prefix + 12 hex chars.
    assert len(a) == 5 + 12


# ────────────────────────────────────────────────────────────────────
# load_crons / save_crons round-trip
# ────────────────────────────────────────────────────────────────────


def _make_cron(cron_id: str = "cron_aaaaaaaaaaaa", **overrides):
    base = dict(
        id=cron_id,
        schedule="0 9 * * *",
        prompt="report ticket status",
        enabled=True,
        created_at=1716372000000,
        last_fire=None,
        fire_count=0,
    )
    base.update(overrides)
    return CronSchedule(**base)


def test_load_returns_empty_for_missing_file():
    isolated_home()
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-empty")
    assert load_crons("agt-empty") == []


def test_save_then_load_round_trip():
    isolated_home()
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-rt")
    cron = _make_cron("cron_aabbccddeeff")
    save_crons("agt-rt", [cron])

    loaded = load_crons("agt-rt")
    assert len(loaded) == 1
    assert loaded[0].id == "cron_aabbccddeeff"
    assert loaded[0].schedule == "0 9 * * *"
    assert loaded[0].prompt == "report ticket status"


def test_save_is_atomic_via_temp_file():
    # The save helper writes to ``.crons.json.tmp`` then ``os.replace``.
    # We can't easily intercept the temp file, but we can verify the
    # final file shape contains valid JSON + the expected wrapper.
    isolated_home()
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-atomic")
    save_crons("agt-atomic", [_make_cron("cron_a1")])

    path = crons_path("agt-atomic")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "crons" in raw
    assert raw["crons"][0]["id"] == "cron_a1"


def test_load_drops_malformed_rows_silently():
    isolated_home()
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-malformed")
    path = crons_path("agt-malformed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "crons": [
                # Valid row.
                {
                    "id": "cron_good",
                    "schedule": "0 9 * * *",
                    "prompt": "ok",
                    "enabled": True,
                },
                # Malformed — missing required field; should be dropped.
                {"id": "cron_bad"},
                # Not a dict — should be skipped.
                "not a dict",
            ],
        }),
        encoding="utf-8",
    )
    loaded = load_crons("agt-malformed")
    assert len(loaded) == 1
    assert loaded[0].id == "cron_good"


def test_load_tolerates_corrupt_json():
    isolated_home()
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-corrupt")
    path = crons_path("agt-corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{this is not json", encoding="utf-8")
    # Should return empty rather than raise — the scheduler can't
    # afford to crash on one agent's bad file.
    assert load_crons("agt-corrupt") == []


# ────────────────────────────────────────────────────────────────────
# upsert_cron + disable_cron
# ────────────────────────────────────────────────────────────────────


def test_upsert_cron_inserts_when_absent():
    isolated_home()
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-up")
    saved = upsert_cron("agt-up", _make_cron("cron_one"))
    assert saved.id == "cron_one"
    loaded = load_crons("agt-up")
    assert len(loaded) == 1


def test_upsert_cron_replaces_when_id_matches():
    isolated_home()
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-replace")
    upsert_cron("agt-replace", _make_cron("cron_x", schedule="0 9 * * *"))
    upsert_cron(
        "agt-replace",
        _make_cron("cron_x", schedule="*/5 * * * *", fire_count=42),
    )
    loaded = load_crons("agt-replace")
    assert len(loaded) == 1
    assert loaded[0].schedule == "*/5 * * * *"
    assert loaded[0].fire_count == 42


def test_disable_cron_flips_enabled_false():
    isolated_home()
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-dis")
    upsert_cron("agt-dis", _make_cron("cron_active", enabled=True))
    updated = disable_cron("agt-dis", "cron_active")
    assert updated is not None
    assert updated.enabled is False
    # Persisted to disk too.
    assert load_crons("agt-dis")[0].enabled is False


def test_disable_cron_returns_none_for_unknown_id():
    isolated_home()
    write_test_agent(os.environ["PUFFO_AGENT_HOME"], "agt-unknown")
    assert disable_cron("agt-unknown", "cron_doesnt_exist") is None
