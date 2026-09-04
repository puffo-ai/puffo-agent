from __future__ import annotations

import json
import asyncio
from types import SimpleNamespace

import pytest

from puffo_agent.agent.harness.runtime import runtime_commands
from puffo_agent.agent.harness.driver import (
    CancelReceipt,
    HarnessEvent,
    PermissionDecision,
    PermissionReceipt,
    PermissionRef,
    RuntimeOpened,
    RuntimeRef,
    RuntimeSpec,
    SessionRef,
    TurnRef,
)
from puffo_agent.agent.harness.drivers.codex import CODEX_CAPABILITIES
from puffo_agent.agent.harness.runtime.runtime_manager import (
    RuntimeManager,
    register_runtime_manager,
    unregister_runtime_manager,
)
from puffo_agent.agent.puffo_core_client import PuffoCoreMessageClient
from puffo_agent.agent.runtime_event_outbox import (
    RuntimeEventOutbox,
    RuntimeEventProjectingSink,
    RuntimeEventUploader,
)
from puffo_agent.agent.runtime_events import RuntimeEventProjector


class ScriptedDriver:
    def __init__(self):
        self.cancel_calls: list[TurnRef] = []
        self.permission_calls: list[tuple[PermissionRef, PermissionDecision]] = []

    async def cancel_turn(self, turn: TurnRef):
        self.cancel_calls.append(turn)
        return CancelReceipt(True, turn)

    async def resolve_permission(
        self, permission: PermissionRef, decision: PermissionDecision,
    ):
        self.permission_calls.append((permission, decision))
        return PermissionReceipt(True, permission)


def runtime_fixture():
    driver = ScriptedDriver()
    manager = RuntimeManager(
        driver,  # type: ignore[arg-type]
        RuntimeSpec("/fixture"),
        agent_id="local-agent-id",
        session_ref=SessionRef("session-1"),
    )
    manager.active_turn_ref = TurnRef("turn-1")
    manager._active_driver_turn_ref = TurnRef("driver-turn-1")
    manager._permission_refs.add(PermissionRef("permission-1"))
    register_runtime_manager("local-agent-id", manager)
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "server-agent-slug"
    client.agent_id = "local-agent-id"
    client.operator_slug = "operator-0001"
    return client, manager, driver


def command(op: str, command_id: str, **values):
    return {
        "type": "runtime_command",
        "command": {
            "version": 1,
            "command_id": command_id,
            "operator_slug": "operator-0001",
            "agent_id": "server-agent-slug",
            "session_ref": "session-1",
            "op": f"runtime.{op}",
            **values,
        },
    }


@pytest.mark.asyncio
async def test_cloud_cancel_reaches_real_manager_once_across_duplicate_frames():
    client, manager, driver = runtime_fixture()
    try:
        frame = command("cancel_turn", "cloud-cancel", turn_ref="turn-1")
        first = await client._dispatch_bridge_frame(frame)
        reconnected = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
        reconnected.slug = client.slug
        reconnected.agent_id = client.agent_id
        reconnected.operator_slug = client.operator_slug
        duplicate = await reconnected._dispatch_bridge_frame(frame)
        assert first == duplicate == {
            "ok": True, "delivered": True, "completed": False,
        }
        assert driver.cancel_calls == [TurnRef("driver-turn-1")]
    finally:
        unregister_runtime_manager("local-agent-id", manager)


@pytest.mark.asyncio
async def test_cloud_permission_derives_unique_active_turn_and_dedupes():
    client, manager, driver = runtime_fixture()
    try:
        frame = command(
            "resolve_permission",
            "cloud-permission",
            permission_ref="permission-1",
            decision="approved",
        )
        first = await client._dispatch_bridge_frame(frame)
        duplicate = await client._dispatch_bridge_frame(frame)
        assert first == duplicate == {
            "ok": True, "delivered": True, "completed": False,
        }
        assert driver.permission_calls == [
            (PermissionRef("permission-1"), PermissionDecision.APPROVE)
        ]
    finally:
        unregister_runtime_manager("local-agent-id", manager)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"version": 2}, "unsupported_version"),
        ({"agent_id": "foreign"}, "foreign_agent"),
        ({"session_ref": "stale"}, "stale_session_ref"),
        ({"op": "unknown"}, "invalid_command"),
        ({"operator_slug": ""}, "invalid_command"),
        ({"turn_ref": ""}, "invalid_target"),
        ({"session_ref": ""}, "invalid_command"),
        ({"command_id": ""}, "invalid_command"),
    ],
)
async def test_invalid_cloud_cancel_is_typed_and_makes_no_driver_call(
    patch, code,
):
    client, manager, driver = runtime_fixture()
    try:
        frame = command(
            "cancel_turn", f"bad-{code}-{patch!r}", turn_ref="turn-1"
        )
        frame["command"].update(patch)
        result = await client._dispatch_bridge_frame(frame)
        assert result["error_code"] == code
        assert driver.cancel_calls == []
        assert driver.permission_calls == []
    finally:
        unregister_runtime_manager("local-agent-id", manager)


