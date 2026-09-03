"""POSIX Driver authority server for constrained LingTai ACP children.

The inherited descriptor is only a locator for one connected AF_UNIX socket.
All authority stays in the Driver-owned endpoint record: role, lineage, depth,
capability, and provider are never accepted from child process state.
"""

from __future__ import annotations

import array
import json
import logging
import os
import socket
import struct
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


DRIVER_AUTHORITY_FD_ENV = "LINGTAI_DRIVER_AUTHORITY_FD"
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024

logger = logging.getLogger(__name__)


class _LeaseState(str, Enum):
    ISSUED = "issued"
    CLAIMED = "claimed"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class AuthorityAuditRecord:
    audit_id: str
    operation: str
    launch_id: str
    state: str
    reason_code: str
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class _EndpointBinding:
    launch_id: str
    role: str
    parent_launch_id: str | None
    depth: int
    capability: str | None
    provider: str = "llm"

    @property
    def provider_capability(self) -> str:
        if self.role == "root":
            return "root"
        return "daemon" if self.capability == "daemon" else "avatar_child"


@dataclass(slots=True)
class _EndpointRecord:
    server_socket: socket.socket
    binding: _EndpointBinding
    state: _LeaseState = _LeaseState.ISSUED
    buffer: bytearray = field(default_factory=bytearray)
    thread: threading.Thread | None = None


class IssuedAuthorityEndpoint:
    """One child-side socket held by the Driver until the immediate spawn."""

    __slots__ = ("_socket",)

    def __init__(self, endpoint: socket.socket) -> None:
        self._socket: socket.socket | None = endpoint

    def fileno(self) -> int:
        endpoint = self._socket
        if endpoint is None:
            raise RuntimeError("authority endpoint is already closed")
        return endpoint.fileno()

    def close(self) -> None:
        endpoint, self._socket = self._socket, None
        if endpoint is not None:
            endpoint.close()


