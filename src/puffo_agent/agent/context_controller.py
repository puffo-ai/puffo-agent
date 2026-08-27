"""Provider-neutral context admission and control.

This module deliberately knows nothing about Inbox storage or message
lifecycle.  Callers supply opaque candidate payloads and a re-plan callback;
providers supply snapshots and bounded native control operations.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import json
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable


FALLBACK_CONTEXT_WINDOW = 200_000
MODEL_VISIBLE_READ_RECEIPT_PREFIX = "puffo:model-visible-read:"
SOFT_TARGET_FRACTION = 0.5


class ContextSource(str, Enum):
    PROVIDER = "provider"
    MODEL = "verified_model"
    FALLBACK = "fallback_200000"
    ESTIMATED = "estimated"
    STATELESS = "stateless_estimated"


@dataclass(frozen=True)
class ContextSnapshot:
    used_tokens: int
    context_window: int
    source: str
    measured_at: datetime


@dataclass(frozen=True)
class ContextCapabilities:
    native_compaction: bool = False
    rollover: bool = False
    native_measurement: bool = False
    diagnostic: str = "context control unsupported"


@dataclass(frozen=True)
class CompactionResult:
    completed: bool
    before: ContextSnapshot | None = None
    after: ContextSnapshot | None = None
    provider_session_id: str | None = None
    diagnostic: str = ""

    @property
    def success(self) -> bool:
        return self.completed


@dataclass(frozen=True)
class RolloverResult:
    completed: bool
    previous_provider_session_id: str | None = None
    provider_session_id: str | None = None
    diagnostic: str = ""

    @property
    def success(self) -> bool:
        return self.completed


@dataclass(frozen=True)
class AdmissionCandidate:
    planning_cycle_key: str
    formatted_batch_tokens: int
    wrapper_overhead_tokens: int
    output_tool_reserve_tokens: int
    payload: Any = None
    minimum_formatted_batch_tokens: int = 0

    @property
    def projected_tokens(self) -> int:
        return (
            self.formatted_batch_tokens
            + self.wrapper_overhead_tokens
            + self.output_tool_reserve_tokens
        )


class DecisionOutcome(str, Enum):
    ADMIT = "admit"
    REPLAN = "replan"
    SHRINK = "shrink"
    ROLLOVER = "rollover"
    DEGRADED = "degraded"


# Short alias useful to integrations that prefer the domain noun.
ContextDecisionOutcome = DecisionOutcome


@dataclass(frozen=True)
class ContextDecision:
    outcome: DecisionOutcome
    candidate: AdmissionCandidate
    snapshot: ContextSnapshot
    projected_tokens: int
    diagnostic: str = ""


@dataclass(frozen=True)
class ProviderAdmissionEvent:
    planning_cycle_key: str
    provider_session_id: str | None
    admitted_at: datetime
    provider_turn_id: str | None = None
    tool_call_id: str | None = None


AdmissionCallback = Callable[[ProviderAdmissionEvent], Awaitable[None]]
ReplanCallback = Callable[[AdmissionCandidate], Awaitable[AdmissionCandidate]]


@dataclass(frozen=True)
class ToolResultAdmission:
    """One callback correlated to a concrete provider tool result."""

    callback: AdmissionCallback
    planning_cycle_key: str
    provider_turn_id: str
    channel_id: str = ""
    tool_names: frozenset[str] = frozenset()
    tool_arguments: tuple[tuple[str, str], ...] = ()
    correlation_receipt: str = ""

    @classmethod
    def build(
        cls,
        callback: AdmissionCallback,
        planning_cycle_key: str,
        provider_turn_id: str,
        *,
        channel_id: str = "",
        tool_names: tuple[str, ...] = (),
        tool_arguments: Mapping[str, Any] | None = None,
        correlation_receipt: str = "",
    ) -> ToolResultAdmission:
        encoded_arguments = normalize_tool_arguments(tool_arguments)
        return cls(
            callback=callback,
            planning_cycle_key=planning_cycle_key,
            provider_turn_id=provider_turn_id,
            channel_id=channel_id,
            tool_names=frozenset(
                normalize_tool_name(name) for name in tool_names if name
            ),
            tool_arguments=encoded_arguments,
            correlation_receipt=correlation_receipt,
        )

    @property
    def receipt_marker(self) -> str:
        if not self.correlation_receipt:
            return ""
        return (
            f"[{MODEL_VISIBLE_READ_RECEIPT_PREFIX}"
            f"{self.correlation_receipt}]"
        )

    def matches(self, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        semantic_tool_name = normalize_tool_name(tool_name)
        if self.tool_names and semantic_tool_name not in self.tool_names:
            return False
        if self.channel_id and str(arguments.get("channel") or "") != self.channel_id:
            return False
        # This is deliberately equality, not a subset check: a continuation
        # must be the original semantic send, not merely a call that happens
        # to share its channel or text.  Provider-added default optionals are
        # removed by the shared normalizer on both sides.
        observed = normalize_tool_arguments(arguments)
        if not self.tool_arguments:
            return True
        if semantic_tool_name in {
            "send_message",
            "send_message_with_attachments",
        }:
            return self.tool_arguments == observed
        # Existing history-tool registrations intentionally specify only the
        # stable semantic fields; provider pagination defaults are not part of
        # their continuation contract.
        return all(item in observed for item in self.tool_arguments)

    @property
    def match_specificity(self) -> int:
        """Prefer the most constrained admission when argument subsets overlap."""
        return len(self.tool_arguments) + int(bool(self.channel_id))


def normalize_tool_arguments(
    arguments: Mapping[str, Any] | None,
) -> tuple[tuple[str, str], ...]:
    """Deterministic semantic representation shared by all providers.

    JSON encoding avoids provider- and Python-version-specific ``repr``
    differences, recursively normalizes mappings, and treats public omitted
    optional send defaults as omitted whether a provider echoes them or not.
    """
    optional_defaults = {
        "root_id": "",
        "visibility_level": "default",
        "send_anyway": False,
    }
    normalized: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        name = str(key)
        if name in optional_defaults and value == optional_defaults[name]:
            continue
        # Local history tools cap their public ``limit`` at 200 before
        # staging their receipt.  Claude reports the requested tool input,
        # while other adapters can report the applied value; compare the
        # shared semantic value so legacy history continuations retain their
        # established matching contract.
        if name == "limit":
            try:
                value = max(1, min(int(value), 200))
            except (TypeError, ValueError):
                pass
        normalized[name] = value
    return tuple(sorted(
        (key, json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))
        for key, value in normalized.items()
    ))


def normalize_tool_name(tool_name: str) -> str:
    """Map trusted Puffo MCP wire names to their provider-neutral identity."""
    name = str(tool_name or "")
    prefix = "mcp__puffo__"
    return name[len(prefix):] if name.startswith(prefix) else name


@runtime_checkable
class ContextProvider(Protocol):
    async def get_context_snapshot(self) -> ContextSnapshot: ...

    def get_context_capabilities(self) -> ContextCapabilities: ...

    async def compact_context(self) -> CompactionResult: ...

    async def rollover_context(self) -> RolloverResult: ...

    def get_provider_session_id(self) -> str | None: ...


def normalize_context_snapshot(
    *,
    used_tokens: int,
    provider_context_window: int | None = None,
    verified_model_context_window: int | None = None,
    measured_at: datetime | None = None,
    estimated_source: str | None = None,
) -> ContextSnapshot:
    """Normalize capacity with provider > verified model > labeled fallback."""
    when = measured_at or datetime.now(timezone.utc)
    if provider_context_window and provider_context_window > 0:
        window = provider_context_window
        source = ContextSource.PROVIDER.value
    elif verified_model_context_window and verified_model_context_window > 0:
        window = verified_model_context_window
        source = ContextSource.MODEL.value
    else:
        window = FALLBACK_CONTEXT_WINDOW
        source = estimated_source or ContextSource.FALLBACK.value
    return ContextSnapshot(
        used_tokens=max(0, int(used_tokens)),
        context_window=int(window),
        source=source,
        measured_at=when,
    )


class ContextController:
    """Bounded context decision loop.

    A call performs no storage mutation and never claims a message.  A
    successful native compact returns ``replan`` after refreshing provider
    state and awaiting the caller's re-read/re-plan callback.
    """

    def __init__(
        self,
        provider: ContextProvider,
        replan: ReplanCallback | None = None,
    ) -> None:
        self._provider = provider
        self._replan = replan
        self._successful_compactions: set[str] = set()
        self._failed_compactions: dict[str, int] = {}
        self._rollovers: set[str] = set()

    @staticmethod
    def projection(snapshot: ContextSnapshot, candidate: AdmissionCandidate) -> int:
        return snapshot.used_tokens + candidate.projected_tokens

    @staticmethod
    def soft_target(snapshot: ContextSnapshot) -> int:
        return int(snapshot.context_window * SOFT_TARGET_FRACTION)

    async def decide(
        self,
        candidate: AdmissionCandidate,
        replan: ReplanCallback | None = None,
    ) -> ContextDecision:
        snapshot = await self._provider.get_context_snapshot()
        projected = self.projection(snapshot, candidate)
        if projected <= self.soft_target(snapshot):
            return ContextDecision(
                DecisionOutcome.ADMIT, candidate, snapshot, projected,
            )

        key = candidate.planning_cycle_key
        capabilities = self._provider.get_context_capabilities()
        while (
            capabilities.native_compaction
            and key not in self._successful_compactions
            and self._failed_compactions.get(key, 0) < 2
        ):
            compact = await self._provider.compact_context()
            if compact.completed:
                self._successful_compactions.add(key)
                # Completion happens before refresh, which happens before replan.
                refreshed = await self._provider.get_context_snapshot()
                callback = replan or self._replan
                replacement = await callback(candidate) if callback else candidate
                return ContextDecision(
                    DecisionOutcome.REPLAN,
                    replacement,
                    refreshed,
                    self.projection(refreshed, replacement),
                    compact.diagnostic,
                )
            self._failed_compactions[key] = self._failed_compactions.get(key, 0) + 1

        target = self.soft_target(snapshot)
        fixed = (
            snapshot.used_tokens
            + candidate.wrapper_overhead_tokens
            + candidate.output_tool_reserve_tokens
        )
        minimum = max(0, candidate.minimum_formatted_batch_tokens)
        if fixed + minimum <= target and candidate.formatted_batch_tokens > minimum:
            shrunk = replace(candidate, formatted_batch_tokens=minimum)
            return ContextDecision(
                DecisionOutcome.SHRINK,
                shrunk,
                snapshot,
                self.projection(snapshot, shrunk),
                "candidate reduction can satisfy the soft target",
            )

        if (
            capabilities.rollover
            and key not in self._rollovers
            and fixed + minimum > target
        ):
            self._rollovers.add(key)
            rolled = await self._provider.rollover_context()
            if rolled.completed:
                refreshed = await self._provider.get_context_snapshot()
                return ContextDecision(
                    DecisionOutcome.ROLLOVER,
                    candidate,
                    refreshed,
                    self.projection(refreshed, candidate),
                    rolled.diagnostic,
                )

        return ContextDecision(
            DecisionOutcome.DEGRADED,
            candidate,
            snapshot,
            projected,
            "context cannot be brought below the soft target with bounded controls",
        )

    # Compatibility name for call sites that describe this as admission.
    evaluate = decide
