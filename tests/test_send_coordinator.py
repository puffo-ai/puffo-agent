from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from puffo_agent.agent import send_coordinator as sc_mod
from puffo_agent.agent.global_inbox_types import BaselineAdapter
from puffo_agent.agent.send_coordinator import (
    CHANNEL_SEND_PATH,
    KEYLESS_CHANNEL_SEND_PATH,
    SemanticSendRequest,
    SendCoordinator,
)
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.http_client import HttpError
from puffo_agent.crypto.http_client import PuffoCoreHttpClient
from puffo_agent.crypto.primitives import KemKeyPair
from puffo_agent.mcp.data_client import DataNotFound
from puffo_agent.portal.workspace_layout import ensure_workspace_shared_link

from .test_puffo_core_tools import _setup, _setup_keyless


class Freshness:
    def __init__(self, baseline=0, active=None):
        self.baseline = baseline
        self.active = active
        self.lookups = []
        self.advances = []

    async def get_context_baseline_seq(self, space_id, channel_id):
        self.lookups.append(("baseline", space_id, channel_id))
        return self.baseline

    async def get_active_turn_through_seq(self, space_id, channel_id):
        self.lookups.append(("active", space_id, channel_id))
        return self.active

    async def advance_active_turn_through_seq(self, space_id, channel_id, seq):
        self.advances.append((space_id, channel_id, seq))
        self.active = seq


async def coordinator_fixture(*, baseline=0, active=None):
    cfg, http, _store = _setup()

    class Data:
        async def lookup_channel_space(self, channel_id):
            return {"ch_a": "sp_1", "ch_b": "sp_1"}.get(channel_id)

        async def get_message_by_envelope(self, _envelope_id):
            raise DataNotFound("not found")

        async def get_send_encryption(self, _slug, _root):
            return True

    data = Data()
    device = KemKeyPair.generate()
    for channel in ("ch_a", "ch_b"):
        http.responses[f"/spaces/sp_1/channels/{channel}/members"] = {
            "members": [{"slug": "alice-1"}],
        }
    http.responses["/certs/sync?slugs=alice-1"] = {
        "entries": [{
            "seq": 1,
            "kind": "device_cert",
            "cert": {
                "device_id": "dev_a",
                "kem_public_key": base64url_encode(device.public_key_bytes()),
            },
        }],
        "has_more": False,
    }
    freshness = Freshness(baseline, active)
    coordinator = SendCoordinator(
        slug=cfg.slug,
        keystore=cfg.keystore,
        http_client=http,
        data_client=data,
        baseline_source=freshness,
        active_turn_source=freshness,
    )
    return coordinator, freshness, http


def test_attachment_validation_allows_only_managed_shared_link(tmp_path):
    workspace = tmp_path / "agents" / "alice" / "workspace"
    shared = tmp_path / "shared"
    ensure_workspace_shared_link(workspace, shared)
    (shared / "evidence.txt").write_text("shared evidence", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    coordinator = SendCoordinator(
        slug="alice",
        keystore=SimpleNamespace(),
        http_client=SimpleNamespace(),
        data_client=SimpleNamespace(),
        workspace=str(workspace),
        shared_workspace=str(shared),
    )
    request = SemanticSendRequest(
        destination="ch_test",
        text="evidence",
        attachment_paths=("shared/evidence.txt",),
    )
    assert coordinator._validate_attachment_targets(request) == [
        (shared / "evidence.txt").resolve()
    ]

    escaped = SemanticSendRequest(
        destination="ch_test",
        text="secret",
        attachment_paths=("escape/secret.txt",),
    )
    with pytest.raises(RuntimeError, match="escapes the workspace"):
        coordinator._validate_attachment_targets(escaped)


@pytest.mark.asyncio
async def test_initial_send_anyway_fails_before_request_shape_is_built():
    coordinator, _, http = await coordinator_fixture(baseline=4, active=6)
    result = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="hello", visibility_level="human",
        send_anyway=True,
    ))
    assert result["state"] == "failed"
    assert result["error_kind"] == "reconsideration_ineligible"
    assert not [call for call in http.calls if call[0] == "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline", [-1, "2", True])
async def test_baseline_invalid_fails_without_send(baseline):
    coordinator, _, http = await coordinator_fixture(baseline=baseline)
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="hello"),
    )
    assert result["state"] == "failed"
    assert result["attempted"] is True
    assert not [call for call in http.calls if call[0].startswith("POST")]


