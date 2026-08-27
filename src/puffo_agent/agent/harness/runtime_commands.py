"""Validated, idempotent delivery of Runtime Manager commands."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Mapping

from .._logging import log_runtime_event
from .driver import (
    PermissionDecision,
    PermissionRef,
    TurnRef,
    UnsupportedCapability,
)
from .runtime_manager import RuntimeStateError, get_runtime_manager

logger = logging.getLogger(__name__)

CommandResult = dict[str, object]
RESULT_CACHE_CAPACITY = 256
_RESULTS: OrderedDict[tuple[str, str], tuple[dict[str, object], CommandResult]] = (
    OrderedDict()
)
_TERMINAL_VALIDATION_ERRORS = frozenset(
    {
        "unsupported_version",
        "invalid_command",
        "foreign_agent",
        "foreign_operator",
        "unsupported_operation",
        "invalid_target",
        "invalid_decision",
    }
)


def _failure(code: str) -> CommandResult:
    return {"ok": False, "error": code, "error_code": code}


def _required_text(command: Mapping[str, object], name: str) -> str:
    value = command.get(name)
    return value if isinstance(value, str) and value.strip() else ""


def _cache_result(
    key: tuple[str, str],
    command: dict[str, object],
    result: CommandResult,
) -> None:
    if not (
        result.get("delivered") is True
        or result.get("error_code") in _TERMINAL_VALIDATION_ERRORS
    ):
        return
    _RESULTS[key] = (command, dict(result))
    _RESULTS.move_to_end(key)
    while len(_RESULTS) > RESULT_CACHE_CAPACITY:
        _RESULTS.popitem(last=False)


async def execute_runtime_command(
    command: Mapping[str, object],
    *,
    expected_agent_id: str,
    manager_agent_id: str,
    require_version: bool,
    permission_turn_from_active: bool,
    cloud_wire: bool = False,
    expected_operator_slug: str = "",
) -> CommandResult:
    """Validate and deliver one command; completion remains event-driven.

    ``expected_operator_slug`` is the cloud-wire lane's configured operator;
    the local control lane authenticates its operator cryptographically at
    pairing time and passes none.
    """
    context = _command_context(
        command,
        expected_agent_id=expected_agent_id,
        manager_agent_id=manager_agent_id,
        require_version=require_version,
        permission_turn_from_active=permission_turn_from_active,
        cloud_wire=cloud_wire,
        expected_operator_slug=expected_operator_slug,
    )
    result = await _execute_command(context)
    _cache_result(context.cache_key, context.command_copy, result)
    _log_command(context, result)
    return result


class _CommandContext:
    def __init__(
        self,
        command: Mapping[str, object],
        *,
        expected_agent_id: str,
        manager_agent_id: str,
        require_version: bool,
        permission_turn_from_active: bool,
        cloud_wire: bool,
        expected_operator_slug: str = "",
    ) -> None:
        self.command = command
        self.expected_agent_id = expected_agent_id
        self.manager_agent_id = manager_agent_id
        self.require_version = require_version
        self.permission_turn_from_active = permission_turn_from_active
        self.cloud_wire = cloud_wire
        self.expected_operator_slug = expected_operator_slug
        self.command_id = _required_text(command, "command_id")
        self.wire_op = _required_text(command, "op")
        self.agent_id = _required_text(command, "agent_id")
        self.session_ref = _required_text(command, "session_ref")
        self.operator_slug = _required_text(command, "operator_slug")
        self.cache_key = (manager_agent_id, self.command_id)
        self.command_copy = dict(command)


def _command_context(
    command: Mapping[str, object], **kwargs: object
) -> _CommandContext:
    return _CommandContext(command, **kwargs)  # type: ignore[arg-type]


def _allowed_fields(context: _CommandContext) -> tuple[set[str], str, str]:
    common = {"command_id", "agent_id", "session_ref", "op"}
    if context.require_version:
        common.add("version")
    if context.cloud_wire:
        common.add("operator_slug")
    cancel = "runtime.cancel_turn" if context.cloud_wire else "cancel_turn"
    permission = (
        "runtime.resolve_permission" if context.cloud_wire else "resolve_permission"
    )
    if context.wire_op == cancel:
        return common | {"turn_ref"}, cancel, permission
    if context.wire_op == permission:
        allowed = common | {"permission_ref", "decision"}
        if not context.permission_turn_from_active:
            allowed.add("turn_ref")
        return allowed, cancel, permission
    return common, cancel, permission


def _validation_failure(context: _CommandContext) -> CommandResult | None:
    allowed, cancel, permission = _allowed_fields(context)
    if context.require_version and context.command.get("version") != 1:
        return _failure("unsupported_version")
    if set(context.command) != allowed or not all(
        (
            context.command_id,
            context.agent_id,
            context.session_ref,
        )
    ):
        return _failure("invalid_command")
    if context.cloud_wire and not context.operator_slug:
        return _failure("invalid_command")
    # ``operator_slug`` is a client-checked authorization fact on this wire,
    # not a server attestation: puffo-server's ``AgentServerMsg`` has no
    # ``RuntimeCommand`` variant, so nothing server-side emits — or
    # verifies — this frame today. The transport's own precedent is to
    # compare a claimed identity against configured identity (the keyless
    # lane gates operator-control messages on
    # ``sender_slug == client.operator_slug``), so a mismatching claim, or
    # any claim at all when no operator is configured to grant it, is
    # rejected rather than trusted.
    if context.cloud_wire and context.operator_slug != context.expected_operator_slug:
        return _failure("foreign_operator")
    if context.agent_id != context.expected_agent_id:
        return _failure("foreign_agent")
    if context.wire_op not in {cancel, permission}:
        return _failure("unsupported_operation")
    return None


async def _execute_command(context: _CommandContext) -> CommandResult:
    invalid = _validation_failure(context)
    if invalid is not None:
        return invalid
    cached = _cached_result(context)
    if cached is not None:
        return cached
    return await _deliver_command(context)


def _cached_result(context: _CommandContext) -> CommandResult | None:
    if not context.command_id or context.cache_key not in _RESULTS:
        return None
    cached_command, cached_result = _RESULTS[context.cache_key]
    _RESULTS.move_to_end(context.cache_key)
    return (
        dict(cached_result)
        if cached_command == context.command_copy
        else _failure("command_id_conflict")
    )


async def _deliver_command(context: _CommandContext) -> CommandResult:
    op = (
        context.wire_op.removeprefix("runtime.")
        if context.cloud_wire
        else context.wire_op
    )
    manager = get_runtime_manager(context.manager_agent_id)
    if manager is None:
        return _failure("runtime_unavailable")
    if context.session_ref != str(manager.session_ref):
        return _failure("stale_session_ref")
    turn_text, active_missing = _turn_text(context, manager, op)
    target = _command_target(context, op)
    decision = _required_text(context.command, "decision")
    if active_missing:
        return _failure("runtime_unavailable")
    if not turn_text or not target:
        return _failure("invalid_target")
    if op == "resolve_permission" and decision not in {
        PermissionDecision.APPROVE.value,
        PermissionDecision.DENY.value,
    }:
        return _failure("invalid_decision")
    try:
        if op == "cancel_turn":
            receipt = await manager.cancel_turn(TurnRef(turn_text))
        else:
            receipt = await manager.resolve_permission(
                TurnRef(turn_text),
                PermissionRef(target),
                PermissionDecision(decision),
            )
    except (RuntimeStateError, ValueError):
        return _failure("stale_runtime_reference")
    return (
        _failure("unsupported_capability")
        if isinstance(receipt, UnsupportedCapability)
        else {"ok": True, "delivered": True, "completed": False}
    )


def _turn_text(context: _CommandContext, manager: object, op: str) -> tuple[str, bool]:
    turn_text = _required_text(context.command, "turn_ref")
    if op != "resolve_permission" or not context.permission_turn_from_active:
        return turn_text, False
    active = manager.active_turn_ref  # type: ignore[attr-defined]
    return (str(active), False) if active is not None else ("", True)


def _command_target(context: _CommandContext, op: str) -> str:
    name = "turn_ref" if op == "cancel_turn" else "permission_ref"
    return _required_text(context.command, name)


def _log_command(context: _CommandContext, result: CommandResult) -> None:
    log_runtime_event(
        logger,
        "runtime.command",
        agent_id=context.manager_agent_id,
        session_ref=context.session_ref,
        turn_ref=_required_text(context.command, "turn_ref"),
        permission_ref=_required_text(context.command, "permission_ref"),
        capability=context.wire_op,
        capability_decision="delivered" if result.get("delivered") else "rejected",
        error_code=str(result.get("error_code") or ""),
    )
