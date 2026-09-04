from __future__ import annotations

import array
import json
import os
import socket
import struct
import threading
import uuid
from typing import Any

import pytest

from puffo_agent.agent.harness.drivers import acp as acp_driver_module
from puffo_agent.agent.harness.drivers.acp import AcpDriver
from puffo_agent.agent.harness.driver import RuntimeSpec
from puffo_agent.agent.harness.driver_authority_server import (
    DRIVER_AUTHORITY_FD_ENV,
    DriverAuthorityServer,
    MAX_FRAME_BYTES,
)


pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "SCM_RIGHTS"),
    reason="Driver authority uses POSIX SCM_RIGHTS",
)


def _client(endpoint) -> socket.socket:
    client = socket.socket(fileno=os.dup(endpoint.fileno()))
    endpoint.close()
    return client


def _send(client: socket.socket, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    client.sendall(struct.pack("!I", len(encoded)) + encoded)


def _receive(client: socket.socket) -> tuple[dict[str, Any], list[int]]:
    buffered = bytearray()
    received_fds: list[int] = []

    def read_exact(count: int) -> bytes:
        while len(buffered) < count:
            data, ancdata, flags, _ = client.recvmsg(
                MAX_FRAME_BYTES + 4,
                socket.CMSG_SPACE(array.array("i", [0]).itemsize),
            )
            if not data:
                raise EOFError
            assert not flags & socket.MSG_CTRUNC
            for level, kind, raw in ancdata:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    usable = len(raw) - (len(raw) % values.itemsize)
                    values.frombytes(raw[:usable])
                    received_fds.extend(values.tolist())
            buffered.extend(data)
        value = bytes(buffered[:count])
        del buffered[:count]
        return value

    size = struct.unpack("!I", read_exact(4))[0]
    response = json.loads(read_exact(size))
    assert isinstance(response, dict)
    return response, received_fds


def _request(
    client: socket.socket, payload: dict[str, Any]
) -> tuple[dict[str, Any], list[int]]:
    _send(client, payload)
    return _receive(client)


def _hello(client: socket.socket) -> dict[str, Any]:
    response, fds = _request(client, {"version": 1, "op": "hello"})
    assert fds == []
    return response


def _close_fds(fds: list[int]) -> None:
    for fd in fds:
        os.close(fd)


def test_endpoint_claim_is_single_use_and_reuse_is_audited() -> None:
    server = DriverAuthorityServer()
    client = _client(server.issue_root(launch_id="root-1"))
    try:
        assert _hello(client) == {
            "version": 1,
            "role": "root",
            "launch_id": "root-1",
            "capability": None,
        }

        denied, fds = _request(client, {"version": 1, "op": "hello"})
        assert fds == []
        assert denied["state"] == "denied"
        assert denied["reason_code"] == "endpoint_already_claimed"
        assert denied["audit_id"].startswith("audit_")
        assert server.audit_records()[-1].audit_id == denied["audit_id"]
    finally:
        client.close()
        server.close()


def test_endpoint_claim_transition_cannot_be_interleaved() -> None:
    """A second claimant cannot reach the ISSUED-to-CLAIMED decision gap."""

    first_inside = threading.Event()
    second_inside = threading.Event()
    release = threading.Event()
    probe_lock = threading.Lock()
    probe_entries = 0

    def claim_probe() -> None:
        nonlocal probe_entries
        with probe_lock:
            probe_entries += 1
            (first_inside if probe_entries == 1 else second_inside).set()
        assert release.wait(timeout=2)

    server = DriverAuthorityServer(claim_probe=claim_probe)
    endpoint = server.issue_root(launch_id="root-race")
    record = server._records[0]
    responses: list[dict[str, Any]] = []
    start = threading.Barrier(3)

    def claim() -> None:
        start.wait()
        responses.append(server._claim(record))

    claimants = [threading.Thread(target=claim) for _ in range(2)]
    for claimant in claimants:
        claimant.start()
    start.wait()
    try:
        assert first_inside.wait(timeout=2)
        assert not second_inside.wait(timeout=0.1)
        release.set()
        for claimant in claimants:
            claimant.join(timeout=2)
            assert not claimant.is_alive()

        assert probe_entries == 1
        assert sum("role" in response for response in responses) == 1
        denied = next(response for response in responses if "state" in response)
        assert denied["state"] == "denied"
        assert denied["reason_code"] == "endpoint_already_claimed"
    finally:
        release.set()
        endpoint.close()
        server.close()


def test_endpoint_binding_mismatch_is_denied_and_audited() -> None:
    server = DriverAuthorityServer()
    client = _client(server.issue_root(launch_id="root-authoritative"))
    try:
        _hello(client)
        denied, fds = _request(
            client,
            {
                "version": 1,
                "op": "authorize_provider_call",
                "call_id": str(uuid.uuid4()),
                "launch_id": "child-self-report",
                "provider": "llm",
                "capability": "root",
            },
        )

        assert fds == []
        assert denied["state"] == "denied"
        assert denied["reason_code"] == "endpoint_binding_mismatch"
        assert server.audit_records()[-1].audit_id == denied["audit_id"]
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize(
    ("override", "expected_state"),
    [
        ({"role": "derived", "depth": 99}, "granted"),
        ({"launch_id": "self-reported-root"}, "denied"),
        ({"provider": "filesystem"}, "denied"),
        ({"capability": "avatar_child"}, "denied"),
    ],
)
def test_provider_decision_uses_only_server_binding(
    override: dict[str, Any], expected_state: str
) -> None:
    server = DriverAuthorityServer()
    client = _client(server.issue_root(launch_id="root-binding"))
    try:
        _hello(client)
        request = {
            "version": 1,
            "op": "authorize_provider_call",
            "call_id": str(uuid.uuid4()),
            "launch_id": "root-binding",
            "provider": "llm",
            "capability": "root",
        }
        request.update(override)
        decision, fds = _request(client, request)

        assert fds == []
        assert decision["state"] == expected_state
        assert decision["audit_id"].startswith("audit_")
        if expected_state == "denied":
            assert decision["reason_code"] == "endpoint_binding_mismatch"
    finally:
        client.close()
        server.close()


def test_provider_calls_are_adjudicated_independently_with_readable_audit_ids() -> None:
    """D4 sends two operations on purpose; D6's legal E2E sends exactly one."""

    server = DriverAuthorityServer()
    client = _client(server.issue_root(launch_id="root-calls"))
    try:
        _hello(client)
        decisions = []
        for _ in range(2):
            decision, fds = _request(
                client,
                {
                    "version": 1,
                    "op": "authorize_provider_call",
                    "call_id": str(uuid.uuid4()),
                    "launch_id": "root-calls",
                    "provider": "llm",
                    "capability": "root",
                },
            )
            assert fds == []
            decisions.append(decision)

        assert [item["state"] for item in decisions] == ["granted", "granted"]
        assert decisions[0]["audit_id"] != decisions[1]["audit_id"]
        records = server.audit_records()
        assert [record.audit_id for record in records] == [
            decision["audit_id"] for decision in decisions
        ]
        assert all(record.operation == "authorize_provider_call" for record in records)
    finally:
        client.close()
        server.close()


def test_protocol_responses_echo_request_call_id() -> None:
    """LingTai correlates every authority response to its request."""

    server = DriverAuthorityServer()
    root = _client(server.issue_root(launch_id="root-call-id"))
    child: socket.socket | None = None
    try:
        root_hello_id = uuid.uuid4().hex
        root_hello, fds = _request(
            root, {"version": 1, "op": "hello", "call_id": root_hello_id}
        )
        assert fds == []
        assert root_hello["call_id"] == root_hello_id

        launch_id = uuid.uuid4().hex
        launch, child_fds = _request(
            root,
            {
                "version": 1,
                "op": "authorize_derived_launch",
                "call_id": launch_id,
                "launch_id": "root-call-id",
                "capability": "daemon",
            },
        )
        assert launch["call_id"] == launch_id
        child = socket.socket(fileno=child_fds.pop())

        child_hello_id = uuid.uuid4().hex
        child_hello, fds = _request(
            child, {"version": 1, "op": "hello", "call_id": child_hello_id}
        )
        assert fds == []
        assert child_hello["call_id"] == child_hello_id

        provider_id = uuid.uuid4().hex
        provider, fds = _request(
            child,
            {
                "version": 1,
                "op": "authorize_provider_call",
                "call_id": provider_id,
                "launch_id": child_hello["launch_id"],
                "provider": "llm",
                "capability": "daemon",
            },
        )
        assert fds == []
        assert provider["call_id"] == provider_id
    finally:
        _close_fds(child_fds if "child_fds" in locals() else [])
        if child is not None:
            child.close()
        root.close()
        server.close()


def test_root_can_issue_one_hop_but_derived_cannot_issue_nested_child() -> None:
    server = DriverAuthorityServer()
    root = _client(server.issue_root(launch_id="root-parent"))
    child: socket.socket | None = None
    try:
        _hello(root)
        granted, child_fds = _request(
            root,
            {
                "version": 1,
                "op": "authorize_derived_launch",
                "launch_id": "root-parent",
                "capability": "daemon",
            },
        )
        assert granted["state"] == "granted"
        assert granted["audit_id"].startswith("audit_")
        assert granted["admission_id"].startswith("admission_")
        assert len(child_fds) == 1

        child = socket.socket(fileno=child_fds.pop())
        child_hello = _hello(child)
        assert child_hello["role"] == "derived"
        assert child_hello["capability"] == "daemon"

        denied, nested_fds = _request(
            child,
            {
                "version": 1,
                "op": "authorize_derived_launch",
                "launch_id": child_hello["launch_id"],
                "capability": "avatar",
            },
        )
        assert nested_fds == []
        assert denied["state"] == "denied"
        assert denied["reason_code"] == "nested_derived_launch_denied"
        assert all(
            record.operation != "authorize_provider_call"
            for record in server.audit_records()
        )
    finally:
        _close_fds(nested_fds if "nested_fds" in locals() else [])
        _close_fds(child_fds if "child_fds" in locals() else [])
        if child is not None:
            child.close()
        root.close()
        server.close()


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00\x00\x00\x00",
        struct.pack("!I", MAX_FRAME_BYTES + 1),
        struct.pack("!I", 1) + b"{",
    ],
)
def test_malformed_frames_disconnect_without_a_decision(payload: bytes) -> None:
    server = DriverAuthorityServer()
    client = _client(server.issue_root(launch_id="root-malformed"))
    client.settimeout(2)
    try:
        client.sendall(payload)
        assert client.recv(1) == b""
        assert server.audit_records() == ()
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("profile", ["puffo-v0", "puffo-v1"])
@pytest.mark.asyncio
async def test_constrained_lingtai_spawn_gets_only_the_issued_endpoint(
    monkeypatch, profile,
) -> None:
    captured: dict[str, Any] = {}

    async def spawn(*command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        captured["child_fd"] = os.dup(kwargs["pass_fds"][0])
        return object()

    monkeypatch.setattr(acp_driver_module.asyncio, "create_subprocess_exec", spawn)
    driver = AcpDriver()
    await driver._spawn(
        driver._validate_launch_plan(
            RuntimeSpec(
                "/workspace",
                executable="lingtai",
                launch_args=(
                    "acp",
                    "--profile",
                    profile,
                    "--runtime-id",
                    "r1",
                ),
                environment={
                    DRIVER_AUTHORITY_FD_ENV: "caller-controlled",
                    "SAFE": "1",
                },
            )
        )
    )
    child = socket.socket(fileno=captured.pop("child_fd"))
    try:
        assert captured["command"] == (
            "lingtai",
            "acp",
            "--profile",
            profile,
            "--runtime-id",
            "r1",
        )
        inherited_fd = captured["pass_fds"][0]
        assert captured["env"] == {
            DRIVER_AUTHORITY_FD_ENV: str(inherited_fd),
            "SAFE": "1",
        }
        assert _hello(child) == {
            "version": 1,
            "role": "root",
            "launch_id": str(driver._runtime_ref),
            "capability": None,
        }
        await driver.close()
        child.settimeout(2)
        assert child.recv(1) == b""
    finally:
        child.close()
        await driver.close()


@pytest.mark.asyncio
async def test_generic_acp_spawn_cannot_inherit_a_caller_supplied_authority(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    async def spawn(*command, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(acp_driver_module.asyncio, "create_subprocess_exec", spawn)
    driver = AcpDriver()
    try:
        await driver._spawn(
            driver._validate_launch_plan(
                RuntimeSpec(
                    "/workspace",
                    executable="agent",
                    environment={DRIVER_AUTHORITY_FD_ENV: "17", "SAFE": "1"},
                )
            )
        )
        assert captured["env"] == {"SAFE": "1"}
        assert "pass_fds" not in captured
        assert driver._driver_authority is None
    finally:
        await driver.close()


@pytest.mark.parametrize("profile_arg", ["--profile=puffo-v0", "--profile=puffo-v1"])
@pytest.mark.asyncio
async def test_constrained_lingtai_rejects_spawn_paths_that_cannot_pass_fds(
    profile_arg,
) -> None:
    driver = AcpDriver(lambda _command, _spec: object())

    with pytest.raises(RuntimeError, match="POSIX local spawn path"):
        await driver._spawn(
            driver._validate_launch_plan(
                RuntimeSpec(
                    "/workspace",
                    executable="lingtai",
                    launch_args=("acp", profile_arg),
                )
            )
        )


def _root_client(server: DriverAuthorityServer, launch_id: str) -> socket.socket:
    return _client(server.issue_root(launch_id=launch_id))


def _derived_client(server: DriverAuthorityServer, root: socket.socket) -> socket.socket:
    """A claimed derived (non-root) endpoint, for the nested-denial path."""
    _hello(root)  # the root endpoint must be claimed before it can derive
    grant, fds = _request(
        root,
        {
            "version": 1,
            "op": "authorize_derived_launch",
            "call_id": str(uuid.uuid4()),
            "launch_id": _ROOT_LAUNCH_ID,
            "capability": "daemon",
        },
    )
    assert grant["state"] == "granted", grant
    assert len(fds) == 1, fds
    child = socket.socket(fileno=fds[0])
    _hello(child)
    return child


_ROOT_LAUNCH_ID = "root-echo"


def _paths_on_a_fresh_root(launch_id: str) -> list[dict[str, Any]]:
    """Requests answered on a root endpoint, in order; the first hello claims it.

    Every entry after that runs against a claimed endpoint, so this list
    reaches eight of the ten leaf response paths. The pre-claim branch and
    the nested-derived denial need different endpoint state and are covered
    by ``test_unclaimed_endpoint_response_echoes_call_id`` and
    ``test_nested_derived_launch_denial_echoes_call_id``.
    """
    return [
        {"version": 1, "op": "hello"},
        {"version": 1, "op": "hello"},  # endpoint_already_claimed
        {"version": 99, "op": "hello"},  # malformed_request
        {"version": 1, "op": "no_such_operation"},  # unsupported_operation
        {
            "version": 1,
            "op": "authorize_provider_call",
            "launch_id": launch_id,
            "provider": "llm",
            "capability": "root",
        },  # granted
        {
            "version": 1,
            "op": "authorize_provider_call",
            "launch_id": "not-the-binding",
            "provider": "llm",
            "capability": "root",
        },  # endpoint_binding_mismatch
        {
            "version": 1,
            "op": "authorize_derived_launch",
            "launch_id": launch_id,
            "capability": "daemon",
        },  # granted, carries a child descriptor
        {
            "version": 1,
            "op": "authorize_derived_launch",
            "launch_id": "not-the-binding",
            "capability": "daemon",
        },  # mismatch
    ]


def test_every_response_echoes_the_request_call_id() -> None:
    """A correlating peer rejects any response that drops its ``call_id``.

    Ten leaf response paths reach the wire. Eight are reachable on a fresh
    root endpoint; the other two need different endpoint state, so they get
    their own cases below rather than being quietly absent:
    ``test_unclaimed_endpoint_response_echoes_call_id`` (a non-hello request
    before the endpoint is claimed) and
    ``test_nested_derived_launch_denial_echoes_call_id`` (a derived endpoint
    asking to derive again).

    The guarantee itself does not come from this enumeration: the echo is
    stamped once in ``_serve`` before ``_send_frame``, so a response path
    added later is covered by construction. These cases prove that stamp is
    live on every path we can name.
    """
    server = DriverAuthorityServer()
    client = _root_client(server, _ROOT_LAUNCH_ID)
    try:
        for request in _paths_on_a_fresh_root(_ROOT_LAUNCH_ID):
            call_id = str(uuid.uuid4())
            response, fds = _request(client, {**request, "call_id": call_id})
            _close_fds(fds)
            assert response.get("call_id") == call_id, (
                f"op={request['op']} version={request['version']} "
                f"dropped call_id; response keys={sorted(response)}"
            )
    finally:
        client.close()
        server.close()


def test_unclaimed_endpoint_response_echoes_call_id() -> None:
    """A non-hello request before the endpoint is claimed is still a response."""
    server = DriverAuthorityServer()
    client = _root_client(server, "root-unclaimed")
    try:
        call_id = str(uuid.uuid4())
        response, fds = _request(
            client,
            {
                "version": 1,
                "op": "authorize_provider_call",
                "call_id": call_id,
                "launch_id": "root-unclaimed",
                "provider": "llm",
                "capability": "root",
            },
        )
        _close_fds(fds)
        assert response["reason_code"] == "endpoint_binding_mismatch"
        assert response.get("call_id") == call_id, response
    finally:
        client.close()
        server.close()


def test_nested_derived_launch_denial_echoes_call_id() -> None:
    """A derived endpoint asking to derive again is denied -- and correlated."""
    server = DriverAuthorityServer()
    root = _root_client(server, _ROOT_LAUNCH_ID)
    child = None
    try:
        child = _derived_client(server, root)
        call_id = str(uuid.uuid4())
        response, fds = _request(
            child,
            {
                "version": 1,
                "op": "authorize_derived_launch",
                "call_id": call_id,
                "launch_id": "anything",
                "capability": "daemon",
            },
        )
        _close_fds(fds)
        assert response["reason_code"] == "nested_derived_launch_denied"
        assert response.get("call_id") == call_id, response
    finally:
        if child is not None:
            child.close()
        root.close()
        server.close()


def test_response_omits_call_id_when_the_request_did_not_correlate() -> None:
    """Requests that send no ``call_id`` keep their existing response shape.

    An uncorrelated request must not receive an invented or null correlation
    key; two existing tests assert hello responses by exact dict equality.
    """
    server = DriverAuthorityServer()
    client = _root_client(server, "root-uncorrelated")
    try:
        for request in _paths_on_a_fresh_root("root-uncorrelated"):
            response, fds = _request(client, request)
            _close_fds(fds)
            assert "call_id" not in response, (
                f"op={request['op']} invented a call_id the caller never sent: "
                f"{response!r}"
            )
    finally:
        client.close()
        server.close()


def test_lingtai_hello_correlation_is_accepted_end_to_end() -> None:
    """The exact exchange LingTai performs before it will bind authority."""
    server = DriverAuthorityServer()
    client = _root_client(server, "root-lingtai")
    try:
        call_id = str(uuid.uuid4())
        response, fds = _request(
            client, {"version": 1, "op": "hello", "call_id": call_id}
        )
        assert fds == []
        # LingTai's driver_authority client raises
        # DriverAuthorityTransportError unless this comparison holds.
        assert response.get("call_id") == call_id
        assert response["role"] == "root"
        assert response["launch_id"] == "root-lingtai"
    finally:
        client.close()
        server.close()