async def _baseline_lifecycle_coordinator(lane):
    if lane == "keyless":
        cfg, http, store = _setup_keyless()
    else:
        cfg, http, store = _setup()
    await store.mark_channel_space("ch_a", "sp_1")
    device = KemKeyPair.generate()
    http.responses["/spaces/sp_1/channels/ch_a/members"] = {
        "members": [{"slug": "alice-1"}],
    }
    http.responses["/certs/sync?slugs=alice-1"] = {
        "entries": [{
            "seq": 1,
            "kind": "device_cert",
            "cert": {
                "device_id": "dev_a",
                "kem_public_key": base64url_encode(device.public_key_bytes()),
            },
        }],
        "has_more": False,
    }
    active = Freshness(active=None)
    coordinator = SendCoordinator(
        slug=cfg.slug,
        keystore=cfg.keystore,
        http_client=http,
        data_client=store,
        baseline_source=BaselineAdapter(store),
        active_turn_source=active,
    )
    return coordinator, active, http, store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lane", "replay"),
    [
        ("native", False),
        ("native", True),
        ("keyless", False),
        ("keyless", True),
    ],
    ids=["native-new", "native-replay", "keyless-new", "keyless-replay"],
)
async def test_null_baseline_lifecycle_preserves_none_and_persists_established(
    lane, replay,
):
    coordinator, active, http, store = await _baseline_lifecycle_coordinator(lane)
    bodies = []
    sent_once = {"done": False}
    send_path = CHANNEL_SEND_PATH if lane == "native" else KEYLESS_CHANNEL_SEND_PATH

    async def commit(_path, body=None):
        bodies.append((_path, body))
        envelope_id = (
            body["envelope"]["envelope_id"] if lane == "native" else "msg_keyless"
        )
        if not sent_once["done"]:
            sent_once["done"] = True
            return {
                "state": "held",
                "envelope_id": envelope_id,
                "context_baseline_seq": body["freshness"]["context_baseline_seq"],
                "seen_seq": body["freshness"]["seen_seq"],
                "latest_seq": 3,
                "latest_envelope_id": "msg_latest",
            }
        return {
            "state": "sent",
            "envelope_id": envelope_id,
            "seq": 7,
            "replay": replay,
            "missing_devices": [],
            "freshness": {
                "mode": "require_current",
                "context_baseline_seq": 5,
                "seen_seq": 0,
                "latest_seq_before_send": 5,
            },
        }

    if lane == "native":
        http.post = commit
    else:
        http.post_unsigned = commit

    held = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="probe",
    ))
    assert held["state"] == "held", held
    null_body = [body for path, body in bodies if path == send_path][0]
    assert null_body["freshness"]["context_baseline_seq"] is None
    assert null_body["freshness"]["seen_seq"] == 0
    assert await store.get_context_baseline("sp_1", "ch_a") is None

    sent = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="establish",
    ))
    assert sent["state"] == "sent", sent
    assert sent["context_baseline_seq"] == 5
    assert sent["seen_seq"] == 0
    assert active.active == 7
    established_body = [body for path, body in bodies if path == send_path][-1]
    assert established_body["freshness"]["context_baseline_seq"] is None
    assert established_body["freshness"]["seen_seq"] == 0
    assert await store.get_context_baseline("sp_1", "ch_a") == 5

    rebuilt = SendCoordinator(
        slug=coordinator.slug,
        keystore=coordinator.keystore,
        http_client=http,
        data_client=store,
        baseline_source=BaselineAdapter(store),
        active_turn_source=Freshness(active=5),
    )
    boundary = await rebuilt._resolve_send_boundary(
        SemanticSendRequest(destination="ch_a", text="later"),
        "sp_1",
        "ch_a",
        combined_error=True,
    )
    assert boundary.baseline == 5
    assert boundary.seen_seq == 5
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["native", "keyless"])
async def test_committed_send_reports_baseline_persistence_failure(lane):
    coordinator, _active, http, store = await _baseline_lifecycle_coordinator(lane)

    class FailingBaseline:
        def __init__(self):
            self.calls = 0

        async def get_context_baseline_seq(self, _space_id, _channel_id):
            return None

        async def set_context_baseline_seq(self, _space_id, _channel_id, _seq):
            self.calls += 1
            raise RuntimeError("sqlite unavailable")

    baseline = FailingBaseline()
    coordinator.baseline_source = baseline

    async def commit(_path, body=None):
        envelope_id = (
            body["envelope"]["envelope_id"] if lane == "native" else "msg_keyless"
        )
        return {
            "state": "sent",
            "envelope_id": envelope_id,
            "seq": 7,
            "replay": False,
            "missing_devices": [],
            "freshness": {
                "mode": "require_current",
                "context_baseline_seq": 5,
                "seen_seq": 0,
                "latest_seq_before_send": 5,
            },
        }

    if lane == "native":
        http.post = commit
    else:
        http.post_unsigned = commit

    result = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="already committed",
    ))

    assert result["state"] == "sent"
    assert "local freshness baseline could not be saved" in result["note"]
    assert baseline.calls == 2
    await store.close()


@pytest.mark.asyncio
async def test_committed_send_stays_sent_when_local_boundary_update_fails():
    coordinator, _freshness, http = await coordinator_fixture(
        baseline=5, active=5
    )

    async def fail_advance(*_args):
        raise RuntimeError("local sqlite unavailable")

    coordinator._advance = fail_advance
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="already committed")
    )

    assert result["state"] == "sent"
    assert len([
        call for call in http.calls
        if call[0] == "POST" and call[1] == CHANNEL_SEND_PATH
    ]) == 1


