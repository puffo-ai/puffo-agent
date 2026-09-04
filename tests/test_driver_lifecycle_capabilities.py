"""Lifecycle and busy-delivery capability contracts."""

from puffo_agent.agent.harness.drivers.claude_code import claude_capabilities
from puffo_agent.agent.harness.drivers.codex import CODEX_CAPABILITIES
from puffo_agent.agent.harness.driver import (
    BusyDelivery,
    CancelCapability,
    CompactCapability,
    ContextStatusCapability,
    DriverCapabilities,
    ProtocolDiagnostics,
    RuntimeOpened,
    RuntimeRef,
    RuntimeLifecycle,
    SessionRef,
    SteerCapability,
)
from puffo_agent.agent.harness.runtime.runtime_manager import RuntimeManagerAdapter


def _shipped_capabilities():
    return {
        "codex": CODEX_CAPABILITIES,
        "claude-code": claude_capabilities(),
        "claude-code+msg_lifecycle_v1": claude_capabilities(
            message_lifecycle_v1=True
        ),
    }


def test_shipped_drivers_declare_lifecycle_and_busy_delivery():
    for name, caps in _shipped_capabilities().items():
        assert RuntimeLifecycle(caps.lifecycle), name
        assert BusyDelivery(caps.busy_delivery), name


def test_shipped_driver_lifecycles_are_the_expected_values():
    assert CODEX_CAPABILITIES.lifecycle == RuntimeLifecycle.PERSISTENT_CHILD
    assert CODEX_CAPABILITIES.busy_delivery == BusyDelivery.STEER
    assert claude_capabilities().lifecycle == RuntimeLifecycle.PERSISTENT_CHILD
    assert claude_capabilities().busy_delivery == BusyDelivery.REJECT
    gated = claude_capabilities(message_lifecycle_v1=True)
    assert gated.lifecycle == RuntimeLifecycle.PERSISTENT_CHILD
    assert gated.busy_delivery == BusyDelivery.STEER


def test_runtime_opened_diagnostics_include_admission_contract():
    opened = RuntimeOpened(
        RuntimeRef("runtime"),
        SessionRef("session"),
        "native-session",
        False,
        CODEX_CAPABILITIES,
        ProtocolDiagnostics(native_capabilities=("thread/resume",)),
    )

    assert opened.diagnostics.native_capabilities == ("thread/resume",)
    assert opened.diagnostics.runtime_lifecycle == "persistent_child"
    assert opened.diagnostics.busy_delivery == "steer"


def test_busy_delivery_agrees_with_steer_for_current_shipped_drivers_only():
    """This is an admission invariant, not a law about the enums."""
    for name, caps in _shipped_capabilities().items():
        steerable = SteerCapability(caps.steer) != SteerCapability.NONE
        declares_steer = BusyDelivery(caps.busy_delivery) == BusyDelivery.STEER
        assert steerable == declares_steer, (
            f"{name}: steer={caps.steer} but busy_delivery={caps.busy_delivery}"
        )


def test_reject_busy_delivery_forces_next_turn_regardless_of_steer():
    incoherent = DriverCapabilities(
        session_resume=False,
        inflight_turn_recovery=False,
        steer=SteerCapability.CURRENT_TURN,
        cancel=CancelCapability.NONE,
        context_status=ContextStatusCapability.NONE,
        compact=CompactCapability.NONE,
        permission_bridge=False,
        lifecycle=RuntimeLifecycle.PER_TURN_CHILD,
        busy_delivery=BusyDelivery.REJECT,
    )

    class _Manager:
        driver = object()

        def current_capabilities(self):
            return incoherent

    adapter = RuntimeManagerAdapter.__new__(RuntimeManagerAdapter)
    adapter.manager = _Manager()
    assert adapter.inbox_notice_delivery_capability() == "next_turn"


def test_shipped_drivers_delivery_capability_is_unchanged_by_the_new_gate():
    expected = {
        "codex": "direct",
        "claude-code": "next_turn",
        "claude-code+msg_lifecycle_v1": "gated",
    }
    for name, caps in _shipped_capabilities().items():

        class _Manager:
            driver = object()

            def current_capabilities(self, _c=caps):
                return _c

        adapter = RuntimeManagerAdapter.__new__(RuntimeManagerAdapter)
        adapter.manager = _Manager()
        assert adapter.inbox_notice_delivery_capability() == expected[name], name
