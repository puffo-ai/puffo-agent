"""Keyless read-route rewriting for cloud (bridge) agents.

A keyless bridge agent has no subkey, so signed read routes are transparently
rewritten to their ``/v2/cloud-agents/*`` twins (server-side SandboxTokenAuth).
The matcher must rewrite exactly the routes that HAVE a twin and leave every
other path — most importantly the events routes, which have no twin — alone.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from puffo_agent.crypto.http_client import cloud_agent_read_twin


@pytest.mark.parametrize(
    "path,expected",
    [
        # ── routes WITH a /v2/cloud-agents twin → rewritten ──
        ("/spaces", "/v2/cloud-agents/spaces"),
        ("/spaces/sp_123/channels", "/v2/cloud-agents/spaces/sp_123/channels"),
        ("/spaces/sp_123/members", "/v2/cloud-agents/spaces/sp_123/members"),
        (
            "/spaces/sp_123/channels/ch_456/members",
            "/v2/cloud-agents/spaces/sp_123/channels/ch_456/members",
        ),
        ("/identities/profiles", "/v2/cloud-agents/identities/profiles"),
        # query string preserved
        (
            "/identities/profiles?slugs=alice-0001,bob-0002",
            "/v2/cloud-agents/identities/profiles?slugs=alice-0001,bob-0002",
        ),
        # ── routes WITHOUT a twin → left alone (None) ──
        ("/spaces/sp_123/events", None),   # per-space event log — no twin
        ("/spaces/events", None),          # global event stream — no twin
        ("/spaces/sp_123", None),          # a bare space id is not a read route
        ("/certs/sync?slugs=x", None),
        ("/devices/subkeys", None),
        ("/cloud-agents/messages", None),  # already a keyless send route, not a read
        # never double-prefix an already-rewritten path
        ("/v2/cloud-agents/spaces", None),
    ],
)
def test_cloud_agent_read_twin(path, expected):
    assert cloud_agent_read_twin(path) == expected


def test_events_routes_are_not_swept_up_by_the_spaces_matcher():
    """Regression guard: the `/spaces/{id}/...` patterns are anchored so the
    events routes (which look similar but have no twin) never match."""
    assert cloud_agent_read_twin("/spaces/sp_abc/events") is None
    assert cloud_agent_read_twin("/spaces/events") is None
    # but a real twinned route under the same space id still rewrites
    assert (
        cloud_agent_read_twin("/spaces/sp_abc/members")
        == "/v2/cloud-agents/spaces/sp_abc/members"
    )