@pytest.mark.asyncio
async def test_boundary_multiple_shared_channel_and_independent_channels():
    coordinator, freshness, http = await coordinator_fixture(baseline=0)
    first = await coordinator.send(SemanticSendRequest(destination="ch_a", text="one"))
    second = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="two", root_id="unknown",
    ))
    assert first["state"] == second["state"] == "sent"
    bodies = [
        body for method, path, body in http.calls
        if method == "POST" and path == CHANNEL_SEND_PATH
    ]
    assert bodies[0]["freshness"]["seen_seq"] == 0
    assert bodies[1]["freshness"]["seen_seq"] == first["seq"]
    freshness.active = None
    await coordinator.send(SemanticSendRequest(destination="ch_b", text="other"))
    channel_bodies = [
        body for method, path, body in http.calls
        if method == "POST" and path == CHANNEL_SEND_PATH
    ]
    assert channel_bodies[-1]["freshness"]["seen_seq"] == 0
    assert ("sp_1", "ch_a", first["seq"]) in freshness.advances


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {},
    {"state": "wat"},
    {"state": "sent", "envelope_id": "wrong", "seq": 1, "replay": False},
    {"state": "sent", "seq": "1", "replay": False},
    {"state": "held", "seen_seq": 0},
])
async def test_response_validation_failed_attempted(bad):
    coordinator, _, http = await coordinator_fixture()
    http.responses[CHANNEL_SEND_PATH] = bad
    result = await coordinator.send(SemanticSendRequest(destination="ch_a", text="x"))
    assert result["state"] == "failed"
    assert result["attempted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 409, 413, 429, 500, 503])
async def test_status_matrix_and_deployment_error(status):
    coordinator, _, http = await coordinator_fixture()

    async def post(path, body):
        http.calls.append(("POST", path, body))
        raise HttpError(status, '{"error":"NOPE","message":"not accepted"}')

    http.post = post
    result = await coordinator.send(SemanticSendRequest(destination="ch_a", text="x"))
    assert result["state"] == "failed"
    assert result["status"] == status
    assert result["error_kind"] == ("deployment" if status in (404, 405) else "http")
    assert all(call[1] != "/messages" for call in http.calls if call[0] == "POST")


@pytest.mark.asyncio
async def test_lost_response_replay_reuses_exact_body():
    coordinator, _, http = await coordinator_fixture(baseline=8)
    bodies = []

    async def post(path, body):
        bodies.append(copy.deepcopy(body))
        if len(bodies) == 1:
            raise TimeoutError("response lost")
        return {
            "state": "sent",
            "envelope_id": body["envelope"]["envelope_id"],
            "seq": 11,
            "replay": True,
            "missing_devices": [],
            "freshness": {
                "mode": "require_current",
                "context_baseline_seq": 8,
                "seen_seq": 8,
                "latest_seq_before_send": 8,
            },
        }

    http.post = post
    result = await coordinator.send(SemanticSendRequest(destination="ch_a", text="x"))
    assert result["state"] == "sent" and result["replay"] is True
    assert bodies[0] == bodies[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "replay", "latest_before", "legacy"),
    [
        ("require_current", False, 4, False),
        ("send_anyway", False, 9, False),
        ("require_current", True, 4, False),
        ("require_current", True, None, True),
    ],
    ids=["strict", "send-anyway", "stored-metadata-replay", "legacy-replay"],
)
async def test_native_committed_response_accepts_frozen_server_variants(
    mode, replay, latest_before, legacy,
):
    coordinator, _, _ = await coordinator_fixture()
    envelope = {"envelope_id": "msg_exact"}
    request_freshness = {
        "mode": mode,
        "context_baseline_seq": 3,
        "seen_seq": 4,
    }
    response = {
        "state": "sent",
        "envelope_id": "msg_exact",
        "seq": 10,
        "replay": replay,
        "missing_devices": ["dev_missing"],
    }
    if not legacy:
        response["freshness"] = {
            **request_freshness,
            "latest_seq_before_send": latest_before,
        }
    result = coordinator._validate_channel_response(
        response, envelope, request_freshness
    )
    assert result.state == "sent"
    assert result.envelope_id == "msg_exact"
    assert result.seq == 10
    assert result.replay is replay
    assert result.missing_devices == ["dev_missing"]
    assert result.latest_seq_before_send == latest_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing-mode",
        "missing-baseline",
        "missing-seen",
        "missing-latest",
        "extra-freshness",
        "mode-mismatch",
        "baseline-mismatch",
        "seen-mismatch",
        "freshness-null",
        "freshness-nonmapping",
        "baseline-bool",
        "seen-bool",
        "latest-bool",
        "baseline-malformed",
        "seen-malformed",
        "latest-malformed",
        "latest-below-boundary",
        "require-current-not-seen",
        "seq-zero",
        "seq-negative",
        "seq-bool",
        "replay-missing",
        "replay-nonbool",
        "missing-devices-missing",
        "missing-devices-nonlist",
        "missing-devices-nonstring",
    ],
)
async def test_native_committed_response_rejects_adversarial_matrix(case):
    coordinator, _, _ = await coordinator_fixture()
    envelope = {"envelope_id": "msg_exact"}
    request_freshness = {
        "mode": "require_current",
        "context_baseline_seq": 3,
        "seen_seq": 4,
    }
    response = {
        "state": "sent",
        "envelope_id": "msg_exact",
        "seq": 5,
        "replay": False,
        "missing_devices": [],
        "freshness": {
            **request_freshness,
            "latest_seq_before_send": 4,
        },
    }
    if case.startswith("missing-") and case in {
        "missing-mode", "missing-baseline", "missing-seen", "missing-latest",
    }:
        key = {
            "missing-mode": "mode",
            "missing-baseline": "context_baseline_seq",
            "missing-seen": "seen_seq",
            "missing-latest": "latest_seq_before_send",
        }[case]
        response["freshness"].pop(key)
    elif case == "extra-freshness":
        response["freshness"]["extra"] = 1
    elif case == "mode-mismatch":
        response["freshness"]["mode"] = "send_anyway"
    elif case == "baseline-mismatch":
        response["freshness"]["context_baseline_seq"] = 2
    elif case == "seen-mismatch":
        response["freshness"]["seen_seq"] = 3
    elif case == "freshness-null":
        response["freshness"] = None
        response["replay"] = True
    elif case == "freshness-nonmapping":
        response["freshness"] = "malformed"
    elif case == "baseline-bool":
        response["freshness"]["context_baseline_seq"] = True
    elif case == "seen-bool":
        response["freshness"]["seen_seq"] = True
    elif case == "latest-bool":
        response["freshness"]["latest_seq_before_send"] = True
    elif case == "baseline-malformed":
        response["freshness"]["context_baseline_seq"] = "3"
    elif case == "seen-malformed":
        response["freshness"]["seen_seq"] = "4"
    elif case == "latest-malformed":
        response["freshness"]["latest_seq_before_send"] = "4"
    elif case == "latest-below-boundary":
        response["freshness"]["latest_seq_before_send"] = 3
    elif case == "require-current-not-seen":
        response["freshness"]["latest_seq_before_send"] = 5
    elif case == "seq-zero":
        response["seq"] = 0
    elif case == "seq-negative":
        response["seq"] = -1
    elif case == "seq-bool":
        response["seq"] = True
    elif case == "replay-missing":
        response.pop("replay")
    elif case == "replay-nonbool":
        response["replay"] = "false"
    elif case == "missing-devices-missing":
        response.pop("missing_devices")
    elif case == "missing-devices-nonlist":
        response["missing_devices"] = "dev"
    elif case == "missing-devices-nonstring":
        response["missing_devices"] = [1]
    result = coordinator._validate_channel_response(
        response, envelope, request_freshness
    )
    assert result.state == "failed"
    assert result.error_kind == "protocol"


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope_id", ["", " ", None])
async def test_native_response_requires_valid_nonempty_request_envelope_id(
    envelope_id,
):
    coordinator, _, _ = await coordinator_fixture()
    result = coordinator._validate_channel_response(
        {
            "state": "sent",
            "envelope_id": envelope_id,
            "seq": 5,
            "replay": False,
            "missing_devices": [],
            "freshness": {
                "mode": "require_current",
                "context_baseline_seq": 3,
                "seen_seq": 4,
                "latest_seq_before_send": 4,
            },
        },
        {"envelope_id": envelope_id},
        {
            "mode": "require_current",
            "context_baseline_seq": 3,
            "seen_seq": 4,
        },
    )
    assert result.state == "failed"
    assert result.error_kind == "protocol"