@pytest.mark.asyncio
async def test_permission_without_active_turn_is_closed_without_driver_call():
    client, manager, driver = runtime_fixture()
    manager.active_turn_ref = None
    try:
        result = await client._dispatch_bridge_frame(command(
            "resolve_permission",
            "no-active-turn",
            permission_ref="permission-1",
            decision="denied",
        ))
        assert result["error_code"] == "runtime_unavailable"
        assert driver.permission_calls == []
    finally:
        unregister_runtime_manager("local-agent-id", manager)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (
            command(
                "resolve_permission",
                "invalid-decision",
                permission_ref="permission-1",
                decision="maybe",
            ),
            "invalid_decision",
        ),
        (
            command(
                "resolve_permission",
                "stale-permission",
                permission_ref="permission-stale",
                decision="approved",
            ),
            "stale_runtime_reference",
        ),
        (
            command("cancel_turn", "stale-turn", turn_ref="turn-stale"),
            "stale_runtime_reference",
        ),
        (
            {
                "type": "runtime_command",
                "command": {
                    "version": 1,
                    "command_id": "unknown-op",
                    "operator_slug": "operator-0001",
                    "agent_id": "server-agent-slug",
                    "session_ref": "session-1",
                    "op": "unknown",
                },
            },
            "unsupported_operation",
        ),
    ],
)
async def test_stale_or_kind_specific_cloud_target_is_closed(frame, code):
    client, manager, driver = runtime_fixture()
    try:
        result = await client._dispatch_bridge_frame(frame)
        assert result["error_code"] == code
        assert driver.cancel_calls == []
        assert driver.permission_calls == []
    finally:
        unregister_runtime_manager("local-agent-id", manager)


@pytest.mark.asyncio
async def test_transient_runtime_unavailable_is_not_cached():
    runtime_commands._RESULTS.clear()
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "server-agent-slug"
    client.agent_id = "local-agent-id"
    client.operator_slug = "operator-0001"
    frame = command("cancel_turn", "transient-manager", turn_ref="turn-1")
    unavailable = await client._dispatch_bridge_frame(frame)
    assert unavailable["error_code"] == "runtime_unavailable"

    _client, manager, driver = runtime_fixture()
    try:
        delivered = await client._dispatch_bridge_frame(frame)
        duplicate = await client._dispatch_bridge_frame(frame)
        assert delivered == duplicate == {
            "ok": True, "delivered": True, "completed": False,
        }
        assert driver.cancel_calls == [TurnRef("driver-turn-1")]
    finally:
        unregister_runtime_manager("local-agent-id", manager)


@pytest.mark.asyncio
async def test_missing_active_turn_rejection_is_not_cached():
    runtime_commands._RESULTS.clear()
    client, manager, driver = runtime_fixture()
    manager.active_turn_ref = None
    frame = command(
        "resolve_permission",
        "transient-active-turn",
        permission_ref="permission-1",
        decision="approved",
    )
    try:
        unavailable = await client._dispatch_bridge_frame(frame)
        assert unavailable["error_code"] == "runtime_unavailable"
        manager.active_turn_ref = TurnRef("turn-1")
        delivered = await client._dispatch_bridge_frame(frame)
        duplicate = await client._dispatch_bridge_frame(frame)
        assert delivered == duplicate == {
            "ok": True, "delivered": True, "completed": False,
        }
        assert driver.permission_calls == [
            (PermissionRef("permission-1"), PermissionDecision.APPROVE)
        ]
    finally:
        unregister_runtime_manager("local-agent-id", manager)


def test_cloud_command_fixture_matches_production_wire_shape():
    cancel = command("cancel_turn", "shape-cancel", turn_ref="turn-1")[
        "command"
    ]
    permission = command(
        "resolve_permission",
        "shape-permission",
        permission_ref="permission-1",
        decision="denied",
    )["command"]
    assert set(cancel) == {
        "version",
        "command_id",
        "op",
        "operator_slug",
        "agent_id",
        "session_ref",
        "turn_ref",
    }
    assert cancel["op"] == "runtime.cancel_turn"
    assert set(permission) == {
        "version",
        "command_id",
        "op",
        "operator_slug",
        "agent_id",
        "session_ref",
        "permission_ref",
        "decision",
    }
    assert permission["op"] == "runtime.resolve_permission"


