"""Observable local scenarios for the durable Puffo Agent message runtime.

The lab uses the production SQLite MessageStore and GlobalInboxRuntime with a
scripted provider. The counting scenario uses an in-process atomic channel
gate; it does not claim to replace the real puffo-server/PostgreSQL test.

Examples:

    .venv/bin/python scripts/message_runtime_lab.py batch --messages 51
    .venv/bin/python scripts/message_runtime_lab.py count --agents 5
    .venv/bin/python scripts/message_runtime_lab.py monitor \
        --db ~/.puffo-agent/agents/<agent-id>/messages.db --watch
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from puffo_agent.agent.context_controller import (  # noqa: E402
    CompactionResult,
    ContextCapabilities,
    ContextSnapshot,
    ProviderAdmissionEvent,
    RolloverResult,
)
from puffo_agent.agent.global_inbox_runtime import (  # noqa: E402
    GlobalInboxRuntime,
)
from puffo_agent.agent.message_store import (  # noqa: E402
    MessageStore,
    ReceiptDisposition,
)

SCHEMA = "puffo.global-inbox-observation/v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EventLog:
    def __init__(self, path: Path, *, scenario: str, run_id: str):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scenario = scenario
        self.run_id = run_id
        self.index = 0

    def emit(self, event: str, *, agent_id: str = "", **data: Any) -> None:
        record = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "event_index": self.index,
            "timestamp": utc_now(),
            "monotonic_ns": time.monotonic_ns(),
            "scenario": self.scenario,
            "agent_id": agent_id,
            "event": event,
            "data": data,
        }
        self.index += 1
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True))
            handle.write("\n")


def _readonly_connection(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=1.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=1000")
    return connection


def _rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql).fetchall()]


def snapshot_database(db_path: Path) -> dict[str, Any]:
    """Read one WAL-aware, query-only Inbox snapshot."""
    with _readonly_connection(db_path) as connection:
        # Pin every query below to one WAL snapshot. query_only prevents writes;
        # BEGIN prevents a commit between SELECTs from producing a torn view.
        connection.execute("BEGIN")
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required = {"messages", "turn_runs", "turn_run_messages"}
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError(f"message runtime schema is missing: {', '.join(missing)}")

        counts = {
            (row["processing_state"] or "none"): row["count"]
            for row in connection.execute(
                "SELECT processing_state, COUNT(*) AS count "
                "FROM messages GROUP BY processing_state"
            ).fetchall()
        }
        messages = _rows(
            connection,
            """
            SELECT envelope_id, server_seq, after_server_seq, local_ordinal,
                   envelope_kind, space_id, channel_id, thread_root_id,
                   sender_slug, processing_state, processing_turn_id,
                   model_visible_at, processed_at
            FROM messages
            ORDER BY COALESCE(server_seq, after_server_seq, 0),
                     CASE WHEN server_seq IS NOT NULL THEN 0 ELSE 1 END,
                     local_ordinal, envelope_id
            """,
        )
        turns = _rows(
            connection,
            """
            SELECT tr.turn_id, tr.provider_session_id, tr.state,
                   tr.started_at, tr.completed_at,
                   MIN(COALESCE(m.server_seq, m.after_server_seq, 0))
                     AS first_message_seq,
                   COUNT(trm.envelope_id) AS message_count
            FROM turn_runs tr
            LEFT JOIN turn_run_messages trm ON trm.turn_id = tr.turn_id
            LEFT JOIN messages m ON m.envelope_id = trm.envelope_id
            GROUP BY tr.turn_id, tr.provider_session_id, tr.state,
                     tr.started_at, tr.completed_at
            ORDER BY first_message_seq, tr.started_at, tr.turn_id
            """,
        )
        memberships = _rows(
            connection,
            """
            SELECT trm.turn_id, trm.ordinal, m.envelope_id, m.server_seq,
                   m.processing_state, m.space_id, m.channel_id,
                   m.thread_root_id, m.envelope_kind
            FROM turn_run_messages trm
            JOIN messages m ON m.envelope_id = trm.envelope_id
            ORDER BY trm.turn_id, trm.ordinal
            """,
        )
        invalid_active_memberships = _rows(
            connection,
            """
            SELECT m.envelope_id, m.processing_state, m.processing_turn_id
            FROM messages m
            WHERE m.processing_state IN ('in_turn', 'processed')
              AND (
                m.processing_turn_id IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM turn_run_messages trm
                    WHERE trm.turn_id = m.processing_turn_id
                      AND trm.envelope_id = m.envelope_id
                )
              )
            ORDER BY m.envelope_id
            """,
        )
        data_version = connection.execute("PRAGMA data_version").fetchone()[0]

    return {
        "db_path": str(db_path),
        "data_version": data_version,
        "counts": counts,
        "messages": messages,
        "turns": turns,
        "memberships": memberships,
        "invariants": {
            "invalid_active_memberships": invalid_active_memberships,
        },
    }


class ScriptedAdapter:
    """Provider boundary that exposes explicit initial/continuation admission."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._initial: tuple[Any, str] | None = None
        self._continuations: dict[str, Any] = {}

    async def get_context_snapshot(self) -> ContextSnapshot:
        return ContextSnapshot(
            used_tokens=0,
            context_window=200_000,
            source="message_runtime_lab",
            measured_at=datetime.now(timezone.utc),
        )

    def get_context_capabilities(self) -> ContextCapabilities:
        return ContextCapabilities(native_measurement=True)

    async def compact_context(self) -> CompactionResult:
        return CompactionResult(completed=False, diagnostic="not used by lab")

    async def rollover_context(self) -> RolloverResult:
        return RolloverResult(completed=False, diagnostic="not used by lab")

    def get_provider_session_id(self) -> str:
        return self.session_id

    def register_admission_callback(
        self, callback: Any, planning_cycle_key: str = ""
    ) -> None:
        self._initial = (callback, planning_cycle_key) if callback else None

    def register_continuation_callback(
        self,
        callback: Any,
        planning_cycle_key: str = "",
        *,
        channel_id: str = "",
        **_correlation: Any,
    ) -> None:
        del channel_id
        if callback is None:
            self._continuations.pop(planning_cycle_key, None)
        else:
            self._continuations[planning_cycle_key] = callback

    async def admit_initial(self) -> None:
        if self._initial is None:
            raise RuntimeError("initial provider admission callback is missing")
        callback, key = self._initial
        self._initial = None
        await callback(
            ProviderAdmissionEvent(
                planning_cycle_key=key,
                provider_session_id=self.session_id,
                provider_turn_id=f"provider-{uuid.uuid4().hex}",
                admitted_at=datetime.now(timezone.utc),
            )
        )

    async def admit_continuation(self, key: str) -> None:
        callback = self._continuations.pop(key)
        await callback(
            ProviderAdmissionEvent(
                planning_cycle_key=key,
                provider_session_id=self.session_id,
                provider_turn_id=f"provider-{uuid.uuid4().hex}",
                admitted_at=datetime.now(timezone.utc),
            )
        )

    async def admit_only_continuation(self) -> None:
        if len(self._continuations) != 1:
            raise RuntimeError(
                "expected exactly one staged provider continuation, "
                f"found {len(self._continuations)}"
            )
        await self.admit_continuation(next(iter(self._continuations)))