@pytest.mark.asyncio
async def test_unknown_native_response_state_does_not_echo_content():
    coordinator, _, _ = await coordinator_fixture()
    sentinel = "UNKNOWN_STATE_CONTENT_SENTINEL"
    result = coordinator._validate_channel_response(
        {"state": sentinel},
        {"envelope_id": "msg_exact"},
        {
            "mode": "require_current",
            "context_baseline_seq": 3,
            "seen_seq": 4,
        },
    )
    assert result.state == "failed"
    assert result.error_kind == "protocol"
    assert sentinel not in (result.error or "")


@pytest.mark.asyncio
async def test_native_committed_response_rejects_absent_freshness_without_replay():
    coordinator, _, _ = await coordinator_fixture()
    result = coordinator._validate_channel_response(
        {
            "state": "sent",
            "envelope_id": "msg_exact",
            "seq": 5,
            "replay": False,
            "missing_devices": [],
        },
        {"envelope_id": "msg_exact"},
        {
            "mode": "require_current",
            "context_baseline_seq": 3,
            "seen_seq": 4,
        },
    )
    assert result.state == "failed"
    assert result.error_kind == "protocol"


@pytest.mark.asyncio
async def test_native_held_response_accepts_exact_frozen_contract():
    coordinator, _, _ = await coordinator_fixture()
    result = coordinator._validate_channel_response(
        {
            "state": "held",
            "envelope_id": "msg_exact",
            "context_baseline_seq": 3,
            "seen_seq": 4,
            "latest_seq": 5,
            "latest_envelope_id": "msg_latest",
        },
        {"envelope_id": "msg_exact"},
        {
            "mode": "require_current",
            "context_baseline_seq": 3,
            "seen_seq": 4,
        },
    )
    assert result.state == "held"
    assert result.latest_seq == 5
    assert result.latest_envelope_id == "msg_latest"