@pytest.mark.asyncio
async def test_runtime_command_cache_has_lru_capacity(monkeypatch):
    runtime_commands._RESULTS.clear()
    monkeypatch.setattr(runtime_commands, "RESULT_CACHE_CAPACITY", 2)
    client, manager, driver = runtime_fixture()
    try:
        for command_id in ("cache-1", "cache-2", "cache-3"):
            result = await client._dispatch_bridge_frame(command(
                "cancel_turn", command_id, turn_ref="turn-1",
            ))
            assert result["delivered"] is True
        assert list(runtime_commands._RESULTS) == [
            ("local-agent-id", "cache-2"),
            ("local-agent-id", "cache-3"),
        ]
        await client._dispatch_bridge_frame(command(
            "cancel_turn", "cache-1", turn_ref="turn-1",
        ))
        assert driver.cancel_calls == [TurnRef("driver-turn-1")] * 4
        assert list(runtime_commands._RESULTS) == [
            ("local-agent-id", "cache-3"),
            ("local-agent-id", "cache-1"),
        ]
    finally:
        unregister_runtime_manager("local-agent-id", manager)


@pytest.mark.asyncio
async def test_runtime_command_logs_exclude_raw_sensitive_sentinels(caplog):
    client, manager, _driver = runtime_fixture()
    try:
        frame = command("cancel_turn", "safe-id", turn_ref="turn-1")
        frame["command"]["plaintext"] = "PLAINTEXT_SENTINEL"
        frame["command"]["credential"] = "CREDENTIAL_SENTINEL"
        frame["command"]["raw_envelope"] = "RAW_ENVELOPE_SENTINEL"
        frame["command"]["raw_command"] = "RAW_COMMAND_SENTINEL"
        frame["raw_frame"] = "RAW_FRAME_SENTINEL"
        result = await client._dispatch_bridge_frame(frame)
        assert result["error_code"] == "invalid_command"
        rendered = "\n".join(record.getMessage() for record in caplog.records)
        for sentinel in (
            "PLAINTEXT_SENTINEL",
            "CREDENTIAL_SENTINEL",
            "RAW_FRAME_SENTINEL",
            "RAW_ENVELOPE_SENTINEL",
            "RAW_COMMAND_SENTINEL",
        ):
            assert sentinel not in rendered
    finally:
        unregister_runtime_manager("local-agent-id", manager)


@pytest.mark.asyncio
async def test_missing_command_object_returns_typed_closed_result():
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "server-agent-slug"
    client.agent_id = "local-agent-id"
    client.operator_slug = "operator-0001"
    result = await client._dispatch_bridge_frame({
        "type": "runtime_command", "command": None,
    })
    assert result == {
        "ok": False,
        "error": "invalid_command",
        "error_code": "invalid_command",
    }


class _OutcomeDriver(ScriptedDriver):
    def __init__(self, event_type, data):
        super().__init__()
        self.queue = asyncio.Queue()
        self.event_type = event_type
        self.data = data

    async def open(self, _spec, _resume=None):
        return RuntimeOpened(
            RuntimeRef("runtime-1"), SessionRef("native-session"),
            "native-session", False, CODEX_CAPABILITIES, SimpleNamespace(),
        )

    async def events(self):
        while True:
            event = await self.queue.get()
            if event is None:
                return
            yield event

    async def close(self):
        await self.queue.put(None)

    async def _emit_outcome(self):
        for event_type, data in (("turn.started", {}), (self.event_type, self.data)):
            await self.queue.put(HarnessEvent.normalized(
                type=event_type, driver="scripted",
                session_ref=SessionRef("native-session"),
                turn_ref=TurnRef("driver-turn-1"), data=data,
            ))

    async def cancel_turn(self, turn):
        receipt = await super().cancel_turn(turn)
        await self._emit_outcome()
        return receipt

    async def resolve_permission(self, permission, decision):
        receipt = await super().resolve_permission(permission, decision)
        await self._emit_outcome()
        return receipt


async def _outcome_runtime(tmp_path, event_type, data, public_type, outcome):
    outbox = RuntimeEventOutbox(tmp_path / f"{public_type}.db")
    event_ids = iter(("evt-started", f"evt-{public_type}-{outcome}"))
    sink = RuntimeEventProjectingSink(outbox, RuntimeEventProjector(
        agent_id="local-agent-id", session_ref="session-1",
        id_factory=lambda: next(event_ids),
    ))
    projected = asyncio.Event()

    async def projecting_sink(event):
        await sink(event)
        if event.type == event_type:
            projected.set()

    driver = _OutcomeDriver(event_type, data)
    manager = RuntimeManager(
        driver,  # type: ignore[arg-type]
        RuntimeSpec("/fixture"),
        agent_id="local-agent-id",
        session_ref=SessionRef("session-1"),
        event_sink=projecting_sink,
    )
    await manager.open()
    manager.active_turn_ref = TurnRef("turn-1")
    manager._active_driver_turn_ref = TurnRef("driver-turn-1")
    manager._turn_refs[TurnRef("driver-turn-1")] = TurnRef("turn-1")
    manager._permission_refs.add(PermissionRef("permission-1"))
    return outbox, projected, driver, manager