async def store_receipt(
    store: MessageStore,
    *,
    envelope_id: str,
    seq: int,
    content: str,
    sender: str,
    kind: str = "channel",
    space_id: str = "space-lab",
    channel_id: str = "channel-lab",
    thread_root_id: str | None = None,
    recipient_slug: str | None = None,
) -> None:
    await store.store_receipt(
        {
            "envelope_id": envelope_id,
            "envelope_kind": kind,
            "sender_slug": sender,
            "recipient_slug": recipient_slug,
            "channel_id": channel_id if kind != "dm" else None,
            "space_id": space_id if kind != "dm" else None,
            "content": content,
            "content_type": "text/plain",
            "sent_at": seq,
            "thread_root_id": thread_root_id,
            "is_encrypted": True,
        },
        server_seq=seq,
        disposition=ReceiptDisposition.ELIGIBLE,
        reason="message runtime lab",
    )


def _new_run_dir(output_dir: Path | None, scenario: str) -> Path:
    if output_dir is not None:
        result = output_dir.resolve()
        if result.exists() and any(result.iterdir()):
            raise RuntimeError(f"output directory is not empty: {result}")
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        result = (
            Path("/tmp")
            / "puffo-message-runtime-lab"
            / f"{scenario}-{stamp}-{suffix}"
        )
    result.mkdir(parents=True, exist_ok=True)
    return result