@pytest.mark.asyncio
async def test_native_held_response_accepts_current_blocker_metadata_contract():
    coordinator, _, _ = await coordinator_fixture()
    response = {
        "state": "held",
        "envelope_id": "msg_exact",
        "context_baseline_seq": 3,
        "seen_seq": 4,
        "latest_seq": 5,
        "latest_envelope_id": "msg_latest",
        "blocking_seq": 5,
        "blocking_envelope_id": "msg_latest",
        "blocking_sender_slug": "agent-peer",
    }
    freshness = {
        "mode": "require_current",
        "context_baseline_seq": 3,
        "seen_seq": 4,
    }

    result = coordinator._validate_channel_response(
        response, {"envelope_id": "msg_exact"}, freshness,
    )

    assert result.state == "held"
    assert result.blocking_seq == 5
    assert result.blocking_envelope_id == "msg_latest"
    assert result.blocking_sender_slug == "agent-peer"

    response["blocking_seq"] = 6
    rejected = coordinator._validate_channel_response(
        response, {"envelope_id": "msg_exact"}, freshness,
    )
    assert rejected.state == "failed"
    assert rejected.error_kind == "protocol"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing-state",
        "missing-envelope",
        "missing-baseline",
        "missing-seen",
        "missing-latest",
        "missing-latest-envelope",
        "extra-field",
        "envelope-mismatch",
        "baseline-mismatch",
        "seen-mismatch",
        "baseline-bool",
        "seen-bool",
        "latest-bool",
        "baseline-malformed",
        "seen-malformed",
        "latest-malformed",
        "latest-equal-boundary",
        "latest-below-boundary",
        "latest-envelope-empty",
        "latest-envelope-whitespace",
        "latest-envelope-nonstring",
    ],
)
async def test_native_held_response_rejects_adversarial_matrix_before_recording(
    case,
):
    coordinator, _, http = await coordinator_fixture(baseline=3, active=4)

    async def post(_path, body):
        response = {
            "state": "held",
            "envelope_id": body["envelope"]["envelope_id"],
            "context_baseline_seq": 3,
            "seen_seq": 4,
            "latest_seq": 5,
            "latest_envelope_id": "msg_latest",
        }
        missing = {
            "missing-state": "state",
            "missing-envelope": "envelope_id",
            "missing-baseline": "context_baseline_seq",
            "missing-seen": "seen_seq",
            "missing-latest": "latest_seq",
            "missing-latest-envelope": "latest_envelope_id",
        }
        if case in missing:
            response.pop(missing[case])
        elif case == "extra-field":
            response["extra"] = 1
        elif case == "envelope-mismatch":
            response["envelope_id"] = "msg_wrong"
        elif case == "baseline-mismatch":
            response["context_baseline_seq"] = 2
        elif case == "seen-mismatch":
            response["seen_seq"] = 3
        elif case == "baseline-bool":
            response["context_baseline_seq"] = True
        elif case == "seen-bool":
            response["seen_seq"] = True
        elif case == "latest-bool":
            response["latest_seq"] = True
        elif case == "baseline-malformed":
            response["context_baseline_seq"] = "3"
        elif case == "seen-malformed":
            response["seen_seq"] = "4"
        elif case == "latest-malformed":
            response["latest_seq"] = "5"
        elif case == "latest-equal-boundary":
            response["latest_seq"] = 4
        elif case == "latest-below-boundary":
            response["latest_seq"] = 3
        elif case == "latest-envelope-empty":
            response["latest_envelope_id"] = ""
        elif case == "latest-envelope-whitespace":
            response["latest_envelope_id"] = " "
        elif case == "latest-envelope-nonstring":
            response["latest_envelope_id"] = 7
        return response

    http.post = post
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_a", text="draft")
    )
    assert result["state"] == "failed"
    assert result["error_kind"] == "protocol"
    assert coordinator._held_evidence == {}


@pytest.mark.asyncio
async def test_dm_route_has_no_freshness():
    coordinator, _, http = await coordinator_fixture()
    device = KemKeyPair.generate()
    http.responses["/certs/sync?slugs=agent-0001,alice-1"] = {
        "entries": [{
            "seq": 1, "kind": "device_cert",
            "cert": {
                "device_id": "dev_dm",
                "kem_public_key": base64url_encode(device.public_key_bytes()),
            },
        }],
        "has_more": False,
    }
    result = await coordinator.send(SemanticSendRequest(
        destination="@alice-1", text="hi", visibility_level="human",
    ))
    assert result["state"] == "sent"
    path, body = [(p, b) for m, p, b in http.calls if m == "POST"][-1]
    assert path == "/messages"
    assert "freshness" not in body


@pytest.mark.asyncio
async def test_plaintext_dm_route_has_no_freshness():
    coordinator, _, http = await coordinator_fixture()

    async def plaintext(_slug, _root):
        return False

    coordinator.data_client.get_send_encryption = plaintext
    result = await coordinator.send(SemanticSendRequest(
        destination="@alice-1", text="hi", visibility_level="human",
    ))
    assert result["state"] == "sent"
    path, body = [(p, b) for m, p, b in http.calls if m == "POST"][-1]
    assert path == "/v2/messages/plaintext"
    assert "freshness" not in body


@pytest.mark.asyncio
async def test_plaintext_dm_policy_cannot_expose_attachment_keys(tmp_path):
    """A DM attachment stays E2EE when text-only DMs permit plaintext."""
    coordinator, _, http = await coordinator_fixture()
    coordinator.workspace = str(tmp_path)
    (tmp_path / "evidence.txt").write_text("proof", encoding="utf-8")

    async def plaintext(_slug, _root):
        return False

    coordinator.data_client.get_send_encryption = plaintext
    device = KemKeyPair.generate()
    http.responses["/certs/sync?slugs=agent-0001,alice-1"] = {
        "entries": [{
            "seq": 1,
            "kind": "device_cert",
            "cert": {
                "device_id": "dev_dm_attachment",
                "kem_public_key": base64url_encode(device.public_key_bytes()),
            },
        }],
        "has_more": False,
    }
    result = await coordinator.send(SemanticSendRequest(
        destination="@alice-1",
        attachment_paths=("evidence.txt",),
        caption="evidence",
    ))
    assert result["state"] == "sent", result
    message_posts = [
        path for method, path, _body in http.calls
        if method == "POST" and path in {"/messages", "/v2/messages/plaintext"}
    ]
    assert message_posts == ["/messages"]


@pytest.mark.asyncio
async def test_channel_roster_path_encodes_model_selected_channel_segment():
    """A destination containing slashes cannot retarget the roster request."""
    coordinator, _, http = await coordinator_fixture()
    encoded = "/spaces/sp_1/channels/ch_a%2F..%2Fmembers/members"
    http.responses[encoded] = {"members": [{"slug": "alice-1"}]}
    assert await coordinator._channel_recipient_slugs(
        "sp_1", "ch_a/../members"
    ) == ["alice-1"]
    assert ("GET", encoded, None) in http.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["encrypted", "plaintext", "keyless"])
