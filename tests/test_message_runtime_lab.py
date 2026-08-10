from __future__ import annotations

import json

import pytest

import scripts.message_runtime_lab as runtime_lab
from scripts.message_runtime_lab import (
    run_batch_scenario,
    run_count_scenario,
    snapshot_database,
)


@pytest.mark.asyncio
async def test_fifty_one_messages_execute_as_two_observable_runtime_turns(tmp_path):
    report = await run_batch_scenario(
        message_count=51,
        targets="mixed",
        output_dir=tmp_path / "batch-51",
    )

    assert report["ok"]
    assert report["observed_turn_sizes"] == [50, 1]
    assert report["turns"][0]["more_pending"] is True
    assert report["turns"][1]["more_pending"] is False
    targets = {
        tuple(target)
        for target in report["turns"][0]["targets"]
    }
    assert {
        ("channel", "space-lab", "channel-lab"),
        ("thread", "space-lab", "channel-lab", "thread-root"),
    }.issubset(targets)
    assert {
        target for target in targets if target[0] == "dm"
    } == {
        ("dm", "sender-0"),
        ("dm", "sender-1"),
        ("dm", "sender-2"),
        ("dm", "sender-3"),
    }
    assert report["turns"][1]["targets"] == [["dm", "sender-2"]]
    assert report["final_snapshot"]["counts"] == {"processed": 51}

    events = [
        json.loads(line)
        for line in (tmp_path / "batch-51" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["event"] for event in events].count("provider.input") == 2
    assert [event["event"] for event in events].count("provider.admitted") == 2


@pytest.mark.asyncio
async def test_five_agent_counting_race_is_contiguous_and_fully_observable(tmp_path):
    report = await run_count_scenario(
        agent_count=5,
        output_dir=tmp_path / "count-5",
    )

    assert report["ok"]
    assert [row["text"] for row in report["committed_messages"][1:]] == [
        "1",
        "2",
        "3",
        "4",
        "5",
    ]
    assert report["attempt_count"] == 15
    assert report["held_count"] == 10
    assert [row["held"] for row in report["rounds"]] == [4, 3, 2, 1, 0]
    assert len({row["winner"] for row in report["rounds"]}) == 5

    expected_processed = {
        row["winner"]: int(row["number"]) for row in report["rounds"]
    }
    for agent_id, snapshot in report["final_snapshots"].items():
        assert snapshot["counts"].get("pending", 0) == 0, agent_id
        assert snapshot["counts"].get("in_turn", 0) == 0, agent_id
        assert (
            snapshot["counts"].get("processed", 0)
            == expected_processed[agent_id]
        ), agent_id
        assert not snapshot["invariants"]["invalid_active_memberships"], agent_id


@pytest.mark.asyncio
async def test_readonly_snapshot_reports_turn_membership_without_content(tmp_path):
    report = await run_batch_scenario(
        message_count=2,
        targets="same-channel",
        output_dir=tmp_path / "snapshot",
    )
    snapshot = snapshot_database(tmp_path / "snapshot" / "agent-batch" / "messages.db")

    assert report["ok"]
    assert snapshot["counts"] == {"processed": 2}
    assert [turn["message_count"] for turn in snapshot["turns"]] == [2]
    assert [row["server_seq"] for row in snapshot["messages"]] == [1, 2]
    assert all("content" not in row for row in snapshot["messages"])


@pytest.mark.asyncio
async def test_readonly_monitor_watch_without_expectations_keeps_observing(
    tmp_path, monkeypatch
):
    await run_batch_scenario(
        message_count=1,
        targets="same-channel",
        output_dir=tmp_path / "monitor",
    )
    db_path = tmp_path / "monitor" / "agent-batch" / "messages.db"
    original_snapshot = runtime_lab.snapshot_database
    snapshots = 0

    def count_snapshot(path):
        nonlocal snapshots
        snapshots += 1
        return original_snapshot(path)

    monkeypatch.setattr(runtime_lab, "snapshot_database", count_snapshot)
    result = await runtime_lab.monitor_database(
        db_path=db_path,
        watch=True,
        interval=0.001,
        timeout=0.005,
        expect_processed=None,
        expect_turn_sizes=(),
        jsonl_path=None,
    )

    assert result == 0
    assert snapshots >= 2


@pytest.mark.asyncio
async def test_monitor_rejects_jsonl_path_that_aliases_database(tmp_path):
    await run_batch_scenario(
        message_count=1,
        targets="same-channel",
        output_dir=tmp_path / "monitor-alias",
    )
    db_path = tmp_path / "monitor-alias" / "agent-batch" / "messages.db"

    with pytest.raises(ValueError, match="must not"):
        await runtime_lab.monitor_database(
            db_path=db_path,
            watch=False,
            interval=0.001,
            timeout=0.005,
            expect_processed=None,
            expect_turn_sizes=(),
            jsonl_path=db_path,
        )