def _input_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_batch_runtime(
    *,
    store: MessageStore,
    adapter: ScriptedAdapter,
    workspace: Path,
    db_path: Path,
    events: EventLog,
    planned_turns: list[dict[str, Any]],
) -> GlobalInboxRuntime:
    runtime: GlobalInboxRuntime

    async def run_turn(planned: Any) -> None:
        await adapter.admit_initial()
        page = await runtime.read_inbox(limit=50)
        rows = await store.get_in_turn_messages(planned.turn_id, adapter.session_id)
        targets_seen: list[tuple[str, ...]] = []
        for row in rows:
            if row.envelope_kind == "dm":
                target = ("dm", row.sender_slug)
            elif row.thread_root_id:
                target = (
                    "thread",
                    row.space_id,
                    row.channel_id,
                    row.thread_root_id,
                )
            else:
                target = ("channel", row.space_id, row.channel_id)
            if target not in targets_seen:
                targets_seen.append(target)
        entry = {
            "turn_id": planned.turn_id,
            "message_ids": [row.envelope_id for row in rows],
            "targets": [list(target) for target in targets_seen],
            "message_count": len(rows),
            "more_pending": page["has_more"],
            "byte_length": len(planned.provider_input.encode("utf-8")),
            "sha256": _input_digest(planned.provider_input),
        }
        planned_turns.append(entry)
        events.emit("provider.input", agent_id="agent-batch", **entry)
        events.emit(
            "provider.admitted",
            agent_id="agent-batch",
            turn_id=planned.turn_id,
            snapshot=snapshot_database(db_path),
        )

    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=run_turn,
        workspace=workspace,
        formatter=lambda item: str(item.content),
        estimator=lambda _text: 1,
    )
    return runtime


async def _inject_batch_messages(
    store: MessageStore,
    *,
    message_count: int,
    targets: str,
) -> None:
    for index in range(message_count):
        kind = "channel"
        channel_id = "channel-lab"
        thread_root_id = None
        recipient_slug = None
        if targets == "mixed":
            selector = index % 3
            if selector == 1:
                thread_root_id = "thread-root"
            elif selector == 2:
                kind = "dm"
                channel_id = ""
                recipient_slug = "agent-batch"
        await store_receipt(
            store,
            envelope_id=f"message-{index + 1:03d}",
            seq=index + 1,
            content=f"payload-{index + 1:03d}",
            sender=f"sender-{index % 4}",
            kind=kind,
            channel_id=channel_id,
            thread_root_id=thread_root_id,
            recipient_slug=recipient_slug,
        )


async def _drain_batch_runtime(
    store: MessageStore,
    runtime: GlobalInboxRuntime,
    events: EventLog,
    db_path: Path,
) -> None:
    while await store.get_pending(limit=1):
        if not await runtime.process_once():
            raise RuntimeError(f"runtime stopped before draining: {runtime.health}")
        events.emit(
            "turn.completed",
            agent_id="agent-batch",
            snapshot=snapshot_database(db_path),
        )


