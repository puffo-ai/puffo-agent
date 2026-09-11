"""Stateless response-contract validation for coordinated message sends."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from .send_coordinator import SendResult


logger = logging.getLogger(__name__)
BASELINE_PERSISTENCE_WARNING = (
    "The message was committed, but the local freshness baseline could not be "
    "saved; a later channel send may need to resynchronize context."
)


_LEGACY_HELD_RESPONSE_FIELDS = frozenset(
    {
        "state",
        "envelope_id",
        "context_baseline_seq",
        "seen_seq",
        "latest_seq",
        "latest_envelope_id",
    }
)
_BLOCKING_HELD_RESPONSE_FIELDS = frozenset(
    {
        "blocking_seq",
        "blocking_envelope_id",
        "blocking_sender_slug",
    }
)
_CURRENT_HELD_RESPONSE_FIELDS = (
    _LEGACY_HELD_RESPONSE_FIELDS | _BLOCKING_HELD_RESPONSE_FIELDS
)


def coordinator_config(coordinator: Any) -> SimpleNamespace:
    """Project the routing fields expected by legacy send helpers."""
    return SimpleNamespace(
        slug=coordinator.slug,
        keystore=coordinator.keystore,
        http_client=coordinator.http_client,
        data_client=coordinator.data_client,
        workspace=coordinator.workspace,
        keyless=bool(getattr(coordinator.http_client, "keyless", False)),
    )


def http_error_detail(body: str) -> str:
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return str(body)[:500] or "HTTP request failed"
    if isinstance(parsed, Mapping):
        return str(parsed.get("message") or parsed.get("error") or parsed)[:500]
    return str(parsed)[:500]


def optional_response_int(raw: Any, name: str, prefix: str) -> int | None:
    value = raw.get(name) if isinstance(raw, Mapping) else None
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{prefix} has invalid {name}")
    return value


async def persist_baseline(
    source: Any,
    space_id: str,
    channel_id: str,
    request_baseline: int | None,
    established: int | None,
) -> bool:
    """Persist a validated Server-established baseline from a null-baseline commit."""
    if request_baseline is not None or established is None:
        return True
    from .send_coordinator import _call_first

    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            await _call_first(
                source,
                ("set_context_baseline_seq",),
                space_id,
                channel_id,
                established,
            )
            return True
        except Exception as exc:
            last_error = exc
    logger.error(
        "persist baseline failed after retry for %s/%s: %s",
        space_id,
        channel_id,
        last_error,
    )
    return False


def validate_keyless_response(raw: Any, request_body: Mapping[str, Any]) -> SendResult:
    from .send_coordinator import SendResult

    if isinstance(raw, SendResult):
        return raw
    if not isinstance(raw, Mapping):
        return SendResult(
            state="failed",
            error="malformed coordinated keyless response",
            error_kind="protocol",
        )
    state = raw.get("state")
    freshness = request_body["freshness"]
    if state == "sent":
        return _keyless_sent_result(raw, freshness)
    if state == "held":
        return _keyless_held_result(raw, freshness)
    return SendResult(
        state="failed",
        error="unknown coordinated keyless state",
        error_kind="protocol",
    )


def _keyless_sent_result(
    raw: Mapping[str, Any], freshness: Mapping[str, Any]
) -> SendResult:
    from .send_coordinator import SendResult

    envelope_id, seq, replay = raw.get("envelope_id"), raw.get("seq"), raw.get("replay")
    missing, queued, echoed = (
        raw.get("missing_devices"),
        raw.get("devices_queued"),
        raw.get("freshness"),
    )
    valid = (
        isinstance(envelope_id, str)
        and bool(envelope_id)
        and not isinstance(seq, bool)
        and isinstance(seq, int)
        and seq > 0
        and isinstance(replay, bool)
        and isinstance(missing, list)
        and all(isinstance(item, str) for item in missing)
        and (
            queued is None
            or (
                not isinstance(queued, bool) and isinstance(queued, int) and queued >= 0
            )
        )
        and _keyless_freshness_is_valid(echoed, freshness)
    )
    if not valid:
        return SendResult(
            state="failed",
            error="invalid coordinated keyless commit",
            error_kind="protocol",
        )
    return SendResult(
        state="sent",
        envelope_id=envelope_id,
        seq=seq,
        replay=replay,
        devices_queued=queued,
        missing_devices=list(missing),
        context_baseline_seq=echoed["context_baseline_seq"],
        seen_seq=freshness["seen_seq"],
        latest_seq_before_send=echoed["latest_seq_before_send"],
        mode=echoed["mode"],
    )


def _keyless_freshness_is_valid(echoed: Any, freshness: Mapping[str, Any]) -> bool:
    if not isinstance(echoed, Mapping) or set(echoed) != {
        "mode",
        "context_baseline_seq",
        "seen_seq",
        "latest_seq_before_send",
    }:
        return False
    latest = echoed.get("latest_seq_before_send")
    echoed_baseline = echoed.get("context_baseline_seq")
    request_baseline = freshness["context_baseline_seq"]
    baseline_ok = (
        not isinstance(echoed_baseline, bool)
        and isinstance(echoed_baseline, int)
        and echoed_baseline >= 0
        and (request_baseline is None or echoed_baseline == request_baseline)
    )
    # A null-baseline commit has no local boundary, so the Server establishes
    # the baseline at the latest committed seq; the echo must agree exactly.
    established_at_latest = request_baseline is not None or (
        not isinstance(latest, bool)
        and isinstance(latest, int)
        and latest == echoed_baseline
    )
    return (
        echoed.get("mode") == freshness["mode"]
        and echoed.get("seen_seq") == freshness["seen_seq"]
        and baseline_ok
        and established_at_latest
        and not isinstance(latest, bool)
        and isinstance(latest, int)
        and latest >= freshness["seen_seq"]
        and (
            freshness["mode"] != "require_current"
            or latest == max(echoed_baseline, freshness["seen_seq"])
        )
    )


def _keyless_held_result(
    raw: Mapping[str, Any], freshness: Mapping[str, Any]
) -> SendResult:
    from .send_coordinator import SendResult

    latest, envelope_id = raw.get("latest_seq"), raw.get("latest_envelope_id")
    has_blocker = any(key in raw for key in _BLOCKING_HELD_RESPONSE_FIELDS)
    valid = (
        raw.get("seen_seq") == freshness["seen_seq"]
        and raw.get("context_baseline_seq") == freshness["context_baseline_seq"]
        and not isinstance(latest, bool)
        and isinstance(latest, int)
        and latest > freshness["seen_seq"]
        and isinstance(envelope_id, str)
        and bool(envelope_id)
        and (not has_blocker or _keyless_blocker_is_valid(raw, latest, envelope_id))
    )
    if not valid:
        return SendResult(
            state="failed",
            error="invalid coordinated keyless hold",
            error_kind="protocol",
        )
    return SendResult(
        state="held",
        context_baseline_seq=freshness["context_baseline_seq"],
        seen_seq=freshness["seen_seq"],
        latest_seq=latest,
        latest_envelope_id=envelope_id,
        blocking_seq=raw.get("blocking_seq"),
        blocking_envelope_id=raw.get("blocking_envelope_id"),
        blocking_sender_slug=raw.get("blocking_sender_slug"),
    )


def _keyless_blocker_is_valid(
    raw: Mapping[str, Any], latest: int, envelope_id: str
) -> bool:
    return (
        _BLOCKING_HELD_RESPONSE_FIELDS.issubset(raw)
        and raw.get("blocking_seq") == latest
        and raw.get("blocking_envelope_id") == envelope_id
        and isinstance(raw.get("blocking_sender_slug"), str)
        and bool(raw.get("blocking_sender_slug", "").strip())
    )


def validate_channel_response(
    raw: Any, envelope: Mapping[str, Any], freshness: Mapping[str, Any]
) -> SendResult:
    from .send_coordinator import SendResult

    if isinstance(raw, SendResult):
        return raw
    if not isinstance(raw, Mapping):
        return _protocol_error("channel send returned a malformed response")
    state = raw.get("state")
    envelope_id = envelope.get("envelope_id")
    if not isinstance(envelope_id, str) or not envelope_id.strip():
        return _protocol_error("request envelope_id is invalid")
    if state not in ("sent", "held"):
        return _protocol_error("unknown channel send state")
    if raw.get("envelope_id") != envelope_id:
        return _protocol_error("response envelope_id mismatch")
    if state == "sent":
        return _validate_sent_channel_response(raw, envelope_id, freshness)
    return _validate_held_channel_response(raw, envelope_id, freshness)


def _validate_sent_channel_response(
    raw: Mapping[str, Any], envelope_id: str, freshness: Mapping[str, Any]
) -> SendResult:
    from .send_coordinator import SendResult

    seq = raw.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
        return _protocol_error("sent response has invalid seq")
    if "replay" not in raw:
        return _protocol_error("sent response omitted replay")
    replay = raw["replay"]
    if not isinstance(replay, bool):
        return _protocol_error("sent response has invalid replay")
    validated = _validate_sent_freshness(raw, freshness, replay)
    if isinstance(validated, SendResult):
        return validated
    has_freshness, latest_before, established_baseline = validated
    if "missing_devices" not in raw:
        return _protocol_error("sent response omitted missing_devices")
    missing = raw["missing_devices"]
    if not isinstance(missing, list) or not all(
        isinstance(value, str) for value in missing
    ):
        return _protocol_error("sent response has invalid missing_devices")
    devices_queued = raw.get("devices_queued")
    if devices_queued is not None and (
        isinstance(devices_queued, bool)
        or not isinstance(devices_queued, int)
        or devices_queued < 0
    ):
        return _protocol_error("sent response has invalid devices_queued")
    return SendResult(
        state="sent",
        envelope_id=envelope_id,
        seq=seq,
        replay=replay,
        devices_queued=devices_queued,
        context_baseline_seq=established_baseline,
        seen_seq=freshness["seen_seq"],
        latest_seq_before_send=latest_before if has_freshness else None,
        missing_devices=missing,
    )


def _validate_sent_freshness(
    raw: Mapping[str, Any], freshness: Mapping[str, Any], replay: bool
) -> tuple[bool, int | None, int | None] | SendResult:
    has_freshness = "freshness" in raw
    response = raw.get("freshness")
    # Legacy-created replay is identified by replay=true and no stored v2
    # freshness metadata in the frozen Server contract.
    if not has_freshness and replay is not True:
        return _protocol_error("sent response omitted freshness")
    if not has_freshness:
        return False, None, None
    if not isinstance(response, Mapping):
        return _protocol_error("sent response freshness is malformed")
    if set(response) != {
        "mode",
        "context_baseline_seq",
        "seen_seq",
        "latest_seq_before_send",
    }:
        return _protocol_error("sent response freshness fields mismatch")
    baseline_echo = response["context_baseline_seq"]
    seen_echo = response["seen_seq"]
    latest_before = response.get("latest_seq_before_send")
    if not _sent_freshness_matches(
        response, freshness, baseline_echo, seen_echo, latest_before
    ):
        return _protocol_error("sent response freshness mismatch")
    return True, latest_before, baseline_echo


def _sent_freshness_matches(
    response: Mapping[str, Any],
    freshness: Mapping[str, Any],
    baseline_echo: Any,
    seen_echo: Any,
    latest_before: Any,
) -> bool:
    request_baseline = freshness["context_baseline_seq"]
    baseline_ok = (
        not isinstance(baseline_echo, bool)
        and isinstance(baseline_echo, int)
        and baseline_echo >= 0
        and (request_baseline is None or baseline_echo == request_baseline)
    )
    # A null-baseline commit has no local boundary, so the Server establishes
    # the baseline at the latest committed seq; the echo must agree exactly.
    established_at_latest = request_baseline is not None or (
        not isinstance(latest_before, bool)
        and isinstance(latest_before, int)
        and latest_before == baseline_echo
    )
    return not (
        response.get("mode") != freshness["mode"]
        or not baseline_ok
        or not established_at_latest
        or isinstance(seen_echo, bool)
        or not isinstance(seen_echo, int)
        or seen_echo < 0
        or seen_echo != freshness["seen_seq"]
        or isinstance(latest_before, bool)
        or not isinstance(latest_before, int)
        or latest_before < 0
        or latest_before < max(baseline_echo, seen_echo)
        or (
            freshness["mode"] == "require_current"
            and latest_before != max(baseline_echo, seen_echo)
        )
    )


def _validate_held_channel_response(
    raw: Mapping[str, Any], envelope_id: str, freshness: Mapping[str, Any]
) -> SendResult:
    from .send_coordinator import SendResult

    response_fields = frozenset(raw)
    if response_fields not in {
        _LEGACY_HELD_RESPONSE_FIELDS,
        _CURRENT_HELD_RESPONSE_FIELDS,
    }:
        return _protocol_error("held response fields mismatch")
    if raw["context_baseline_seq"] != freshness["context_baseline_seq"]:
        return _protocol_error("held response watermark mismatch")
    values = {
        "seen_seq": raw["seen_seq"],
        "latest_seq": raw["latest_seq"],
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values.values()
    ):
        return _protocol_error("held response has incomplete boundary watermarks")
    boundary = freshness["seen_seq"]
    if freshness["context_baseline_seq"] is not None:
        boundary = max(boundary, freshness["context_baseline_seq"])
    if (
        values["seen_seq"] != freshness["seen_seq"]
        or values["latest_seq"] <= boundary
        or not isinstance(raw.get("latest_envelope_id"), str)
        or not raw.get("latest_envelope_id").strip()
    ):
        return _protocol_error("held response watermark mismatch")
    if response_fields == _CURRENT_HELD_RESPONSE_FIELDS and (
        raw["blocking_seq"] != values["latest_seq"]
        or raw["blocking_envelope_id"] != raw["latest_envelope_id"]
        or not isinstance(raw["blocking_sender_slug"], str)
        or not raw["blocking_sender_slug"].strip()
    ):
        return _protocol_error("held response blocker metadata mismatch")
    return SendResult(
        state="held",
        envelope_id=envelope_id,
        context_baseline_seq=freshness["context_baseline_seq"],
        seen_seq=values["seen_seq"],
        latest_seq=values["latest_seq"],
        latest_envelope_id=raw["latest_envelope_id"],
        blocking_seq=raw.get("blocking_seq"),
        blocking_envelope_id=raw.get("blocking_envelope_id"),
        blocking_sender_slug=raw.get("blocking_sender_slug"),
    )


def _protocol_error(message: str) -> SendResult:
    from .send_coordinator import SendResult

    return SendResult(state="failed", error=message, error_kind="protocol")
