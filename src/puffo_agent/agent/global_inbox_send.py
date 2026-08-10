from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping, TYPE_CHECKING

from ._logging import log_runtime_event
from .global_inbox_types import SendAttemptState

if TYPE_CHECKING:
    from .global_inbox_runtime import GlobalInboxRuntime

# This is part of the observable Inbox event contract.  Keep the original
# logger name while moving the implementation behind the runtime facade.
logger = logging.getLogger("puffo_agent.agent.global_inbox_runtime")
_USE_TRACE_RUNTIME = object()


@dataclass(frozen=True)
class SendTrace:
    destination: str
    send_anyway: bool
    runtime: Any
    active: Any
    route: Any
    transport: str
    mode: str | None
    attempt_phase: str
    attempt: int
    send_attempt_id: str
    started: float


class TrackingSendDelegate:
    """Mark semantic attempts before awaiting the worker-owned coordinator."""

    def __init__(
        self,
        coordinator: Any,
        attempts: SendAttemptState,
        runtime: GlobalInboxRuntime | None = None,
    ):
        self.coordinator = coordinator
        self.attempts = attempts
        self.runtime = runtime

    def _build_trace(self, request: Any, kwargs: Mapping[str, Any]) -> SendTrace:
        if isinstance(request, Mapping):
            destination = str(request.get("destination", request.get("channel", "")))
        elif request is not None:
            destination = str(getattr(request, "destination", ""))
        else:
            destination = str(kwargs.get("destination", kwargs.get("channel", "")))
        send_anyway = bool(
            request.get("send_anyway", False)
            if isinstance(request, Mapping)
            else getattr(request, "send_anyway", False)
            if request is not None
            else kwargs.get("send_anyway", False)
        )
        prior_held = any(
            prior_destination == destination and prior_state == "held"
            for prior_destination, prior_state in zip(
                self.attempts.destinations, self.attempts.states
            )
        )
        is_dm = destination.startswith("@")
        transport = (
            "keyless"
            if bool(
                getattr(
                    getattr(self.coordinator, "http_client", None), "keyless", False
                )
            )
            else "dm"
            if is_dm
            else "channel"
        )
        mode = (
            ("send_anyway" if send_anyway else "require_current") if not is_dm else None
        )
        attempt_phase = "reconsider" if prior_held else "initial"
        self.attempts.record(destination, "attempting")
        attempt = self.attempts.attempts
        runtime = self.runtime
        active = runtime.active if runtime is not None else None
        route = (
            runtime.resolve_active_send_route(destination, request, kwargs)
            if runtime is not None
            else None
        )
        started = time.monotonic()
        send_attempt_id = (
            f"{active.turn_id}:{attempt}"
            if active is not None and active.turn_id
            else f"unbound:{attempt}"
        )
        return SendTrace(
            destination=destination,
            send_anyway=send_anyway,
            runtime=runtime,
            active=active,
            route=route,
            transport=transport,
            mode=mode,
            attempt_phase=attempt_phase,
            attempt=attempt,
            send_attempt_id=send_attempt_id,
            started=started,
        )

    @staticmethod
    def _identity_fields(
        trace: SendTrace, runtime: Any = _USE_TRACE_RUNTIME
    ) -> dict[str, Any]:
        runtime = trace.runtime if runtime is _USE_TRACE_RUNTIME else runtime
        active = trace.active
        route = trace.route
        return {
            "agent_id": runtime.agent_id if runtime is not None else None,
            "turn_id": active.turn_id if active is not None else None,
            "provider_session_id": (
                active.provider_session_id if active is not None else None
            ),
            "provider_turn_id": active.provider_turn_id if active is not None else None,
            "space_id": route.space_id if route is not None else None,
            "channel_id": route.channel_id if route is not None else None,
            "thread_root_id": route.thread_root_id if route is not None else None,
            "dm_peer": route.dm_peer if route is not None else None,
        }

    def _log_attempt(self, trace: SendTrace) -> None:
        log_runtime_event(
            logger,
            "send.attempted",
            level=logging.INFO,
            **self._identity_fields(trace),
            mode=trace.mode,
            attempt_phase=trace.attempt_phase,
            transport=trace.transport,
            attempt=trace.attempt,
            send_attempt_id=trace.send_attempt_id,
            state="attempting",
        )

    def _log_delegate_failure(self, trace: SendTrace) -> None:
        self.attempts.states[-1] = "failed"
        log_runtime_event(
            logger,
            "send.failed",
            level=logging.INFO,
            **self._identity_fields(trace),
            mode=trace.mode,
            attempt_phase=trace.attempt_phase,
            transport=trace.transport,
            attempt=trace.attempt,
            send_attempt_id=trace.send_attempt_id,
            state="failed",
            duration_ms=int((time.monotonic() - trace.started) * 1000),
            error_category="delegate_exception",
        )

    def _log_reconsideration(
        self,
        trace: SendTrace,
        result: Mapping[str, Any],
        audit: Mapping[str, Any],
    ) -> None:
        eligible = bool(audit.get("eligible"))
        if not audit:
            eligible = result.get("error_kind") != "reconsideration_ineligible"
        active = trace.active
        route = trace.route
        log_runtime_event(
            logger,
            "reconsideration.eligible" if eligible else "reconsideration.blocked",
            agent_id=trace.runtime.agent_id if trace.runtime is not None else None,
            turn_id=audit.get("turn_id") or (active.turn_id if active else None),
            provider_session_id=(
                audit.get("provider_session_id")
                or (active.provider_session_id if active else None)
            ),
            provider_turn_id=active.provider_turn_id if active else None,
            space_id=route.space_id if route else None,
            channel_id=route.channel_id if route else None,
            envelope_id=audit.get("latest_envelope_id"),
            latest_seq=audit.get("latest_seq"),
            seen_seq=audit.get("admitted_seq"),
            transport=trace.transport,
            mode=trace.mode,
            send_attempt_id=trace.send_attempt_id,
            outcome="accepted" if eligible else "rejected",
            decision_reason=audit.get("decision_reason")
            or (
                "synchronized_and_admitted"
                if eligible
                else "reconsideration_ineligible"
            ),
        )

    def _stage_held_result(
        self,
        trace: SendTrace,
        result: dict[str, Any],
    ) -> Any:
        source = getattr(self.coordinator, "held_recovery_source", None)
        runtime = getattr(source, "runtime", None)
        by_target = getattr(runtime, "held", None)
        route = trace.route
        # Only this target's own recovery may describe this result; without a
        # resolved route or a staged entry the coordinator's own per-key
        # verdict stands unchanged.
        staging = (
            by_target.get((route.space_id, route.channel_id))
            if isinstance(by_target, dict) and route is not None
            else None
        )
        if staging is not None:
            result["synchronized"] = bool(staging.synchronized)
            if staging.recovered_through_seq is not None:
                result["recovered_through_seq"] = staging.recovered_through_seq
            result["recovery_more_pending"] = bool(staging.more_pending)
            if staging.diagnostic:
                result["synchronization_diagnostic"] = staging.diagnostic
            if staging.correlation_key:
                result["continuation_correlation_key"] = staging.correlation_key
        active = trace.active
        log_runtime_event(
            logger,
            "held.synchronized",
            agent_id=runtime.agent_id if runtime is not None else None,
            turn_id=active.turn_id if active is not None else None,
            provider_session_id=active.provider_session_id if active else None,
            send_attempt_id=trace.send_attempt_id,
            latest_seq=result.get("latest_seq"),
            outcome="available" if result.get("synchronized") else "unavailable",
        )
        return runtime

    def _log_result(
        self,
        trace: SendTrace,
        result: Mapping[str, Any],
        state: str,
        runtime: Any,
    ) -> None:
        event = {
            "held": "send.held",
            "sent": "send.committed",
        }.get(state, "send.failed")
        log_runtime_event(
            logger,
            event,
            level=logging.INFO,
            **self._identity_fields(trace, runtime),
            correlation_key=result.get("continuation_correlation_key"),
            envelope_id=result.get("envelope_id"),
            context_baseline_seq=result.get("context_baseline_seq"),
            seen_seq=result.get("seen_seq"),
            latest_seq=result.get("latest_seq") if state == "held" else None,
            latest_seq_before_send=(
                result.get("latest_seq_before_send") if state == "sent" else None
            ),
            seq=result.get("seq"),
            mode=trace.mode,
            attempt_phase=trace.attempt_phase,
            transport=trace.transport,
            attempt=trace.attempt,
            send_attempt_id=trace.send_attempt_id,
            state=state,
            duration_ms=int((time.monotonic() - trace.started) * 1000),
            error_category=result.get("error_kind"),
        )

    async def send(self, request: Any = None, **kwargs: Any) -> dict[str, Any]:
        trace = self._build_trace(request, kwargs)
        self._log_attempt(trace)
        try:
            result = await self.coordinator.send(request, **kwargs)
        except BaseException:
            self._log_delegate_failure(trace)
            raise
        audit = result.pop("_reconsideration_audit", {})
        if not isinstance(audit, Mapping):
            audit = {}
        state = str(result.get("state", "failed"))
        self.attempts.states[-1] = state
        if trace.send_anyway:
            self._log_reconsideration(trace, result, audit)
        runtime = trace.runtime
        if state == "held":
            runtime = self._stage_held_result(trace, result)
        self._log_result(trace, result, state, runtime)
        return result

    async def send_message(self, **kwargs: Any) -> dict[str, Any]:
        return await self.send(kwargs)