async def run_batch_scenario(
    *,
    message_count: int = 51,
    targets: str = "mixed",
    output_dir: Path | None = None,
) -> dict[str, Any]:
    if message_count < 1:
        raise ValueError("message_count must be positive")
    run_dir = _new_run_dir(output_dir, "batch")
    run_id = f"run_{uuid.uuid4().hex}"
    events = EventLog(run_dir / "events.jsonl", scenario="batch", run_id=run_id)
    workspace = run_dir / "agent-batch"
    db_path = workspace / "messages.db"
    store = MessageStore(db_path)
    await store.open()
    adapter = ScriptedAdapter("provider-batch")
    planned_turns: list[dict[str, Any]] = []
    runtime = _build_batch_runtime(
        store=store,
        adapter=adapter,
        workspace=workspace,
        db_path=db_path,
        events=events,
        planned_turns=planned_turns,
    )
    events.emit("run.started", agent_id="agent-batch", message_count=message_count)
    await _inject_batch_messages(
        store, message_count=message_count, targets=targets,
    )
    events.emit(
        "messages.injected",
        agent_id="agent-batch",
        snapshot=snapshot_database(db_path),
    )
    await _drain_batch_runtime(store, runtime, events, db_path)

    final_snapshot = snapshot_database(db_path)
    expected_sizes = [
        min(50, message_count - offset)
        for offset in range(0, message_count, 50)
    ]
    observed_sizes = [turn["message_count"] for turn in planned_turns]
    assertions = {
        "turn_sizes": observed_sizes == expected_sizes,
        "target_kinds": {
            target[0]
            for turn in planned_turns
            for target in turn["targets"]
        }
        == (
            {"channel", "thread", "dm"}
            if targets == "mixed" and message_count >= 3
            else {
                "channel",
                *(
                    ("thread",)
                    if targets == "mixed" and message_count >= 2
                    else ()
                ),
            }
        ),
        "processed_all": final_snapshot["counts"].get("processed", 0)
        == message_count,
        "pending_empty": final_snapshot["counts"].get("pending", 0) == 0,
        "in_turn_empty": final_snapshot["counts"].get("in_turn", 0) == 0,
        "membership_invariants": not final_snapshot["invariants"][
            "invalid_active_memberships"
        ],
    }
    report = {
        "scenario": "batch",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "db_path": str(db_path),
        "message_count": message_count,
        "targets": targets,
        "expected_turn_sizes": expected_sizes,
        "observed_turn_sizes": observed_sizes,
        "turns": planned_turns,
        "final_snapshot": final_snapshot,
        "assertions": assertions,
        "ok": all(assertions.values()),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    events.emit("run.completed", agent_id="agent-batch", report=report)
    await store.close()
    if not report["ok"]:
        raise AssertionError(json.dumps(assertions, sort_keys=True))
    return report


@dataclass(frozen=True)
class ChannelMessage:
    seq: int
    envelope_id: str
    sender: str
    text: str


class AtomicChannel:
    """Deterministic stand-in for the server's locked conversation head."""

    def __init__(self, events: EventLog):
        self._lock = asyncio.Lock()
        self.events = events
        self.messages = [
            ChannelMessage(1, "seed-count", "operator", "Count from 1")
        ]
        self.attempts = 0
        self.held = 0

    async def send(
        self,
        *,
        agent_id: str,
        seen_seq: int,
        text: str,
        envelope_id: str,
        mode: str = "require_current",
    ) -> dict[str, Any]:
        async with self._lock:
            self.attempts += 1
            head = self.messages[-1]
            self.events.emit(
                "send.attempted",
                agent_id=agent_id,
                envelope_id=envelope_id,
                seen_seq=seen_seq,
                latest_seq=head.seq,
                text=text,
                mode=mode,
            )
            if seen_seq > head.seq:
                raise RuntimeError("agent claimed a future channel boundary")
            if mode == "require_current" and seen_seq < head.seq:
                self.held += 1
                result = {
                    "state": "held",
                    "envelope_id": envelope_id,
                    "seen_seq": seen_seq,
                    "latest_seq": head.seq,
                    "latest_envelope_id": head.envelope_id,
                    "latest_message": asdict(head),
                }
                self.events.emit("send.result", agent_id=agent_id, **result)
                return result
            committed = ChannelMessage(
                seq=head.seq + 1,
                envelope_id=envelope_id,
                sender=agent_id,
                text=text,
            )
            self.messages.append(committed)
            result = {
                "state": "sent",
                "envelope_id": envelope_id,
                "seen_seq": seen_seq,
                "seq": committed.seq,
                "message": asdict(committed),
            }
            self.events.emit("send.result", agent_id=agent_id, **result)
            return result


@dataclass
class CountingAgent:
    agent_id: str
    store: MessageStore
    adapter: ScriptedAdapter
    runtime: GlobalInboxRuntime
    db_path: Path
    commands: asyncio.Queue[str]
    results: asyncio.Queue[dict[str, Any]]
    ready: asyncio.Event
    task: asyncio.Task[bool] | None = None


def _numeric_values(rows: Iterable[Any]) -> list[int]:
    values: list[int] = []
    for row in rows:
        try:
            values.append(int(str(row.content)))
        except (TypeError, ValueError):
            continue
    return values


@dataclass
class CountingTurnRunner:
    events: EventLog
    channel: AtomicChannel
    agent_id: str
    store: MessageStore
    adapter: ScriptedAdapter
    commands: asyncio.Queue[str]
    results: asyncio.Queue[dict[str, Any]]
    ready: asyncio.Event
    holder: dict[str, CountingAgent]

    async def __call__(self, planned: Any) -> None:
        self.events.emit(
            "provider.input",
            agent_id=self.agent_id,
            turn_id=planned.turn_id,
            message_ids=list(planned.message_ids),
            targets=[list(target) for target in planned.targets],
            sha256=_input_digest(planned.provider_input),
        )
        await self.adapter.admit_initial()
        await self.holder["agent"].runtime.read_inbox(limit=50)
        self.ready.set()
        while True:
            command = await self.commands.get()
            if command != "attempt":
                raise RuntimeError(f"unknown counting command: {command}")
            rows = await self.store.get_in_turn_messages(
                planned.turn_id, self.adapter.session_id
            )
            seen_seq = max(
                (
                    row.server_seq
                    for row in rows
                    if row.server_seq is not None
                    and row.envelope_kind == "channel"
                ),
                default=0,
            )
            next_number = max(_numeric_values(rows), default=0) + 1
            result = await self.channel.send(
                agent_id=self.agent_id,
                seen_seq=seen_seq,
                text=str(next_number),
                envelope_id=f"{self.agent_id}-{uuid.uuid4().hex}",
            )
            if result["state"] == "sent":
                await self.results.put(result)
                return

            latest = result["latest_message"]
            if await self.store.get_message_by_envelope(
                latest["envelope_id"]
            ) is None:
                await store_receipt(
                    self.store,
                    envelope_id=latest["envelope_id"],
                    seq=latest["seq"],
                    content=latest["text"],
                    sender=latest["sender"],
                )
            recovered = await self.holder[
                "agent"
            ].runtime.held_recovery_source.query_held_messages(
                "space-lab",
                "channel-lab",
                latest["seq"],
                latest["envelope_id"],
                self.adapter.session_id,
            )
            if not recovered:
                raise RuntimeError("held continuation produced no context")
            # Local held synchronization is not a content read. The in-process
            # lab must perform the content-bearing read that admits the row.
            await self.holder["agent"].runtime.read_inbox(limit=50)
            staged = self.holder["agent"].runtime.held.get(
                ("space-lab", "channel-lab"),
            )
            if staged is None or not staged.synchronized:
                raise RuntimeError("held continuation did not synchronize")
            await self.results.put(result)


async def _create_counting_agent(
    *,
    index: int,
    run_dir: Path,
    events: EventLog,
    channel: AtomicChannel,
) -> CountingAgent:
    agent_id = f"agent-{index + 1}"
    workspace = run_dir / agent_id
    db_path = workspace / "messages.db"
    store = MessageStore(db_path)
    await store.open()
    await store_receipt(
        store,
        envelope_id="seed-count",
        seq=1,
        content="Count from 1",
        sender="operator",
    )
    adapter = ScriptedAdapter(f"provider-{agent_id}")
    # This lab adapter is genuinely in-process: the scripted provider and tool
    # handler share one event loop. Keep its immediate boundary explicit.
    adapter.tool_result_admission_boundary = "tool_return"
    commands: asyncio.Queue[str] = asyncio.Queue()
    results: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    ready = asyncio.Event()
    holder: dict[str, CountingAgent] = {}
    runner = CountingTurnRunner(
        events, channel, agent_id, store, adapter, commands, results, ready, holder
    )
    runtime = GlobalInboxRuntime(
        store=store,
        adapter=adapter,
        run_turn=runner,
        workspace=workspace,
        formatter=lambda item: str(item.content),
        estimator=lambda _text: 1,
    )
    agent = CountingAgent(
        agent_id=agent_id,
        store=store,
        adapter=adapter,
        runtime=runtime,
        db_path=db_path,
        commands=commands,
        results=results,
        ready=ready,
    )
    holder["agent"] = agent
    return agent


async def _abort_counting_agents(
    agents: Sequence[CountingAgent], label: str, cause: BaseException
) -> None:
    diagnostics = {}
    for agent in agents:
        task = agent.task
        if task is None:
            diagnostics[agent.agent_id] = "not_started"
        elif task.cancelled():
            diagnostics[agent.agent_id] = "cancelled"
        elif task.done():
            diagnostics[agent.agent_id] = repr(task.exception())
        else:
            diagnostics[agent.agent_id] = "running"
            task.cancel()
    await asyncio.gather(
        *(agent.task for agent in agents if agent.task is not None),
        return_exceptions=True,
    )
    for agent in agents:
        await agent.store.close()
    raise RuntimeError(f"{label} failed: {diagnostics}") from cause


async def _wait_counting_step(
    agents: Sequence[CountingAgent], awaitable: Any, label: str
) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=5.0)
    except Exception as exc:
        await _abort_counting_agents(agents, label, exc)