def _assert_projected_outcome(outbox, public_type, outcome):
    rows = outbox.prefix()
    outcomes = [row.event for row in rows if row.event["type"] == public_type]
    assert len(outcomes) == 1
    if public_type == "turn.finished":
        assert outcomes[0]["payload"]["outcome"] == outcome
        if outcome == "failed":
            assert outcomes[0]["payload"]["error"]["code"] == "provider_unavailable"
    else:
        assert outcomes[0]["payload"] == {
            "permission_ref": "permission-1",
            "state": "approved",
            "title": "Permission required",
        }


async def _upload_projected_outcome(outbox, public_type):
    uploaded = []

    async def transport(_path, body):
        uploaded.extend(json.loads(body)["events"])
        return 200, {"accepted": [
            {"event_id": event["event_id"], "cursor": event["event_id"]}
            for event in uploaded
        ]}

    result = await RuntimeEventUploader(outbox, transport).upload_once()
    assert result.state == "uploaded"
    assert len([event for event in uploaded if event["type"] == public_type]) == 1
    assert outbox.prefix() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op", "target", "event_type", "data", "public_type", "outcome"),
    [
        (
            "cancel_turn", {"turn_ref": "turn-1"}, "turn.completed",
            {"outcome": "cancelled"}, "turn.finished", "cancelled",
        ),
        (
            "cancel_turn", {"turn_ref": "turn-1"}, "turn.completed",
            {"outcome": "failed", "error_code": "provider_unavailable", "retryable": True},
            "turn.finished", "failed",
        ),
        (
            "resolve_permission",
            {"permission_ref": "permission-1", "decision": "approved"},
            "turn.permission_updated",
            {"permission_ref": "permission-1", "state": "approved"},
            "permission.updated", "approved",
        ),
    ],
)
async def test_real_manager_driver_outcome_uses_canonical_outbox_upload(
    tmp_path, op, target, event_type, data, public_type, outcome,
):
    runtime_commands._RESULTS.clear()
    outbox, projected, driver, manager = await _outcome_runtime(
        tmp_path, event_type, data, public_type, outcome,
    )
    client = PuffoCoreMessageClient.__new__(PuffoCoreMessageClient)
    client.slug = "server-agent-slug"
    client.agent_id = "local-agent-id"
    client.operator_slug = "operator-0001"
    invalid = command(op, f"invalid-{outcome}", **target)
    invalid["command"]["agent_id"] = "foreign-agent"
    assert (await client._dispatch_bridge_frame(invalid))["error_code"] == (
        "foreign_agent"
    )
    assert outbox.prefix() == []
    assert driver.cancel_calls == []
    assert driver.permission_calls == []

    delivered = await client._dispatch_bridge_frame(command(
        op, f"connected-{outcome}", **target,
    ))
    assert delivered == {"ok": True, "delivered": True, "completed": False}
    await asyncio.wait_for(projected.wait(), timeout=1)
    _assert_projected_outcome(outbox, public_type, outcome)
    await _upload_projected_outcome(outbox, public_type)
    await manager.close()
    outbox.close()


@pytest.mark.asyncio
async def test_cloud_command_operator_slug_is_checked_against_configured_operator():
    """``operator_slug`` is verified, not merely required.

    Nothing server-side emits or attests this frame — ``AgentServerMsg``
    carries no ``RuntimeCommand`` variant — so the claim is worth exactly
    what the client checks. The transport's own precedent is applied: the
    keyless lane already gates operator-control messages on
    ``sender_slug == client.operator_slug``, so a mismatching claim, or any
    claim at all when no operator is configured to grant it, is terminal.
    """
    client, manager, driver = runtime_fixture()
    try:
        foreign = command("cancel_turn", "foreign-operator", turn_ref="turn-1")
        foreign["command"]["operator_slug"] = "attacker-0002"
        assert (await client._dispatch_bridge_frame(foreign))["error_code"] == (
            "foreign_operator"
        )
        assert driver.cancel_calls == []

        # No operator configured ⇒ nobody can resolve permissions here.
        client.operator_slug = ""
        assert (
            await client._dispatch_bridge_frame(
                command("cancel_turn", "unconfigured-operator", turn_ref="turn-1")
            )
        )["error_code"] == "foreign_operator"
        assert driver.cancel_calls == []

        # The configured operator still gets through.
        client.operator_slug = "operator-0001"
        delivered = await client._dispatch_bridge_frame(
            command("cancel_turn", "matching-operator", turn_ref="turn-1")
        )
        assert delivered == {"ok": True, "delivered": True, "completed": False}
        assert driver.cancel_calls == [TurnRef("driver-turn-1")]
    finally:
        unregister_runtime_manager("local-agent-id", manager)