class DriverAuthorityServer:
    """Own endpoint bindings and decide every LingTai provider/launch request."""

    def __init__(self, *, claim_probe: Callable[[], None] | None = None) -> None:
        if os.name != "posix" or not hasattr(socket, "SCM_RIGHTS"):
            raise RuntimeError("Driver authority requires POSIX SCM_RIGHTS")
        self._lock = threading.Lock()
        self._records: list[_EndpointRecord] = []
        self._audits: list[AuthorityAuditRecord] = []
        self._closed = False
        # Deterministic concurrency tests pause after observing ISSUED while
        # the state lock is still held. Production never supplies this hook.
        self._claim_probe = claim_probe

    def issue_root(self, *, launch_id: str) -> IssuedAuthorityEndpoint:
        """Create and start the endpoint for one root ACP process launch."""

        if not launch_id:
            raise ValueError("root launch_id must be non-empty")
        binding = _EndpointBinding(launch_id, "root", None, 0, None)
        record, child = self._issue_endpoint(binding)
        self._start_record(record)
        return IssuedAuthorityEndpoint(child)

    def audit_records(self) -> tuple[AuthorityAuditRecord, ...]:
        with self._lock:
            return tuple(self._audits)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            records = tuple(self._records)
            for record in records:
                record.state = _LeaseState.CLOSED
        for record in records:
            try:
                record.server_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            record.server_socket.close()
        current = threading.current_thread()
        for record in records:
            if record.thread is not None and record.thread is not current:
                record.thread.join(timeout=2)

    def _issue_endpoint(
        self, binding: _EndpointBinding
    ) -> tuple[_EndpointRecord, socket.socket]:
        server, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        os.set_inheritable(server.fileno(), False)
        os.set_inheritable(child.fileno(), False)
        record = _EndpointRecord(server, binding)
        with self._lock:
            if self._closed:
                server.close()
                child.close()
                raise RuntimeError("Driver authority server is closed")
            self._records.append(record)
        return record, child

    def _start_record(self, record: _EndpointRecord) -> None:
        thread = threading.Thread(
            target=self._serve,
            args=(record,),
            name=f"puffo.driver-authority.{record.binding.launch_id}",
            daemon=True,
        )
        record.thread = thread
        thread.start()

    def _serve(self, record: _EndpointRecord) -> None:
        try:
            while True:
                request = self._recv_frame(record)
                response, child = self._handle_request(record, request)
                # Correlation is stamped here, at the one point every response
                # passes through on its way to the wire. A peer that matches
                # responses to requests by call_id rejects any reply that omits
                # it, and this server has ten leaf response paths; threading a
                # parameter through each one means a path added later can drop
                # the echo silently. Only a request that correlates gets a
                # correlation key back.
                request_call_id = request.get("call_id")
                if isinstance(request_call_id, str):
                    response["call_id"] = request_call_id
                try:
                    self._send_frame(record.server_socket, response, child=child)
                finally:
                    if child is not None:
                        child.close()
        except (EOFError, OSError, ValueError):
            pass
        except Exception:
            logger.exception(
                "Driver authority endpoint failed for launch %s",
                record.binding.launch_id,
            )
        finally:
            with self._lock:
                record.state = _LeaseState.CLOSED
            record.server_socket.close()

    def _handle_request(
        self, record: _EndpointRecord, request: dict[str, Any]
    ) -> tuple[dict[str, Any], socket.socket | None]:
        operation = request.get("op")
        call_id = request.get("call_id")
        if not isinstance(call_id, str):
            call_id = None
        if request.get("version") != PROTOCOL_VERSION or not isinstance(operation, str):
            return self._decision(
                record, "malformed", "indeterminate", "malformed_request", call_id=call_id
            ), None
        if operation == "hello":
            return self._claim(record, call_id=call_id), None
        with self._lock:
            claimed = record.state is _LeaseState.CLAIMED
        if not claimed:
            return self._mismatch(record, operation, call_id=call_id), None
        if operation == "authorize_derived_launch":
            return self._authorize_derived_launch(record, request, call_id=call_id)
        if operation == "authorize_provider_call":
            return self._authorize_provider_call(record, request), None
        return self._decision(
            record, operation, "indeterminate", "unsupported_operation", call_id=call_id
        ), None

    def _claim(self, record: _EndpointRecord, *, call_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if record.state is not _LeaseState.ISSUED:
                return self._record_decision_locked(
                    record, "hello", "denied", "endpoint_already_claimed", call_id=call_id
                )
            if self._claim_probe is not None:
                self._claim_probe()
            record.state = _LeaseState.CLAIMED
            binding = record.binding
        return {
            "version": PROTOCOL_VERSION,
            "role": binding.role,
            "launch_id": binding.launch_id,
            "capability": binding.capability,
        }

    def _authorize_derived_launch(
        self, record: _EndpointRecord, request: dict[str, Any], *, call_id: str | None = None
    ) -> tuple[dict[str, Any], socket.socket | None]:
        binding = record.binding
        if binding.role != "root":
            return self._decision(
                record,
                "authorize_derived_launch",
                "denied",
                "nested_derived_launch_denied",
                call_id=call_id,
            ), None
        capability = request.get("capability")
        if request.get("launch_id") != binding.launch_id or capability not in {
            "daemon",
            "avatar",
        }:
            return self._mismatch(record, "authorize_derived_launch", call_id=call_id), None
        child_binding = _EndpointBinding(
            launch_id=f"launch_{uuid.uuid4().hex}",
            role="derived",
            parent_launch_id=binding.launch_id,
            depth=1,
            capability=capability,
        )
        child_record, child_endpoint = self._issue_endpoint(child_binding)
        self._start_record(child_record)
        response = self._decision(
            record, "authorize_derived_launch", "granted", "allowed", call_id=call_id
        )
        response["admission_id"] = f"admission_{uuid.uuid4().hex}"
        return response, child_endpoint

    def _authorize_provider_call(
        self, record: _EndpointRecord, request: dict[str, Any]
    ) -> dict[str, Any]:
        binding = record.binding
        call_id = request.get("call_id")
        valid_call_id = isinstance(call_id, str) and _is_uuid(call_id)
        matches = (
            valid_call_id
            and request.get("launch_id") == binding.launch_id
            and request.get("provider") == binding.provider
            and request.get("capability") == binding.provider_capability
        )
        if not matches:
            return self._mismatch(
                record,
                "authorize_provider_call",
                call_id=call_id if isinstance(call_id, str) else None,
            )
        return self._decision(
            record,
            "authorize_provider_call",
            "granted",
            "allowed",
            call_id=call_id,
        )

    def _mismatch(
        self,
        record: _EndpointRecord,
        operation: str,
        *,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        return self._decision(
            record,
            operation,
            "denied",
            "endpoint_binding_mismatch",
            call_id=call_id,
        )

    def _decision(
        self,
        record: _EndpointRecord,
        operation: str,
        state: str,
        reason_code: str,
        *,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            return self._record_decision_locked(
                record, operation, state, reason_code, call_id=call_id
            )

    def _record_decision_locked(
        self,
        record: _EndpointRecord,
        operation: str,
        state: str,
        reason_code: str,
        *,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        audit_id = f"audit_{uuid.uuid4().hex}"
        self._audits.append(
            AuthorityAuditRecord(
                audit_id,
                operation,
                record.binding.launch_id,
                state,
                reason_code,
                call_id,
            )
        )
        return {
            "version": PROTOCOL_VERSION,
            "state": state,
            "reason_code": reason_code,
            "audit_id": audit_id,
        }

    @staticmethod
    def _send_frame(
        endpoint: socket.socket,
        payload: dict[str, Any],
        *,
        child: socket.socket | None = None,
    ) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
        if not encoded or len(encoded) > MAX_FRAME_BYTES:
            raise ValueError("authority response exceeds frame bound")
        frame = struct.pack("!I", len(encoded)) + encoded
        if child is None:
            endpoint.sendall(frame)
            return
        rights = array.array("i", [child.fileno()])
        sent = endpoint.sendmsg(
            [frame], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)]
        )
        if sent < len(frame):
            endpoint.sendall(frame[sent:])

    @staticmethod
    def _recv_frame(record: _EndpointRecord) -> dict[str, Any]:
        received_fds: list[int] = []

        def read_exact(count: int) -> bytes:
            while len(record.buffer) < count:
                data, ancdata, flags, _ = record.server_socket.recvmsg(
                    MAX_FRAME_BYTES + 4,
                    socket.CMSG_SPACE(array.array("i", [0]).itemsize),
                )
                if not data:
                    raise EOFError
                if flags & socket.MSG_CTRUNC:
                    raise ValueError("authority request ancillary data was truncated")
                for level, kind, raw in ancdata:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        items = array.array("i")
                        usable = len(raw) - (len(raw) % items.itemsize)
                        items.frombytes(raw[:usable])
                        received_fds.extend(items.tolist())
                record.buffer.extend(data)
            value = bytes(record.buffer[:count])
            del record.buffer[:count]
            return value

        try:
            size = struct.unpack("!I", read_exact(4))[0]
            if size <= 0 or size > MAX_FRAME_BYTES:
                raise ValueError("authority request frame is out of bounds")
            raw = read_exact(size)
            if received_fds:
                raise ValueError("authority request must not carry descriptors")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("authority request must be an object")
            return value
        finally:
            for fd in received_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


__all__ = [
    "AuthorityAuditRecord",
    "DRIVER_AUTHORITY_FD_ENV",
    "DriverAuthorityServer",
    "IssuedAuthorityEndpoint",
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
]