async def _run_counting_rounds(
    agents: Sequence[CountingAgent], events: EventLog
) -> list[dict[str, Any]]:
    active = list(agents)
    rounds: list[dict[str, Any]] = []
    while active:
        for agent in active:
            await agent.commands.put("attempt")
        results = await _wait_counting_step(
            agents,
            asyncio.gather(*(agent.results.get() for agent in active)),
            "counting round",
        )
        sent = [
            (agent, result)
            for agent, result in zip(active, results, strict=True)
            if result["state"] == "sent"
        ]
        held = [
            (agent, result)
            for agent, result in zip(active, results, strict=True)
            if result["state"] == "held"
        ]
        if len(sent) != 1 or len(held) != len(active) - 1:
            raise AssertionError(
                f"round expected one sent and {len(active) - 1} held: {results}"
            )
        winner, result = sent[0]
        rounds.append(
            {
                "active_agents": [agent.agent_id for agent in active],
                "winner": winner.agent_id,
                "number": result["message"]["text"],
                "sent": 1,
                "held": len(held),
            }
        )
        assert winner.task is not None
        await _wait_counting_step(
            agents, winner.task, f"{winner.agent_id} completion"
        )
        events.emit(
            "turn.completed",
            agent_id=winner.agent_id,
            snapshot=snapshot_database(winner.db_path),
        )
        active = [agent for agent, _result in held]
        for agent in active:
            events.emit(
                "sqlite.snapshot",
                agent_id=agent.agent_id,
                snapshot=snapshot_database(agent.db_path),
            )
    return rounds


