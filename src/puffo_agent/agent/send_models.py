"""Data contracts shared by semantic message send coordination."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class SemanticSendRequest:
    """The complete model-facing send contract (and nothing freshness-related)."""

    destination: str
    text: str = ""
    attachment_paths: tuple[str, ...] = ()
    caption: str = ""
    root_id: str = ""
    visibility_level: str = "default"
    send_anyway: bool = False
    covers: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SemanticSendRequest:
        allowed = {
            "destination",
            "channel",
            "text",
            "attachment_paths",
            "paths",
            "caption",
            "root_id",
            "visibility_level",
            "send_anyway",
            "covers",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown send field(s): {', '.join(sorted(unknown))}")
        destination = value.get("destination", value.get("channel", ""))
        paths = value.get("attachment_paths", value.get("paths", ())) or ()
        if not isinstance(paths, (list, tuple)) or not all(
            isinstance(item, str) for item in paths
        ):
            raise ValueError("attachment paths must be a list of strings")
        covers = value.get("covers", ()) or ()
        if not isinstance(covers, (list, tuple)) or not all(
            isinstance(item, str) for item in covers
        ):
            raise ValueError("covers must be a list of message-id strings")
        return cls(
            destination=str(destination or ""),
            text=str(value.get("text") or ""),
            attachment_paths=tuple(paths),
            caption=str(value.get("caption") or ""),
            root_id=str(value.get("root_id") or ""),
            visibility_level=str(value.get("visibility_level") or "default"),
            send_anyway=value.get("send_anyway", False) is True,
            covers=tuple(covers),
        )

    def to_rpc_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "channel": self.destination,
            "root_id": self.root_id,
            "visibility_level": self.visibility_level,
            "send_anyway": self.send_anyway,
        }
        if self.covers:
            body["covers"] = list(self.covers)
        if self.attachment_paths:
            body["paths"] = list(self.attachment_paths)
            body["caption"] = self.caption
        else:
            body["text"] = self.text
        return body

    def attempt_fingerprint(self) -> str:
        """Identify the logical draft behind one send attempt.

        ``send_anyway`` is excluded so an unchanged draft's reconsideration
        retry keeps the fingerprint of the attempt that was held, while any
        revised draft gets a different one.
        """
        arguments = self.to_tool_arguments()
        arguments.pop("send_anyway", None)
        return hashlib.sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def to_tool_arguments(self) -> dict[str, Any]:
        """Mirror the public model call without materializing omitted defaults."""
        arguments: dict[str, Any] = {"channel": self.destination}
        if self.attachment_paths:
            arguments.update(
                {
                    "caption": self.caption,
                    "paths": list(self.attachment_paths),
                }
            )
        else:
            arguments["text"] = self.text
        if self.root_id:
            arguments["root_id"] = self.root_id
        if self.visibility_level != "default":
            arguments["visibility_level"] = self.visibility_level
        if self.send_anyway:
            arguments["send_anyway"] = True
        if self.covers:
            arguments["covers"] = list(self.covers)
        return arguments


@dataclass
class SendResult:
    state: str
    attempted: bool = True
    envelope_id: Optional[str] = None
    seq: Optional[int] = None
    replay: Optional[bool] = None
    devices_queued: Optional[int] = None
    context_baseline_seq: Optional[int] = None
    seen_seq: Optional[int] = None
    latest_seq: Optional[int] = None
    latest_envelope_id: Optional[str] = None
    blocking_seq: Optional[int] = None
    blocking_envelope_id: Optional[str] = None
    blocking_sender_slug: Optional[str] = None
    latest_seq_before_send: Optional[int] = None
    mode: Optional[str] = None
    missing_devices: list[str] = field(default_factory=list)
    recovered_messages: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    error_kind: Optional[str] = None
    status: Optional[int] = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            key: value for key, value in data.items() if value not in (None, [], "")
        }