@pytest.mark.parametrize("include_metadata", [True, False])
async def test_legacy_dm_transports_preserve_optional_metadata(
    transport, include_metadata, monkeypatch,
):
    """All three legacy DM routes keep optional commit metadata optional."""
    captured = []
    original_to_dict = sc_mod.SendResult.to_dict

    def capture_before_serialization(value):
        captured.append(value)
        return original_to_dict(value)

    monkeypatch.setattr(sc_mod.SendResult, "to_dict", capture_before_serialization)
    metadata = {
        "seq": 17,
        "replay": True,
        "devices_queued": 2,
        "missing_devices": ["missing-device"],
    } if include_metadata else {}
    if transport == "keyless":
        cfg, http, store = _setup_keyless()
        http.responses["/v2/cloud-agents/messages"] = {
            "envelope_id": "keyless-dm", **metadata,
        }
        coordinator = SendCoordinator(
            slug=cfg.slug, keystore=cfg.keystore, http_client=http,
            data_client=store,
        )
    else:
        coordinator, _, http = await coordinator_fixture()
        if transport == "plaintext":
            async def plaintext(_slug, _root):
                return False
            coordinator.data_client.get_send_encryption = plaintext
        else:
            device = KemKeyPair.generate()
            http.responses["/certs/sync?slugs=agent-0001,alice-1"] = {
                "entries": [{
                    "seq": 1, "kind": "device_cert",
                    "cert": {
                        "device_id": "metadata-device",
                        "kem_public_key": base64url_encode(device.public_key_bytes()),
                    },
                }],
                "has_more": False,
            }

        async def post(_path, _body):
            return metadata
        http.post = post

    result = await coordinator.send(SemanticSendRequest(
        destination="@alice-1", text="metadata",
    ))
    assert result["state"] == "sent", result
    sent = next(value for value in reversed(captured) if value.state == "sent")
    for name, value in metadata.items():
        assert result[name] == value
        assert getattr(sent, name) == value
    for name in {"seq", "replay", "devices_queued", "missing_devices"} - set(metadata):
        assert name not in result
    if not include_metadata:
        assert sent.seq is None
        assert sent.replay is None
        assert sent.devices_queued is None
        assert sent.missing_devices == []


@pytest.mark.asyncio
async def test_plaintext_channel_no_downgrade():
    """A channel send never downgrades: even when the daemon-level send-mode
    decision says plaintext (turn bundle cleared, e.g. a turn-unbound
    background wakeup), the channel envelope still goes out encrypted."""
    coordinator, _, http = await coordinator_fixture()

    async def plaintext(_slug, _root):
        return False

    coordinator.data_client.get_send_encryption = plaintext
    result = await coordinator.send(SemanticSendRequest(
        destination="ch_a", text="must not downgrade",
    ))
    assert result["state"] == "sent", result
    posts = [
        call for call in http.calls
        if call[0] == "POST" and call[1] == CHANNEL_SEND_PATH
    ]
    assert posts
    envelope = posts[-1][2]["envelope"]
    assert envelope["type"] != "plaintext_message_envelope"


@pytest.mark.asyncio
async def test_non_json_nominal_success_is_protocol_failure():
    coordinator, _, http = await coordinator_fixture()

    async def post(_path, _body):
        raise HttpError(200, "non-JSON body on 200 response: <html>")

    http.post = post
    result = await coordinator.send(SemanticSendRequest(destination="ch_a", text="x"))
    assert result["state"] == "failed"
    assert result["error_kind"] == "protocol"


@pytest.mark.asyncio
async def test_keyless_client_ref_is_stable_per_retry_and_new_per_logical_send():
    cfg, http, store = _setup_keyless()
    await store.mark_channel_space("ch_abc", "sp_test")
    freshness = Freshness(baseline=0)
    coordinator = SendCoordinator(
        slug=cfg.slug,
        keystore=cfg.keystore,
        http_client=http,
        data_client=store,
        baseline_source=freshness,
        active_turn_source=freshness,
    )
    bodies = []

    async def post_unsigned(path, body=None):
        assert path == KEYLESS_CHANNEL_SEND_PATH
        bodies.append(copy.deepcopy(body))
        if len(bodies) == 1:
            raise TimeoutError("lost response")
        freshness = body["freshness"]
        return {
            "state": "sent",
            "envelope_id": f"keyless-{len(bodies)}",
            "seq": freshness["seen_seq"] + 1,
            "replay": len(bodies) == 2,
            "missing_devices": [],
            "freshness": {
                "mode": freshness["mode"],
                "context_baseline_seq": freshness["context_baseline_seq"],
                "seen_seq": freshness["seen_seq"],
                "latest_seq_before_send": freshness["seen_seq"],
            },
        }

    http.post_unsigned = post_unsigned
    first = await coordinator.send(
        SemanticSendRequest(destination="ch_abc", text="first")
    )
    second = await coordinator.send(
        SemanticSendRequest(destination="ch_abc", text="second")
    )
    assert first["state"] == second["state"] == "sent"
    assert bodies[0] == bodies[1]
    assert bodies[0]["client_ref"]
    assert bodies[0]["client_ref"] == bodies[1]["client_ref"]
    assert bodies[2]["client_ref"] != bodies[0]["client_ref"]
    assert all("client_request_id" not in body for body in bodies)
    await store.close()