async def run_count_scenario(
    *,
    agent_count: int = 5,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    if agent_count < 2:
        raise ValueError("agent_count must be at least 2")
    run_dir = _new_run_dir(output_dir, "count")
    run_id = f"run_{uuid.uuid4().hex}"
    events = EventLog(run_dir / "events.jsonl", scenario="count", run_id=run_id)
    channel = AtomicChannel(events)
    agents: list[CountingAgent] = []

    for index in range(agent_count):
        agents.append(await _create_counting_agent(
            index=index,
            run_dir=run_dir,
            events=events,
            channel=channel,
        ))

    events.emit("run.started", agent_count=agent_count)
    for agent in agents:
        agent.task = asyncio.create_task(agent.runtime.process_once())
    await _wait_counting_step(
        agents,
        asyncio.gather(*(agent.ready.wait() for agent in agents)),
        "provider admission",
    )
    for agent in agents:
        events.emit(
            "provider.admitted",
            agent_id=agent.agent_id,
            snapshot=snapshot_database(agent.db_path),
        )
    rounds = await _run_counting_rounds(agents, events)

    committed_numbers = [int(message.text) for message in channel.messages[1:]]
    final_snapshots = {
        agent.agent_id: snapshot_database(agent.db_path) for agent in agents
    }
    expected_processed = {
        round_result["winner"]: int(round_result["number"])
        for round_result in rounds
    }
    assertions = {
        "numbers_are_contiguous": committed_numbers
        == list(range(1, agent_count + 1)),
        "one_winner_per_agent": len(
            {message.sender for message in channel.messages[1:]}
        )
        == agent_count,
        "triangular_attempt_count": channel.attempts
        == agent_count * (agent_count + 1) // 2,
        "triangular_held_count": channel.held
        == agent_count * (agent_count - 1) // 2,
        "all_processed": all(
            snapshot["counts"].get("processed", 0)
            == expected_processed[agent_id]
            and snapshot["counts"].get("pending", 0) == 0
            and snapshot["counts"].get("in_turn", 0) == 0
            for agent_id, snapshot in final_snapshots.items()
        ),
        "visible_prefixes_complete": all(
            [row["envelope_id"] for row in snapshot["messages"]]
            == [
                message.envelope_id
                for message in channel.messages[: expected_processed[agent_id]]
            ]
            for agent_id, snapshot in final_snapshots.items()
        ),
        "membership_invariants": all(
            not snapshot["invariants"]["invalid_active_memberships"]
            for snapshot in final_snapshots.values()
        ),
    }
    report = {
        "scenario": "count",
        "transport": "in_process_atomic_channel",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "agent_count": agent_count,
        "attempt_count": channel.attempts,
        "held_count": channel.held,
        "committed_messages": [asdict(message) for message in channel.messages],
        "rounds": rounds,
        "final_snapshots": final_snapshots,
        "assertions": assertions,
        "ok": all(assertions.values()),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    events.emit("run.completed", report=report)
    for agent in agents:
        await agent.store.close()
    if not report["ok"]:
        raise AssertionError(json.dumps(assertions, sort_keys=True))
    return report


async def monitor_database(
    *,
    db_path: Path,
    watch: bool,
    interval: float,
    timeout: float,
    expect_processed: int | None,
    expect_turn_sizes: Sequence[int],
    jsonl_path: Path | None,
) -> int:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    if jsonl_path is not None:
        _validate_observer_output_path(db_path, jsonl_path)
    started = time.monotonic()
    previous: str | None = None
    sink = jsonl_path.open("a", encoding="utf-8") if jsonl_path else None
    has_expectations = expect_processed is not None or bool(expect_turn_sizes)
    try:
        while True:
            snapshot = snapshot_database(db_path)
            body = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
            if body != previous:
                print(json.dumps(snapshot, indent=2, sort_keys=True), flush=True)
                if sink:
                    sink.write(body + "\n")
                    sink.flush()
                previous = body
            actual_turn_sizes = [turn["message_count"] for turn in snapshot["turns"]]
            processed_ok = (
                expect_processed is None
                or snapshot["counts"].get("processed", 0) >= expect_processed
            )
            turn_sizes_ok = (
                not expect_turn_sizes
                or actual_turn_sizes == list(expect_turn_sizes)
            )
            invariants_ok = not snapshot["invariants"][
                "invalid_active_memberships"
            ]
            if has_expectations and processed_ok and turn_sizes_ok and invariants_ok:
                return 0
            if not watch:
                return 0 if invariants_ok and not has_expectations else 1
            if time.monotonic() - started >= timeout:
                return 0 if invariants_ok and not has_expectations else 1
            await asyncio.sleep(interval)
    finally:
        if sink:
            sink.close()


def _output_path(value: str) -> Path | None:
    return Path(value).expanduser() if value else None


def _validate_observer_output_path(db_path: Path, output_path: Path) -> None:
    db_path = db_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    protected = (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    )
    if output_path in protected:
        raise ValueError("observer JSONL output must not overwrite SQLite files")
    if output_path.exists():
        for candidate in protected:
            if candidate.exists() and output_path.samefile(candidate):
                raise ValueError(
                    "observer JSONL output must not alias SQLite files"
                )


def _turn_sizes(value: str) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(int(part) for part in value.split(","))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    batch = subparsers.add_parser("batch", help="run a durable 50/51 batch scenario")
    batch.add_argument("--messages", type=int, default=51)
    batch.add_argument("--targets", choices=("same-channel", "mixed"), default="mixed")
    batch.add_argument("--output-dir", default="")

    count = subparsers.add_parser("count", help="run a multi-agent counting race")
    count.add_argument("--agents", type=int, default=5)
    count.add_argument("--output-dir", default="")

    monitor = subparsers.add_parser("monitor", help="observe an existing messages.db")
    monitor.add_argument("--db", required=True)
    monitor.add_argument("--watch", action="store_true")
    monitor.add_argument("--interval", type=float, default=0.1)
    monitor.add_argument("--timeout", type=float, default=30.0)
    monitor.add_argument("--expect-processed", type=int)
    monitor.add_argument("--expect-turn-sizes", type=_turn_sizes, default=())
    monitor.add_argument("--jsonl", default="")
    return parser


async def async_main(args: argparse.Namespace) -> int:
    if args.command == "batch":
        report = await run_batch_scenario(
            message_count=args.messages,
            targets=args.targets,
            output_dir=_output_path(args.output_dir),
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.command == "count":
        report = await run_count_scenario(
            agent_count=args.agents,
            output_dir=_output_path(args.output_dir),
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    return await monitor_database(
        db_path=Path(args.db).expanduser(),
        watch=args.watch,
        interval=args.interval,
        timeout=args.timeout,
        expect_processed=args.expect_processed,
        expect_turn_sizes=args.expect_turn_sizes,
        jsonl_path=_output_path(args.jsonl),
    )


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
