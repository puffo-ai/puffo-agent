"""Per-daemon attach registry for ws-local agents."""

from __future__ import annotations

from puffo_agent.portal.ws_local.hub import AttachPoint, WsLocalHub


def _point(slug: str) -> AttachPoint:
    return AttachPoint(
        slug=slug, agent_id=f"{slug}-1", agent_cfg=object(),
        client=object(), reporter=object(),
        ack_timeout_s=180.0, ping_interval_s=30.0,
    )


def test_register_makes_servable():
    hub = WsLocalHub()
    assert hub.is_servable("alice") is False
    p = _point("alice")
    hub.register(p)
    assert hub.is_servable("alice") is True
    assert hub.get("alice") is p


def test_unregister_removes():
    hub = WsLocalHub()
    p = _point("alice")
    hub.register(p)
    hub.unregister(p)
    assert hub.is_servable("alice") is False
    assert hub.get("alice") is None


def test_unregister_by_stale_point_is_noop():
    """A replaced point must not let the old Worker's teardown evict the
    live registration."""
    hub = WsLocalHub()
    old = _point("alice")
    new = _point("alice")
    hub.register(old)
    hub.register(new)  # re-registration replaces
    hub.unregister(old)  # stale teardown
    assert hub.get("alice") is new


def test_registry_is_shared_instance():
    hub = WsLocalHub()
    assert hub.registry is hub.registry
    assert hub.registry.active_count() == 0