def _keyless_dto_handlers(captured):
    async def upload(_request):
        return web.json_response({"blob_id": "blob_1"})

    async def send(request):
        raw = await request.read()
        body = json.loads(raw)
        allowed = {
            "client_ref", "space_id", "channel_id", "plaintext",
            "is_visible_to_human", "freshness", "thread_root_id",
            "reply_to_id", "attachments",
        }
        if set(body) - allowed or set(body.get("freshness", {})) != {
            "context_baseline_seq", "seen_seq", "mode",
        }:
            return web.json_response({"error": "unknown_field"}, status=400)
        if not {
            "client_ref", "space_id", "channel_id", "plaintext", "freshness",
        } <= set(body):
            return web.json_response({"error": "missing_field"}, status=400)
        if (
            not all(isinstance(body[name], str) for name in (
                "client_ref", "space_id", "channel_id", "plaintext",
                "thread_root_id", "reply_to_id",
            ))
            or isinstance(body["freshness"]["context_baseline_seq"], bool)
            or not isinstance(body["freshness"]["context_baseline_seq"], int)
            or isinstance(body["freshness"]["seen_seq"], bool)
            or not isinstance(body["freshness"]["seen_seq"], int)
            or body["freshness"]["mode"] not in {
                "require_current", "send_anyway",
            }
        ):
            return web.json_response({"error": "bad_type"}, status=400)
        if (
            not isinstance(body["attachments"], list)
            or len(body["attachments"]) != 1
            or set(body["attachments"][0]) != {
            "blob_id", "filename", "mime_type", "size_bytes",
            }
            or not all(
                isinstance(body["attachments"][0][name], str)
                for name in ("blob_id", "filename", "mime_type")
            )
            or isinstance(body["attachments"][0]["size_bytes"], bool)
            or not isinstance(body["attachments"][0]["size_bytes"], int)
        ):
            return web.json_response({"error": "bad_attachment"}, status=400)
        captured.append(raw)
        return web.json_response({
            "state": "sent",
            "envelope_id": "msg_sent",
            "seq": 1,
            "replay": False,
            "missing_devices": ["device_missing"],
            "freshness": {
                "context_baseline_seq": 0,
                "seen_seq": 0,
                "mode": "require_current",
                "latest_seq_before_send": 0,
            },
        })
    return upload, send


async def _start_keyless_dto_server(captured):
    upload, send = _keyless_dto_handlers(captured)

    app = web.Application()
    app.router.add_post("/v2/cloud-agents/blobs/upload", upload)
    app.router.add_post(KEYLESS_CHANNEL_SEND_PATH, send)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets
    base = f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
    return runner, base


def _expected_keyless_dto():
    return {
        "client_ref": "send_" + ("0" * 32),
        "space_id": "sp_test", "channel_id": "ch_abc",
        "plaintext": "caption", "is_visible_to_human": True,
        "freshness": {
            "context_baseline_seq": 0, "seen_seq": 0,
            "mode": "require_current",
        },
        "thread_root_id": "msg_root", "reply_to_id": "msg_root",
        "attachments": [{
            "blob_id": "blob_1", "filename": "evidence.txt",
            "mime_type": "text/plain", "size_bytes": 5,
        }],
    }


async def _assert_keyless_dto_response(http, captured, result):
    expected = _expected_keyless_dto()
    assert captured == [json.dumps(expected).encode()]
    assert result["seq"] == 1
    assert result["replay"] is False
    assert result["missing_devices"] == ["device_missing"]
    assert result["mode"] == "require_current"
    assert result["context_baseline_seq"] == 0
    assert result["seen_seq"] == 0
    assert result["latest_seq_before_send"] == 0
    for bad in (
        {**expected, "visibility_level": "human"},
        {**expected, "freshness": {**expected["freshness"], "unknown": 1}},
    ):
        with pytest.raises(HttpError) as error:
            await http.post_unsigned(KEYLESS_CHANNEL_SEND_PATH, bad)
        assert error.value.status == 400


@pytest.mark.asyncio
async def test_keyless_real_http_serializes_exact_server_dto(
    tmp_path, monkeypatch,
):
    cfg, _fake_http, store = _setup_keyless()
    await store.mark_channel_space("ch_abc", "sp_test")
    await store.store({
        "envelope_id": "msg_root", "envelope_kind": "channel",
        "sender_slug": "alice-0001", "channel_id": "ch_abc",
        "space_id": "sp_test", "content": "root", "sent_at": 1,
    })
    (tmp_path / "evidence.txt").write_text("proof", encoding="utf-8")
    monkeypatch.setattr(
        sc_mod.uuid, "uuid4", lambda: SimpleNamespace(hex="0" * 32),
    )
    captured: list[bytes] = []
    runner, base = await _start_keyless_dto_server(captured)
    http = PuffoCoreHttpClient(base, cfg.keystore, cfg.slug, keyless=True)
    coordinator = SendCoordinator(
        slug=cfg.slug,
        keystore=cfg.keystore,
        http_client=http,
        data_client=store,
        workspace=str(tmp_path),
        baseline_source=Freshness(0),
        active_turn_source=Freshness(0),
    )
    try:
        result = await coordinator.send(SemanticSendRequest(
            destination="ch_abc",
            attachment_paths=("evidence.txt",),
            caption="caption",
            root_id="msg_root",
            visibility_level="human",
        ))
        await _assert_keyless_dto_response(http, captured, result)
    finally:
        await http.close()
        await runner.cleanup()
        await store.close()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda echo: echo.pop("context_baseline_seq"),
        lambda echo: echo.__setitem__("context_baseline_seq", 99),
        lambda echo: echo.pop("mode"),
        lambda echo: echo.__setitem__("seen_seq", 99),
        lambda echo: echo.pop("latest_seq_before_send"),
    ],
)
def test_keyless_sent_requires_complete_exact_freshness_echo(mutation):
    cfg, http, store = _setup_keyless()
    coordinator = SendCoordinator(
        slug=cfg.slug,
        keystore=cfg.keystore,
        http_client=http,
        data_client=store,
    )
    request = {
        "freshness": {
            "mode": "require_current",
            "context_baseline_seq": 3,
            "seen_seq": 4,
        }
    }
    response = {
        "state": "sent",
        "envelope_id": "msg-1",
        "seq": 5,
        "replay": False,
        "missing_devices": [],
        "freshness": {
            **request["freshness"],
            "latest_seq_before_send": 4,
        },
    }
    mutation(response["freshness"])
    result = coordinator._validate_keyless_response(response, request)
    assert result.state == "failed"
    assert result.error_kind == "protocol"


@pytest.mark.asyncio
async def test_keyless_coordinated_endpoint_unavailable_fails_closed():
    cfg, http, store = _setup_keyless()
    await store.mark_channel_space("ch_abc", "sp_test")
    freshness = Freshness(baseline=0)
    coordinator = SendCoordinator(
        slug=cfg.slug,
        keystore=cfg.keystore,
        http_client=http,
        data_client=store,
        baseline_source=freshness,
        active_turn_source=freshness,
    )
    calls = []

    async def post_unsigned(path, body=None):
        calls.append((path, body))
        raise HttpError(404, '{"error":"not deployed"}')

    http.post_unsigned = post_unsigned
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_abc", text="do not bypass")
    )
    assert result["state"] == "failed"
    assert result["error_kind"] == "freshness_unavailable"
    assert [path for path, _body in calls] == [KEYLESS_CHANNEL_SEND_PATH]
    assert calls[0][1]["client_ref"]
    assert "client_request_id" not in calls[0][1]
    await store.close()


@pytest.mark.asyncio
async def test_keyless_held_result_is_structured_and_content_free():
    cfg, http, store = _setup_keyless()
    await store.mark_channel_space("ch_abc", "sp_test")
    freshness = Freshness(baseline=2)
    coordinator = SendCoordinator(
        slug=cfg.slug,
        keystore=cfg.keystore,
        http_client=http,
        data_client=store,
        baseline_source=freshness,
        active_turn_source=freshness,
    )

    async def post_unsigned(path, body=None):
        assert path == KEYLESS_CHANNEL_SEND_PATH
        return {
            "state": "held",
            "seen_seq": body["freshness"]["seen_seq"],
            "context_baseline_seq": body["freshness"]["context_baseline_seq"],
            "latest_seq": 5,
            "latest_envelope_id": "newest",
            "recovered_messages": [{"content": "must not escape"}],
        }

    http.post_unsigned = post_unsigned
    result = await coordinator.send(
        SemanticSendRequest(destination="ch_abc", text="draft")
    )
    assert result["state"] == "held"
    assert result["seen_seq"] == 2
    assert result["latest_seq"] == 5
    assert result["latest_envelope_id"] == "newest"
    assert result["synchronized"] is False
    assert "recovered_messages" not in result
    assert "must not escape" not in str(result)
    await store.close()


@pytest.mark.asyncio
async def test_keyless_unsynchronized_held_stays_blocked_after_admission():
    cfg, http, store = _setup_keyless()
    await store.mark_channel_space("ch_abc", "sp_test")
    freshness = Freshness(baseline=2)
    freshness.active = SimpleNamespace(
        turn_id="turn-a", provider_session_id="session-a"
    )

    async def boundary(_space_id, _channel_id):
        return boundary.value

    boundary.value = 2
    freshness.get_active_turn_through_seq = boundary

    class UnavailableRecovery:
        async def wait_for_held_delivery(self, *_args):
            return False

    coordinator = SendCoordinator(
        slug=cfg.slug,
        keystore=cfg.keystore,
        http_client=http,
        data_client=store,
        baseline_source=freshness,
        active_turn_source=freshness,
        held_recovery_source=UnavailableRecovery(),
        provider_session_id="session-a",
    )
    coordinated = []

    async def post_unsigned(path, body=None):
        coordinated.append((path, copy.deepcopy(body)))
        return {
            "state": "held",
            "seen_seq": body["freshness"]["seen_seq"],
            "context_baseline_seq": body["freshness"]["context_baseline_seq"],
            "latest_seq": 5,
            "latest_envelope_id": "newest",
        }

    http.post_unsigned = post_unsigned
    first = await coordinator.send(SemanticSendRequest(
        destination="ch_abc", text="draft"
    ))
    assert first["state"] == "held"
    assert first["synchronized"] is False
    boundary.value = 5
    blocked = await coordinator.send(SemanticSendRequest(
        destination="ch_abc", text="override", send_anyway=True
    ))
    assert blocked["state"] == "failed"
    assert blocked["error_kind"] == "reconsideration_ineligible"
    assert [path for path, _body in coordinated] == [
        KEYLESS_CHANNEL_SEND_PATH
    ]
    assert not [
        call for call in http.calls
        if call[0].startswith("POST") or "upload" in call[1]
    ]
    await store.close()
